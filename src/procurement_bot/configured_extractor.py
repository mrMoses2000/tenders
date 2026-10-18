"""Credential-aware lazy construction for the isolated AGY parser profile."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from procurement_bot.config import Settings
from procurement_bot.errors import PermanentProviderError, ProviderAuthenticationDeferred
from procurement_bot.provider_credentials import (
    CredentialFileVersion,
    credential_file_version,
    provider_auth_wait_reason,
    same_credential_file_contents,
    same_file_identity,
)
from procurement_bot.providers.agy import AgyCliStatusError, AgyExtractor, StructuredExtractor

T = TypeVar("T", bound=BaseModel)


class ConfiguredAgyExtractor:
    """Prepare and cache the parser profile on demand, not during worker startup."""

    def __init__(
        self,
        settings: Settings,
        *,
        extractor_factory=AgyExtractor,
    ) -> None:
        self._settings = settings
        self._extractor_factory = extractor_factory
        self._extractor: StructuredExtractor | None = None
        self._profile_token: Path | None = None
        self._profile_version: CredentialFileVersion | None = None
        self._profile_lock = asyncio.Lock()

    async def extract(
        self,
        text: str,
        output_model: type[T],
        *,
        context: Any = None,
        spec_sha256: str = "",
        context_sha256: str = "",
    ) -> T:
        extractor = await self._get_extractor()
        try:
            return await self._invoke_extractor(
                extractor,
                text,
                output_model,
                context=context,
                spec_sha256=spec_sha256,
                context_sha256=context_sha256,
            )
        except PermanentProviderError as exc:
            # AGY prepares a short-lived HOME for each extraction. If its
            # cached profile token vanished after our preflight, rebuild once;
            # no provider request has started at that point.
            if "isolated AGY profile is missing antigravity-oauth-token" not in str(exc):
                raise
            extractor = await self._refresh_extractor(extractor)
            return await self._invoke_extractor(
                extractor,
                text,
                output_model,
                context=context,
                spec_sha256=spec_sha256,
                context_sha256=context_sha256,
            )

    @staticmethod
    async def _invoke_extractor(
        extractor: StructuredExtractor,
        text: str,
        output_model: type[T],
        *,
        context: Any,
        spec_sha256: str,
        context_sha256: str,
    ) -> T:
        try:
            return await extractor.extract(
                text,
                output_model,
                context=context,
                spec_sha256=spec_sha256,
                context_sha256=context_sha256,
            )
        except AgyCliStatusError as exc:
            reason = provider_auth_wait_reason(exc.diagnostic_code)
            if reason is None:
                raise
            raise ProviderAuthenticationDeferred(reason=reason) from exc

    async def _get_extractor(self) -> StructuredExtractor:
        async with self._profile_lock:
            return self._get_extractor_locked()

    async def _refresh_extractor(self, stale: StructuredExtractor) -> StructuredExtractor:
        async with self._profile_lock:
            if self._extractor is not stale:
                if self._extractor is not None:
                    return self._extractor
            self._extractor = None
            self._profile_token = None
            self._profile_version = None
            return self._get_extractor_locked()

    def _get_extractor_locked(self) -> StructuredExtractor:
        token = self._settings.agy_home / ".gemini" / "antigravity-cli" / (
            "antigravity-oauth-token"
        )
        try:
            source = self._settings._available_agy_auth_token()
        except ValueError as exc:
            self._extractor = None
            self._profile_token = None
            self._profile_version = None
            raise ProviderAuthenticationDeferred(
                available_at=datetime.now(UTC) + timedelta(minutes=30),
            ) from exc
        if self._extractor is not None and self._profile_token == token:
            current_version = credential_file_version(token)
            same_contents = same_credential_file_contents(source, token)
            same_path = same_file_identity(source, token)
            if same_contents and (
                current_version == self._profile_version or not same_path
            ):
                # A distinct fallback source containing the same credential is
                # not a rotation. Remember metadata only; never retain bytes or
                # a credential digest in the configured adapter.
                self._profile_version = current_version
                return self._extractor
        try:
            home = self._settings.prepare_isolated_agy_home()
        except ValueError as exc:
            if "AGY auth token was not found:" not in str(exc):
                raise
            raise ProviderAuthenticationDeferred(
                available_at=datetime.now(UTC) + timedelta(minutes=30),
            ) from exc
        self._extractor = self._extractor_factory(
            executable=self._settings.agy_executable,
            timeout_seconds=self._settings.agy_timeout_seconds,
            model=self._settings.agy_model,
            concurrency=self._settings.agy_concurrency,
            home_dir=home,
        )
        self._profile_token = token
        self._profile_version = credential_file_version(token)
        return self._extractor
