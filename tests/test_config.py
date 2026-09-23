import json
from pathlib import Path

import pytest

from procurement_bot.config import Settings


def test_settings_resolve_private_roots(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        MEDIA_ROOT=tmp_path / "media",
        RESULTS_ROOT=tmp_path / "results",
    )
    settings.prepare_directories()
    assert settings.media_root.is_absolute()
    assert settings.media_root.is_dir()


def test_bot_token_is_required_only_for_bot_process():
    settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="")
    with pytest.raises(ValueError, match="required"):
        settings.require_telegram_token()


def test_legacy_telegram_token_name_is_accepted_without_exposing_value():
    settings = Settings(_env_file=None, TELEGRAM_TOKEN="test-token")  # noqa: S106
    assert settings.require_telegram_token() == "test-token"


def test_telegram_allowlist_is_deny_by_default():
    settings = Settings(_env_file=None, TELEGRAM_ALLOWED_USER_IDS="")
    with pytest.raises(ValueError, match="required"):
        settings.require_telegram_allowed_user_ids()


def test_telegram_allowlist_parses_comma_separated_ids():
    settings = Settings(_env_file=None, TELEGRAM_ALLOWED_USER_IDS="123, 456,123")
    assert settings.require_telegram_allowed_user_ids() == {123, 456}


def test_telegram_access_can_bootstrap_a_known_username():
    settings = Settings(
        _env_file=None,
        TELEGRAM_ALLOWED_USER_IDS="",
        TELEGRAM_BOOTSTRAP_USERNAMES="@JustMoses0002",
    )
    assert settings.telegram_access_config() == (set(), frozenset({"justmoses0002"}))


def test_telegram_access_is_deny_by_default_without_id_or_username():
    settings = Settings(_env_file=None)
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        settings.telegram_access_config()


def test_waha_key_is_required_before_enabled_worker_starts():
    settings = Settings(_env_file=None, WAHA_API_KEY="")
    with pytest.raises(ValueError, match="WAHA_API_KEY"):
        settings.require_waha_api_key()

    configured = Settings(_env_file=None, WAHA_API_KEY=" secret ")
    assert configured.require_waha_api_key() == "secret"


def test_assemblyai_key_is_loaded_from_exact_external_variable(tmp_path: Path) -> None:
    source = tmp_path / "external.env"
    source.write_text(
        "ASSEMBLI_AI_4=wrong\nASSEMBLI_AI_5='right-private-key'\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    settings = Settings(
        _env_file=None,
        ASSEMBLYAI_ENV_FILE=source,
        ASSEMBLYAI_KEY_NAME="ASSEMBLI_AI_5",
    )
    assert settings.require_assemblyai_api_key() == "right-private-key"


def test_assemblyai_missing_exact_key_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "external.env"
    source.write_text("ASSEMBLI_AI_4=not-the-requested-key\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        ASSEMBLYAI_ENV_FILE=source,
        ASSEMBLYAI_KEY_NAME="ASSEMBLI_AI_5",
    )
    with pytest.raises(ValueError, match="missing or empty"):
        settings.require_assemblyai_api_key()


def test_waha_webhook_requires_a_distinct_long_secret() -> None:
    with pytest.raises(ValueError, match="at least 24"):
        Settings(  # noqa: S106 - deterministic non-secret test fixture
            _env_file=None,
            WAHA_WEBHOOK_SECRET="short",  # noqa: S106
        ).require_waha_webhook_secret()
    configured = Settings(
        _env_file=None,
        WAHA_WEBHOOK_SECRET="webhook-secret-that-is-long-enough",  # noqa: S106
    )
    assert configured.require_waha_webhook_secret() == "webhook-secret-that-is-long-enough"

    with pytest.raises(ValueError, match="loopback"):
        Settings(_env_file=None, WAHA_WEBHOOK_BIND_HOST="0.0.0.0")  # noqa: S104

    with pytest.raises(ValueError, match="explicit opt-in"):
        Settings(_env_file=None, WAHA_WEBHOOK_BIND_HOST="172.29.247.1")

    private = Settings(
        _env_file=None,
        WAHA_WEBHOOK_BIND_HOST="172.29.247.1",
        WAHA_WEBHOOK_ALLOW_PRIVATE_BIND=True,
    )
    assert private.waha_webhook_bind_host == "172.29.247.1"


def test_isolated_agy_home_copies_only_auth_token(tmp_path: Path):
    source = tmp_path / "source-token"
    source.write_text("secret-token", encoding="utf-8")
    isolated = tmp_path / "agy-home"
    settings = Settings(
        _env_file=None,
        AGY_HOME=isolated,
        AGY_AUTH_TOKEN_SOURCE=source,
    )

    root = settings.prepare_isolated_agy_home()

    target = root / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    assert target.read_text(encoding="utf-8") == "secret-token"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (root / ".gemini" / "antigravity" / "mcp_config.json").exists()
    settings_file = root / ".gemini" / "antigravity-cli" / "settings.json"
    assert json.loads(settings_file.read_text())["permissions"]["allow"] == []


def test_browser_mcp_config_is_explicit_and_secret_free() -> None:
    disabled = Settings(_env_file=None)
    with pytest.raises(ValueError, match="disabled"):
        disabled.require_browser_mcp_server()

    configured = Settings(
        _env_file=None,
        BROWSER_RESEARCH_ENABLED=True,
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["-y","@playwright/mcp@1.2.3"]}'
        ),
    )
    assert configured.require_browser_mcp_server() == {
        "command": "/usr/bin/npx",
        "args": ["-y", "@playwright/mcp@1.2.3"],
    }

    with pytest.raises(ValueError, match="must not embed"):
        Settings(
            _env_file=None,
            BROWSER_RESEARCH_ENABLED=True,
            AGY_BROWSER_MCP_SERVER_JSON=(
                '{"command":"node","env":{"TOKEN":"secret"}}'
            ),
        ).require_browser_mcp_server()


def test_browser_home_allows_only_the_exact_mcp(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("browser-auth", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        BROWSER_RESEARCH_ENABLED=True,
        AGY_AUTH_TOKEN_SOURCE=token,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="browsermcp",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@browsermcp/mcp@1.2.3"]}'
        ),
    )

    home, server = settings.prepare_isolated_agy_browser_home()

    profile = home / ".gemini" / "antigravity-cli"
    policy = json.loads((profile / "settings.json").read_text())
    assert policy["permissions"]["allow"] == ["mcp(browsermcp/*)"]
    assert "command(*)" in policy["permissions"]["deny"]
    assert json.loads((home / ".gemini/config/mcp_config.json").read_text()) == {
        "mcpServers": {"browsermcp": server}
    }
    assert (profile / "antigravity-oauth-token").stat().st_mode & 0o777 == 0o600
