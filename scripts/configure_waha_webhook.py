"""Preview or apply the authenticated Uran WAHA message webhook repair."""

from __future__ import annotations

import argparse
import asyncio
import copy
import ipaddress
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

from dotenv import dotenv_values, set_key

from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.providers.waha import WahaClient
from scripts.manage_uran_launch import EXPECTED_BASELINE_SHA256, _read_target
from scripts.run_uran_three_item_pilot import _capture_baseline

EXPECTED_CASE_ID = UUID("889f7d8e-5b6d-4906-ac34-5074f9cbb924")
EXPECTED_RUN_ID = UUID("9729892b-b8f0-474a-a75a-1844ce4b6d21")
EXPECTED_EVENT = "message"
EXPECTED_HEADER = "X-Webhook-Secret"
WAHA_ENV_PATH = Path("var/secrets/waha.env")


class ConfigurationRefusal(RuntimeError):
    """The repair guard failed; no configuration should be changed."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def _validate_webhook_url(settings: Any, env_values: dict[str, str]) -> str:
    url = env_values.get("WHATSAPP_HOOK_URL", "")
    parsed = urlsplit(url)
    expected_netloc = f"{settings.waha_webhook_bind_host}:{settings.waha_webhook_port}"
    if (
        parsed.scheme != "http"
        or parsed.hostname != settings.waha_webhook_bind_host
        or parsed.port != settings.waha_webhook_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/webhooks/waha"
        or parsed.query
        or parsed.fragment
        or parsed.netloc != expected_netloc
    ):
        raise ConfigurationRefusal("configured hook route does not match the app webhook bind")
    return url


def _parse_env_headers(raw: str) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for part in raw.split(";"):
        if not part.strip():
            continue
        name, separator, value = part.partition(":")
        if not separator or not name.strip():
            raise ConfigurationRefusal("existing WAHA custom headers are malformed")
        headers.append((name.strip(), value.strip()))
    return headers


def _render_env_headers(headers: list[tuple[str, str]]) -> str:
    return ";".join(f"{name}:{value}" for name, value in headers)


def _merge_env_headers(raw: str, secret: str) -> str:
    if any(character in secret for character in ";\n\r\x00"):
        raise ConfigurationRefusal("webhook secret cannot be represented in WAHA custom headers")
    parsed = _parse_env_headers(raw)
    merged: list[tuple[str, str]] = []
    secret_written = False
    for name, value in parsed:
        if name.casefold() == EXPECTED_HEADER.casefold():
            if not secret_written:
                merged.append((EXPECTED_HEADER, secret))
                secret_written = True
            continue
        merged.append((name, value))
    if not secret_written:
        merged.append((EXPECTED_HEADER, secret))
    if any("\n" in value or "\r" in value or "\x00" in value for _, value in merged):
        raise ConfigurationRefusal("existing WAHA custom headers contain unsafe characters")
    return _render_env_headers(merged)


def _headers_from_api(raw: Any, secret: str) -> Any:
    if raw is None:
        raw = []
    if isinstance(raw, list):
        headers = copy.deepcopy(raw)
        matches = [
            item
            for item in headers
            if isinstance(item, dict)
            and str(item.get("name", "")).casefold() == EXPECTED_HEADER.casefold()
        ]
        if matches:
            for item in matches:
                item["name"] = EXPECTED_HEADER
                item["value"] = secret
        else:
            headers.append({"name": EXPECTED_HEADER, "value": secret})
        return headers
    if isinstance(raw, dict):
        headers = copy.deepcopy(raw)
        matches = [key for key in headers if str(key).casefold() == EXPECTED_HEADER.casefold()]
        for key in matches:
            headers.pop(key)
        headers[EXPECTED_HEADER] = secret
        return headers
    raise ConfigurationRefusal("session customHeaders has an unsupported shape")


def merge_session_config(
    current_config: dict[str, Any], *, webhook_url: str, secret: str
) -> dict[str, Any]:
    """Add the message event and auth header while preserving all other config."""

    if not isinstance(current_config, dict):
        raise ConfigurationRefusal("session config is not an object")
    merged = copy.deepcopy(current_config)
    webhooks = merged.get("webhooks", [])
    if not isinstance(webhooks, list) or any(not isinstance(item, dict) for item in webhooks):
        raise ConfigurationRefusal("session webhooks has an unsupported shape")

    matching = [
        item
        for item in webhooks
        if isinstance(item.get("url"), str) and item["url"] == webhook_url
    ]
    if matching:
        for hook in matching:
            events = hook.get("events", [])
            if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
                raise ConfigurationRefusal("matching webhook events has an unsupported shape")
            if EXPECTED_EVENT not in events:
                events.append(EXPECTED_EVENT)
            hook["events"] = events
            hook["customHeaders"] = _headers_from_api(hook.get("customHeaders"), secret)
    else:
        webhooks.append(
            {
                "url": webhook_url,
                "events": [EXPECTED_EVENT],
                "customHeaders": [{"name": EXPECTED_HEADER, "value": secret}],
            }
        )
    merged["webhooks"] = webhooks
    return merged


def _session_has_required_header(config: Any, webhook_url: str, secret: str) -> bool:
    if not isinstance(config, dict) or not isinstance(config.get("webhooks"), list):
        return False
    for hook in config["webhooks"]:
        if not isinstance(hook, dict) or hook.get("url") != webhook_url:
            continue
        events = hook.get("events", [])
        if not isinstance(events, list) or EXPECTED_EVENT not in events:
            continue
        raw = hook.get("customHeaders", [])
        if isinstance(raw, list):
            if any(
                isinstance(item, dict)
                and str(item.get("name", "")).casefold() == EXPECTED_HEADER.casefold()
                and item.get("value") == secret
                for item in raw
            ):
                return True
        elif isinstance(raw, dict):
            if any(
                str(key).casefold() == EXPECTED_HEADER.casefold() and value == secret
                for key, value in raw.items()
            ):
                return True
    return False


def _backup_private(path: Path, data: bytes, *, suffix: str) -> Path:
    directory = path.parent / "waha-webhook-backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = directory / f"{timestamp}-{suffix}"
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    backup.chmod(0o600)
    return backup


def _api_url(base_url: str, session: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationRefusal("WAHA API base URL is not safe for authenticated access")
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname.casefold() == "localhost"
    if not is_loopback:
        raise ConfigurationRefusal("authenticated WAHA access is restricted to loopback")
    return urljoin(base_url.rstrip("/") + "/", f"api/sessions/{quote(session, safe='')}")


async def _api_request(
    client: WahaClient,
    url: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    response = await client._transport.request(
        method=method,
        url=url,
        headers={
            "Accept": "application/json",
            "X-Api-Key": client._api_key,
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        body=body,
        timeout_seconds=timeout_seconds or client._timeout_seconds,
    )
    if not 200 <= response.status < 300:
        raise ConfigurationRefusal(
            f"WAHA API request failed with HTTP {response.status}",
            http_status=response.status,
        )
    if not response.body:
        return {}
    try:
        result = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigurationRefusal("WAHA API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ConfigurationRefusal("WAHA API returned an unexpected response shape")
    return result


async def _guard_uran_baseline() -> dict[str, Any]:
    settings = get_settings()
    pool = await create_pool(settings.postgres_dsn)
    try:
        async with pool.acquire() as connection:
            target = await _read_target(connection)
            baseline = await _capture_baseline(connection, target["group_ids"])
    finally:
        await pool.close()
    if target["run"]["case_id"] != EXPECTED_CASE_ID or target["run"]["id"] != EXPECTED_RUN_ID:
        raise ConfigurationRefusal("Uran case/run identity guard failed")
    if len(target["group_ids"]) != 55 or baseline["baseline_hash"] != EXPECTED_BASELINE_SHA256:
        raise ConfigurationRefusal("Uran frozen 55-item baseline guard failed")
    return {
        "case_id_matches": True,
        "run_id_matches": True,
        "baseline_groups": len(target["group_ids"]),
        "baseline_matches": True,
    }


async def configure(*, apply: bool = False) -> dict[str, Any]:
    settings = get_settings()
    guard = await _guard_uran_baseline()
    if not settings.waha_enabled:
        raise ConfigurationRefusal("WAHA provider access is disabled")
    if not settings.waha_webhook_enabled:
        raise ConfigurationRefusal("application webhook ingress is disabled")
    secret = settings.require_waha_webhook_secret()
    env_path = WAHA_ENV_PATH
    if env_path.is_symlink() or not env_path.is_file():
        raise ConfigurationRefusal("private WAHA environment file is missing or unsafe")
    env_values = {key: value or "" for key, value in dotenv_values(env_path).items() if key}
    webhook_url = _validate_webhook_url(settings, env_values)
    env_headers = _merge_env_headers(env_values.get("WHATSAPP_HOOK_CUSTOM_HEADERS", ""), secret)
    env_changed = env_headers != env_values.get("WHATSAPP_HOOK_CUSTOM_HEADERS", "")

    client = WahaClient(
        base_url=settings.waha_base_url,
        api_key=settings.require_waha_api_key(),
        session=settings.waha_session,
        enabled=settings.waha_enabled,
        timeout_seconds=settings.waha_timeout_seconds,
    )
    api_url = _api_url(settings.waha_base_url, settings.waha_session)
    session = await _api_request(client, api_url, "GET")
    if session.get("name") != settings.waha_session or session.get("status") != "WORKING":
        raise ConfigurationRefusal("allowlisted WAHA session must be WORKING before update")
    current_config = session.get("config", {})
    next_config = merge_session_config(current_config, webhook_url=webhook_url, secret=secret)
    session_changed = next_config != current_config
    event_present_before = False
    if isinstance(current_config, dict) and isinstance(current_config.get("webhooks", []), list):
        event_present_before = any(
            isinstance(hook, dict)
            and hook.get("url") == webhook_url
            and isinstance(hook.get("events", []), list)
            and EXPECTED_EVENT in hook.get("events", [])
            for hook in current_config.get("webhooks", [])
        )
    result: dict[str, Any] = {
        **guard,
        "preview": not apply,
        "session_working_before": True,
        "webhook_route_matches_bind": True,
        "event_present_before": event_present_before,
        "session_config_change_needed": session_changed,
        "env_header_change_needed": env_changed,
        "env_header_merge_preserves_existing": True,
        "applied": False,
    }
    if not apply or (not session_changed and not env_changed):
        result["verified"] = _session_has_required_header(current_config, webhook_url, secret)
        result["env_header_matches"] = not env_changed
        return result

    raw_env = env_path.read_bytes()
    raw_config = json.dumps(current_config, ensure_ascii=False, sort_keys=True, indent=2).encode()
    env_backup = _backup_private(env_path, raw_env, suffix="waha-env.bak")
    config_backup = _backup_private(env_path, raw_config, suffix="session-config.json")
    result["private_backups_created"] = True
    result["backup_modes_private"] = all(
        stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (env_backup, config_backup)
    )

    if env_changed:
        set_key(str(env_path), "WHATSAPP_HOOK_CUSTOM_HEADERS", env_headers, quote_mode="always")
        env_path.chmod(0o600)
        persisted_headers = dotenv_values(env_path).get("WHATSAPP_HOOK_CUSTOM_HEADERS") or ""
        if persisted_headers != env_headers:
            raise ConfigurationRefusal("WAHA environment header did not persist exactly")

    if session_changed:
        await _api_request(
            client, api_url, "PUT", {"name": settings.waha_session, "config": next_config},
        )
        deadline = time.monotonic() + 60
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigurationRefusal(
                    "WAHA session did not return to WORKING within 60 seconds"
                )
            session = await _api_request(
                client,
                api_url,
                "GET",
                timeout_seconds=min(client._timeout_seconds, remaining),
            )
            if session.get("status") == "WORKING":
                break
            await asyncio.sleep(3)
    else:
        session = await _api_request(client, api_url, "GET")

    verified = _session_has_required_header(session.get("config"), webhook_url, secret)
    env_after = {key: value or "" for key, value in dotenv_values(env_path).items() if key}
    env_header_verified = _merge_env_headers(
        env_after.get("WHATSAPP_HOOK_CUSTOM_HEADERS", ""), secret
    ) == env_after.get("WHATSAPP_HOOK_CUSTOM_HEADERS", "")
    result.update(
        {
            "applied": True,
            "session_working_after": session.get("status") == "WORKING",
            "verified": verified,
            "env_header_matches": env_header_verified,
        }
    )
    if not verified or not env_header_verified:
        raise ConfigurationRefusal("post-update webhook configuration verification failed")
    return result


async def _main(apply: bool) -> int:
    try:
        result = await configure(apply=apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        error = {"ok": False, "error_class": type(exc).__name__}
        if isinstance(exc, ConfigurationRefusal) and exc.http_status is not None:
            error["http_status"] = exc.http_status
        print(json.dumps(error, sort_keys=True))
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the guarded configuration repair",
    )
    raise SystemExit(asyncio.run(_main(parser.parse_args().apply)))
