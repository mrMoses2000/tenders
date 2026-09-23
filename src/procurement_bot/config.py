from __future__ import annotations

import json
import os
import re
import shutil
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    postgres_dsn: str = "postgresql:///procurement_bot?host=/var/run/postgresql"
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"),
    )
    telegram_allowed_user_ids: str = ""
    telegram_bootstrap_usernames: str = ""
    telegram_api_base_url: str = ""

    log_level: str = "INFO"
    poll_timeout_seconds: int = Field(default=30, ge=1, le=60)
    telegram_ingress_concurrency: int = Field(default=8, ge=1, le=64)
    worker_concurrency: int = Field(default=4, ge=1, le=32)
    job_lease_seconds: int = Field(default=300, ge=30, le=3600)
    max_job_attempts: int = Field(default=5, ge=1, le=20)

    media_root: Path = Path("./var/media")
    results_root: Path = Path("./var/results")
    max_source_file_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    telegram_cloud_download_limit_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_audio_duration_seconds: int = Field(default=3600, ge=1, le=86_400)

    agy_executable: str = "agy"
    agy_model: str = "gemini-3.8-flash-high"
    agy_timeout_seconds: int = Field(default=240, ge=30, le=1800)
    agy_concurrency: int = Field(default=2, ge=1, le=8)
    agy_home: Path = Path("./var/agy-home")
    agy_auth_token_source: Path = Path(
        "~/.gemini/antigravity-cli/antigravity-oauth-token"
    )

    # Dedicated browser-agent profile. This is never the parser profile and is
    # disabled until an operator explicitly provisions exactly one browser MCP.
    browser_research_enabled: bool = False
    agy_browser_home: Path = Path("./var/agy-browser-home")
    agy_browser_mcp_server_name: str = "playwright"
    agy_browser_mcp_server_json: str = ""
    agy_browser_timeout_seconds: int = Field(default=240, ge=30, le=1800)
    agy_browser_concurrency: int = Field(default=1, ge=1, le=4)

    voice_provider: str = "assemblyai"
    assemblyai_env_file: Path = Path("/home/moses/audio_transcription/.env")
    assemblyai_key_name: str = "ASSEMBLI_AI_5"
    assemblyai_timeout_seconds: int = Field(default=10_800, ge=30, le=21_600)
    transcription_min_language_confidence: float = Field(default=0.55, ge=0, le=1)

    waha_enabled: bool = False
    waha_base_url: str = "http://127.0.0.1:3000"
    waha_api_key: SecretStr = SecretStr("")
    waha_session: str = "default"
    waha_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    waha_webhook_enabled: bool = False
    waha_webhook_secret: SecretStr = SecretStr("")
    waha_webhook_bind_host: str = "127.0.0.1"
    waha_webhook_allow_private_bind: bool = False
    waha_webhook_port: int = Field(default=8081, ge=1, le=65535)

    @field_validator(
        "media_root",
        "results_root",
        "agy_home",
        "agy_auth_token_source",
        "agy_browser_home",
        "assemblyai_env_file",
    )
    @classmethod
    def absolute_private_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        normalized = value.upper().strip()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("voice_provider")
    @classmethod
    def valid_voice_provider(cls, value: str) -> str:
        normalized = value.casefold().strip()
        if normalized not in {"assemblyai", "disabled"}:
            raise ValueError("VOICE_PROVIDER must be assemblyai or disabled")
        return normalized

    @field_validator("assemblyai_key_name")
    @classmethod
    def valid_assemblyai_key_name(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"ASSEMBLI_AI_[1-9][0-9]*", normalized):
            raise ValueError("ASSEMBLYAI_KEY_NAME must name an ASSEMBLI_AI_N variable")
        return normalized

    @field_validator("waha_webhook_bind_host")
    @classmethod
    def webhook_must_bind_safe_address(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized == "localhost":
            return normalized
        try:
            address = ip_address(normalized)
        except ValueError as exc:
            raise ValueError("WAHA webhook bind host must be an IP or localhost") from exc
        if address.is_unspecified or not (address.is_loopback or address.is_private):
            raise ValueError("WAHA webhook bind host must be loopback or private unicast")
        return normalized

    @model_validator(mode="after")
    def private_webhook_bind_requires_explicit_opt_in(self) -> Settings:
        host = self.waha_webhook_bind_host
        if host == "localhost" or ip_address(host).is_loopback:
            return self
        if not self.waha_webhook_allow_private_bind:
            raise ValueError("private WAHA webhook bind requires explicit opt-in")
        return self

    def require_telegram_token(self) -> str:
        value = self.telegram_bot_token.get_secret_value().strip()
        if not value:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for the bot process")
        return value

    def require_assemblyai_api_key(self) -> str:
        if not self.assemblyai_env_file.is_file():
            raise ValueError("ASSEMBLYAI_ENV_FILE does not exist")
        values = dotenv_values(self.assemblyai_env_file)
        value = str(values.get(self.assemblyai_key_name) or "").strip()
        if not value:
            raise ValueError("configured AssemblyAI key is missing or empty")
        return value

    def require_telegram_allowed_user_ids(self) -> set[int]:
        raw = self.telegram_allowed_user_ids.strip()
        if not raw:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS is required; use comma-separated numeric IDs"
            )
        try:
            values = {int(value.strip()) for value in raw.split(",") if value.strip()}
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS contains a non-numeric value") from exc
        if not values or any(value <= 0 for value in values):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive IDs")
        return values

    def telegram_access_config(self) -> tuple[set[int], frozenset[str]]:
        ids: set[int] = set()
        raw_ids = self.telegram_allowed_user_ids.strip()
        if raw_ids:
            try:
                ids = {
                    int(value.strip())
                    for value in raw_ids.split(",")
                    if value.strip()
                }
            except ValueError as exc:
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS contains a non-numeric value"
                ) from exc
            if not ids or any(value <= 0 for value in ids):
                raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive IDs")

        from procurement_bot.telegram_access import normalize_bootstrap_usernames

        usernames = normalize_bootstrap_usernames(
            value.strip()
            for value in self.telegram_bootstrap_usernames.split(",")
            if value.strip()
        )
        if not ids and not usernames:
            raise ValueError(
                "configure TELEGRAM_ALLOWED_USER_IDS or TELEGRAM_BOOTSTRAP_USERNAMES"
            )
        return ids, usernames

    def require_waha_api_key(self) -> str:
        value = self.waha_api_key.get_secret_value().strip()
        if not value:
            raise ValueError("WAHA_API_KEY is required when WAHA_ENABLED=true")
        return value

    def require_waha_webhook_secret(self) -> str:
        value = self.waha_webhook_secret.get_secret_value().strip()
        if len(value) < 24:
            raise ValueError("WAHA_WEBHOOK_SECRET must contain at least 24 characters")
        return value

    def require_browser_mcp_server(self) -> dict[str, object]:
        """Return the exact one-server config expected in the isolated browser HOME."""

        if not self.browser_research_enabled:
            raise ValueError("browser research is disabled")
        name = self.agy_browser_mcp_server_name.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name):
            raise ValueError(
                "AGY_BROWSER_MCP_SERVER_NAME contains unsupported characters"
            )
        raw = self.agy_browser_mcp_server_json.strip()
        if not raw:
            raise ValueError(
                "AGY_BROWSER_MCP_SERVER_JSON is required when browser research is enabled"
            )
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("AGY_BROWSER_MCP_SERVER_JSON is invalid JSON") from exc
        if not isinstance(value, dict) or not value:
            raise ValueError("AGY_BROWSER_MCP_SERVER_JSON must be a non-empty object")
        if "env" in value:
            raise ValueError("browser MCP server config must not embed environment secrets")
        if set(value) != {"command", "args"}:
            raise ValueError("browser MCP config must contain exactly command and args")
        command = value["command"]
        arguments = value["args"]
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise ValueError("browser MCP command must be an absolute path")
        if not isinstance(arguments, list) or any(
            not isinstance(argument, str) or "\x00" in argument
            for argument in arguments
        ):
            raise ValueError("browser MCP args must be a JSON array of safe strings")
        return value

    def prepare_isolated_agy_browser_home(self) -> tuple[Path, dict[str, object]]:
        """Provision one browser-only AGY profile with an exact MCP allowlist."""

        server = self.require_browser_mcp_server()
        source = self.agy_auth_token_source
        if not source.is_file():
            raise ValueError(f"AGY auth token was not found: {source}")
        root = self.agy_browser_home
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        profile = root / ".gemini" / "antigravity-cli"
        config_dir = root / ".gemini" / "config"
        for directory in (profile, config_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

        token = profile / "antigravity-oauth-token"
        temporary = profile / ".antigravity-oauth-token.tmp"
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(token)
        token.chmod(0o600)

        name = self.agy_browser_mcp_server_name.strip()
        settings = {
            "toolPermission": "request-review",
            "enableTerminalSandbox": True,
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": [f"mcp({name}/*)"],
                "deny": [
                    "command(*)",
                    "unsandboxed(*)",
                    "read_file(*)",
                    "write_file(*)",
                    "read_url(*)",
                    "execute_url(*)",
                ],
                "ask": [],
            },
        }
        settings_path = profile / "settings.json"
        settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        settings_path.chmod(0o600)
        mcp_path = config_dir / "mcp_config.json"
        mcp_path.write_text(
            json.dumps(
                {"mcpServers": {name: server}},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        mcp_path.chmod(0o600)
        return root, server

    def prepare_directories(self) -> None:
        for path in (self.media_root, self.results_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def prepare_isolated_agy_home(self) -> Path:
        """Create a parser-only AGY profile containing authentication, not MCP config."""

        source = self.agy_auth_token_source
        if not source.is_file():
            raise ValueError(f"AGY auth token was not found: {source}")
        root = self.agy_home
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        profile = root / ".gemini" / "antigravity-cli"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        target = profile / "antigravity-oauth-token"
        temporary = profile / ".antigravity-oauth-token.tmp"
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)
        target.chmod(0o600)
        # No tool permission is granted. The provider uses a minimal transport
        # schema so AGY completes directly instead of exploring the workspace.
        settings_file = profile / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "allowNonWorkspaceAccess": False,
                    "artifactReviewPolicy": "always-proceed",
                    "permissions": {"allow": []},
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        settings_file.chmod(0o600)
        return root


@lru_cache
def get_settings() -> Settings:
    return Settings()
