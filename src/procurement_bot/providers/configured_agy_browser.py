"""Lazy credential-aware construction for the isolated AGY browser profile."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from procurement_bot.config import Settings
from procurement_bot.errors import ProviderAuthenticationDeferred
from procurement_bot.provider_credentials import (
    CredentialFileVersion,
    credential_file_version,
    provider_auth_wait_reason,
    same_credential_file_contents,
    same_file_identity,
)
from procurement_bot.providers.agy import AgyCliStatusError
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.browser import PreparedBrowserCall


class ConfiguredAgyBrowserExecutor:
    """Prepare the browser-only AGY HOME on the first requested browser call."""

    def __init__(
        self,
        settings: Settings,
        *,
        executable: Path,
        mcp_server_name: str,
        expected_mcp_server: Mapping[str, Any],
        transport_factory=AgyBrowserMcpExecutor,
        **transport_kwargs: Any,
    ) -> None:
        self._settings = settings
        self._executable = executable
        self._mcp_server_name = mcp_server_name
        self._expected_mcp_server = dict(expected_mcp_server)
        self._transport_factory = transport_factory
        self._transport_kwargs = transport_kwargs
        self._transport: AgyBrowserMcpExecutor | Any | None = None
        self._profile_token: Path | None = None
        self._profile_version: CredentialFileVersion | None = None
        self._profile_lock = asyncio.Lock()
        # StagedResearchProvider reads this setting without forcing profile setup.
        self.captcha_cooldown_seconds = transport_kwargs["captcha_cooldown_seconds"]

    async def execute(self, call: PreparedBrowserCall) -> Mapping[str, Any]:
        transport = await self._get_transport()
        try:
            return await transport.execute(call)
        except AgyCliStatusError as exc:
            reason = provider_auth_wait_reason(exc.diagnostic_code)
            if reason is None:
                raise
            raise ProviderAuthenticationDeferred(reason=reason) from exc

    async def _get_transport(self):
        async with self._profile_lock:
            token = (
                self._settings.agy_browser_home
                / ".gemini"
                / "antigravity-cli"
                / "antigravity-oauth-token"
            )
            try:
                source = self._settings._available_agy_auth_token()
            except ValueError as exc:
                self._transport = None
                self._profile_token = None
                self._profile_version = None
                raise ProviderAuthenticationDeferred() from exc
            if self._transport is not None and self._profile_token == token:
                current_version = credential_file_version(token)
                same_contents = same_credential_file_contents(source, token)
                same_path = same_file_identity(source, token)
                if same_contents and (
                    current_version == self._profile_version or not same_path
                ):
                    self._profile_version = current_version
                    return self._transport
            try:
                home_dir, server = self._settings.prepare_isolated_agy_browser_home()
            except ValueError as exc:
                if "AGY auth token was not found:" not in str(exc):
                    raise
                raise ProviderAuthenticationDeferred() from exc
            if server != self._expected_mcp_server:
                raise ValueError("browser MCP config changed after startup validation")
            self._transport = self._transport_factory(
                executable=self._executable,
                home_dir=home_dir,
                mcp_server_name=self._mcp_server_name,
                expected_mcp_server=server,
                **self._transport_kwargs,
            )
            self._profile_token = token
            self._profile_version = credential_file_version(token)
            return self._transport
