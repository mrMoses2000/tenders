"""One-time local runtime bootstrap without printing or hard-coding secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_POSTGRES_DSN = (
    "postgresql:///transcription_bot?host=/var/run/postgresql"
    "&search_path=procurement_bot"
)
_UNQUOTED_ENV_VALUE = re.compile(r"[A-Za-z0-9_./:@?&%+=,-]*")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    app_env: Path
    waha_env: Path
    generated_secrets: int


def _parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key in sorted(values):
                value = values[key]
                if any(character in value for character in "\n\r\x00"):
                    raise ValueError(f"environment value for {key} is unsafe")
                rendered = (
                    value
                    if _UNQUOTED_ENV_VALUE.fullmatch(value)
                    else f"'{value}'"
                )
                if "'" in value:
                    raise ValueError(f"environment value for {key} contains a quote")
                handle.write(f"{key}={rendered}\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def configure_local_runtime(
    *,
    app_env: Path,
    waha_env: Path,
    telegram_username: str,
) -> BootstrapResult:
    from procurement_bot.telegram_access import normalize_telegram_username

    username = normalize_telegram_username(telegram_username)
    if username is None:
        raise ValueError("invalid Telegram username")

    app = _parse_env(app_env)
    waha = _parse_env(waha_env)
    generated = 0

    api_key = app.get("WAHA_API_KEY") or waha.get("WAHA_API_KEY")
    if not api_key:
        api_key = secrets.token_hex(32)
        generated += 1
    webhook_secret = app.get("WAHA_WEBHOOK_SECRET")
    if not webhook_secret:
        webhook_secret = secrets.token_hex(32)
        generated += 1
    dashboard_password = waha.get("WAHA_DASHBOARD_PASSWORD")
    if not dashboard_password:
        dashboard_password = secrets.token_hex(32)
        generated += 1

    app.update(
        {
            "AGY_BROWSER_MCP_SERVER_JSON": json.dumps(
                {
                    "command": "/home/moses/.local/bin/npx",
                    "args": [
                        "-y",
                        "@playwright/mcp@0.0.82",
                        "--browser",
                        "chrome",
                        "--headless",
                        "--isolated",
                        "--no-webmcp",
                        "--image-responses",
                        "omit",
                        "--output-dir",
                        str(
                            app_env.resolve().parent
                            / "var"
                            / "browser-artifacts"
                        ),
                        "--output-max-size",
                        "52428800",
                    ],
                },
                separators=(",", ":"),
            ),
            "AGY_BROWSER_MCP_SERVER_NAME": "playwright",
            "BROWSER_RESEARCH_ENABLED": "true",
            "VOICE_PROVIDER": "assemblyai",
            "ASSEMBLYAI_ENV_FILE": "/home/moses/audio_transcription/.env",
            "ASSEMBLYAI_KEY_NAME": "ASSEMBLI_AI_5",
            "ASSEMBLYAI_TIMEOUT_SECONDS": "10800",
            "POSTGRES_DSN": DEFAULT_POSTGRES_DSN,
            "TELEGRAM_BOOTSTRAP_USERNAMES": username,
            "WAHA_ENABLED": "true",
            "WAHA_BASE_URL": "http://127.0.0.1:13000",
            "WAHA_API_KEY": api_key,
            "WAHA_SESSION": "default",
            "WAHA_WEBHOOK_ENABLED": "true",
            "WAHA_WEBHOOK_SECRET": webhook_secret,
            # Dedicated Compose bridge gateway; not reachable from the LAN.
            "WAHA_WEBHOOK_BIND_HOST": "172.29.247.1",
            "WAHA_WEBHOOK_ALLOW_PRIVATE_BIND": "true",
            "WAHA_WEBHOOK_PORT": "18081",
        }
    )
    waha.update(
        {
            "TZ": "Asia/Almaty",
            "WAHA_API_KEY": api_key,
            "WAHA_DASHBOARD_ENABLED": "true",
            "WAHA_DASHBOARD_USERNAME": "admin",
            "WAHA_DASHBOARD_PASSWORD": dashboard_password,
            "WHATSAPP_API_HOSTNAME": "127.0.0.1",
            "WHATSAPP_API_PORT": "3000",
            "WAHA_LOG_FORMAT": "JSON",
            # The official container mounts its durable named volume here.
            "WAHA_LOCAL_STORE_BASE_DIR": "/app/.sessions",
            "WAHA_PRINT_QR": "false",
            "NODE_OPTIONS": "--max-old-space-size=2048",
            "WHATSAPP_DEFAULT_ENGINE": "NOWEB",
            "WHATSAPP_HOOK_URL": (
                "http://172.29.247.1:18081/webhooks/waha"
            ),
            "WHATSAPP_HOOK_EVENTS": "message",
            "WHATSAPP_HOOK_CUSTOM_HEADERS": (
                f"X-Webhook-Secret:{webhook_secret}"
            ),
            "WHATSAPP_HOOK_RETRIES_POLICY": "linear",
            "WHATSAPP_HOOK_RETRIES_DELAY_SECONDS": "2",
            "WHATSAPP_HOOK_RETRIES_ATTEMPTS": "15",
        }
    )
    _write_env(app_env, app)
    _write_env(waha_env, waha)
    return BootstrapResult(
        app_env=app_env.resolve(),
        waha_env=waha_env.resolve(),
        generated_secrets=generated,
    )


def fetch_waha_qr(
    *,
    base_url: str,
    api_key: str,
    session: str,
    output: Path,
) -> Path:
    if not api_key:
        raise ValueError("WAHA API key is missing")
    parsed_url = urllib.parse.urlsplit(base_url)
    if parsed_url.scheme != "http" or parsed_url.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("WAHA QR bootstrap requires a loopback HTTP base URL")
    headers = {"X-Api-Key": api_key, "Accept": "image/png"}
    session_url = f"{base_url.rstrip('/')}/api/sessions/{session}"
    status_request = urllib.request.Request(  # noqa: S310
        session_url,
        headers={**headers, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(status_request, timeout=30) as response:  # noqa: S310
            session_status = str(json.load(response).get("status") or "")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        session_status = "MISSING"

    if session_status == "MISSING":
        action_url = f"{base_url.rstrip('/')}/api/sessions"
        action_body = {"name": session}
    elif session_status == "FAILED":
        action_url = f"{session_url}/restart"
        action_body = None
    elif session_status == "STOPPED":
        action_url = f"{session_url}/start"
        action_body = None
    else:
        action_url = ""
        action_body = None
    if action_url:
        action = urllib.request.Request(  # noqa: S310
            action_url,
            data=(
                json.dumps(action_body, separators=(",", ":")).encode()
                if action_body is not None
                else b""
            ),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(action, timeout=30):  # noqa: S310
            pass

    deadline = time.monotonic() + 90
    last_error: Exception | None = None
    while True:
        request = urllib.request.Request(  # noqa: S310 - loopback URL validated above
            f"{base_url.rstrip('/')}/api/{session}/auth/qr",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                content = response.read(2 * 1024 * 1024)
                if (
                    response.headers.get_content_type() == "image/png"
                    and content.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    break
                last_error = RuntimeError("WAHA did not return a PNG QR code")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise RuntimeError("WAHA QR was not ready within 90 seconds") from last_error
        time.sleep(2)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output.resolve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("configure", "qr"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--telegram-username", default="JustMoses0002")
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    if arguments.command == "configure":
        result = configure_local_runtime(
            app_env=root / ".env",
            waha_env=root / "var" / "secrets" / "waha.env",
            telegram_username=arguments.telegram_username,
        )
        print(
            f"configured app={result.app_env} waha={result.waha_env} "
            f"generated_secrets={result.generated_secrets}"
        )
        return

    app = _parse_env(root / ".env")
    path = fetch_waha_qr(
        base_url=app.get("WAHA_BASE_URL", "http://127.0.0.1:13000"),
        api_key=app.get("WAHA_API_KEY", ""),
        session=app.get("WAHA_SESSION", "default"),
        output=root / "var" / "waha-qr.png",
    )
    print(f"qr={path}")


if __name__ == "__main__":
    main()
