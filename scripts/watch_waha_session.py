"""Recover a failed local WAHA session without changing login state or sending.

Run from the repository root under a user systemd timer. Only FAILED triggers
the documented idempotent restart endpoint; QR and STOPPED require an operator.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlparse

from procurement_bot.config import Settings, get_settings

STATE_FILE = Path("var/run/waha-session-watchdog.json")
RESTART_COOLDOWN_SECONDS = 30 * 60


def _local_base_url(settings: Settings) -> str:
    parsed = urlparse(settings.waha_base_url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query:
        raise RuntimeError("WAHA watchdog requires a local HTTP endpoint")
    hostname = parsed.hostname or ""
    if hostname != "localhost":
        try:
            if not ip_address(hostname).is_loopback:
                raise RuntimeError("WAHA watchdog requires a loopback endpoint")
        except ValueError as exc:
            raise RuntimeError("WAHA watchdog requires a loopback endpoint") from exc
    return settings.waha_base_url.rstrip("/")


def _request(settings: Settings, method: str, suffix: str) -> dict[str, object]:
    key = settings.waha_api_key.get_secret_value().strip()
    if not key:
        raise RuntimeError("WAHA API key is unavailable")
    request = urllib.request.Request(  # noqa: S310 - validated loopback URL
        _local_base_url(settings) + suffix,
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={"X-Api-Key": key, "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            data = response.read(8193)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("WAHA session API is unavailable") from exc
    if len(data) > 8192:
        raise RuntimeError("WAHA session response is too large")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WAHA session response is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("WAHA session response is not an object")
    return parsed


def watch_once(
    settings: Settings, *, state_file: Path = STATE_FILE, now: float | None = None,
) -> str:
    session = quote(settings.waha_session, safe="")
    if not session:
        raise RuntimeError("WAHA session name is empty")
    base = f"/api/sessions/{session}"
    status = _request(settings, "GET", base).get("status")
    if not isinstance(status, str) or not status or len(status) > 40:
        raise RuntimeError("WAHA session status is invalid")
    if status != "FAILED":
        return status

    timestamp = time.time() if now is None else now
    try:
        last = json.loads(state_file.read_text(encoding="utf-8")).get("last_restart_at")
    except (OSError, ValueError, AttributeError):
        last = None
    if isinstance(last, (int, float)) and 0 <= timestamp - last < RESTART_COOLDOWN_SECONDS:
        return "FAILED_COOLDOWN"

    restarted = _request(settings, "POST", base + "/restart")
    if restarted.get("status") not in {"STARTING", "WORKING"}:
        raise RuntimeError("WAHA restart was not acknowledged")
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps({"last_restart_at": timestamp}), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, state_file)
    return "RESTART_REQUESTED"


def main() -> int:
    try:
        status = watch_once(get_settings())
    except RuntimeError as exc:
        print(f"waha_session_watchdog error={exc}")
        return 1
    print(f"waha_session_watchdog status={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
