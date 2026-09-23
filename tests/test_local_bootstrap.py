from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from procurement_bot.local_bootstrap import configure_local_runtime, fetch_waha_qr


def _values(path: Path) -> dict[str, str]:
    return {key: value or "" for key, value in dotenv_values(path).items()}


def test_configure_preserves_token_and_generates_matching_waha_secrets(
    tmp_path: Path,
) -> None:
    app = tmp_path / ".env"
    app.write_text("TELEGRAM_TOKEN=kept-secret\n", encoding="utf-8")
    waha = tmp_path / "var/secrets/waha.env"

    result = configure_local_runtime(
        app_env=app,
        waha_env=waha,
        telegram_username="@JustMoses0002",
    )

    app_values = _values(app)
    waha_values = _values(waha)
    assert app_values["TELEGRAM_TOKEN"] == "kept-secret"  # noqa: S105
    assert app_values["TELEGRAM_BOOTSTRAP_USERNAMES"] == "justmoses0002"
    assert app_values["WAHA_ENABLED"] == "true"
    assert app_values["WAHA_WEBHOOK_ENABLED"] == "true"
    assert app_values["WAHA_WEBHOOK_BIND_HOST"] == "172.29.247.1"
    assert app_values["WAHA_WEBHOOK_ALLOW_PRIVATE_BIND"] == "true"
    assert app_values["BROWSER_RESEARCH_ENABLED"] == "true"
    assert app_values["VOICE_PROVIDER"] == "assemblyai"
    assert app_values["ASSEMBLYAI_ENV_FILE"] == "/home/moses/audio_transcription/.env"
    assert app_values["ASSEMBLYAI_KEY_NAME"] == "ASSEMBLI_AI_5"
    assert "@playwright/mcp@0.0.82" in app_values[
        "AGY_BROWSER_MCP_SERVER_JSON"
    ]
    assert app_values["WAHA_API_KEY"] == waha_values["WAHA_API_KEY"]
    assert app_values["WAHA_WEBHOOK_SECRET"] in waha_values[
        "WHATSAPP_HOOK_CUSTOM_HEADERS"
    ]
    assert waha_values["WAHA_LOCAL_STORE_BASE_DIR"] == "/app/.sessions"
    assert waha_values["WHATSAPP_API_PORT"] == "3000"
    assert (
        waha_values["WHATSAPP_HOOK_URL"]
        == "http://172.29.247.1:18081/webhooks/waha"
    )
    assert result.generated_secrets == 3
    assert app.stat().st_mode & 0o777 == 0o600
    assert waha.stat().st_mode & 0o777 == 0o600


def test_configure_is_idempotent_and_does_not_rotate_secrets(tmp_path: Path) -> None:
    app = tmp_path / ".env"
    app.write_text("TELEGRAM_TOKEN=kept-secret\n", encoding="utf-8")
    waha = tmp_path / "waha.env"
    configure_local_runtime(
        app_env=app,
        waha_env=waha,
        telegram_username="JustMoses0002",
    )
    before_app = _values(app)
    before_waha = _values(waha)

    result = configure_local_runtime(
        app_env=app,
        waha_env=waha,
        telegram_username="JustMoses0002",
    )

    assert result.generated_secrets == 0
    assert _values(app) == before_app
    assert _values(waha) == before_waha


def test_qr_bootstrap_rejects_non_loopback_server(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        fetch_waha_qr(
            base_url="https://example.com",
            api_key="not-a-real-secret",  # noqa: S106
            session="default",
            output=tmp_path / "qr.png",
        )
