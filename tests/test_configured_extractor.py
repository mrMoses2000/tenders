from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from procurement_bot import cli
from procurement_bot.config import Settings
from procurement_bot.configured_extractor import (
    ConfiguredAgyExtractor,
    ConfiguredStructuredExtractor,
)
from procurement_bot.errors import (
    PermanentProviderError,
    ProviderAuthenticationDeferred,
    RetryableProviderError,
)
from procurement_bot.providers.agy import AgyCliStatusError


class _Result(BaseModel):
    value: str


class _FakeExtractor:
    def __init__(self, **kwargs) -> None:
        self.home_dir = kwargs["home_dir"]
        self.calls = 0

    async def extract(self, _text, output_model, **_kwargs):
        self.calls += 1
        return output_model.model_validate({"value": "ok"})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        AGY_AUTH_TOKEN_SOURCE=tmp_path / "missing-primary-token",
        AGY_HOME=tmp_path / "parser-home",
        AGY_BROWSER_HOME=tmp_path / "browser-home",
    )


@pytest.mark.asyncio
async def test_missing_auth_defers_without_constructing_or_calling_provider(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    created: list[_FakeExtractor] = []

    def factory(**kwargs):
        extractor = _FakeExtractor(**kwargs)
        created.append(extractor)
        return extractor

    configured = ConfiguredAgyExtractor(settings, extractor_factory=factory)

    with pytest.raises(ProviderAuthenticationDeferred) as caught:
        await configured.extract("parse this", _Result)

    assert caught.value.reason == "provider_auth_missing"
    assert created == []
    assert not settings.agy_home.exists()


@pytest.mark.asyncio
async def test_missing_profile_token_recovers_when_primary_auth_returns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    created: list[_FakeExtractor] = []

    def factory(**kwargs):
        extractor = _FakeExtractor(**kwargs)
        created.append(extractor)
        return extractor

    configured = ConfiguredAgyExtractor(settings, extractor_factory=factory)
    with pytest.raises(ProviderAuthenticationDeferred):
        await configured.extract("first", _Result)

    primary = settings.agy_auth_token_source
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("first-token", encoding="utf-8")
    first = await configured.extract("first", _Result)
    assert first.value == "ok"
    assert len(created) == 1 and created[0].calls == 1

    cached_token = settings.agy_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    cached_token.unlink()
    primary.unlink()
    with pytest.raises(ProviderAuthenticationDeferred):
        await configured.extract("missing again", _Result)
    assert created[0].calls == 1

    primary.write_text("restored-token", encoding="utf-8")
    restored = await configured.extract("restored", _Result)
    assert restored.value == "ok"
    assert len(created) == 2
    assert created[1].calls == 1
    assert cached_token.read_text(encoding="utf-8") == "restored-token"


@pytest.mark.asyncio
async def test_cached_parser_profile_refreshes_after_primary_token_rotation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.parent.mkdir(parents=True, exist_ok=True)
    settings.agy_auth_token_source.write_bytes(b"token-a")
    created: list[_FakeExtractor] = []
    configured = ConfiguredAgyExtractor(
        settings,
        extractor_factory=lambda **kwargs: created.append(_FakeExtractor(**kwargs))
        or created[-1],
    )

    await configured.extract("first", _Result)
    old_version = settings.agy_auth_token_source.stat()
    settings.agy_auth_token_source.write_bytes(b"token-b")
    os.utime(
        settings.agy_auth_token_source,
        ns=(old_version.st_atime_ns, old_version.st_mtime_ns + 1_000_000_000),
    )

    await configured.extract("after rotation", _Result)
    assert len(created) == 2
    assert created[0].calls == 1
    assert created[1].calls == 1
    assert (
        settings.agy_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    ).read_bytes() == b"token-b"


@pytest.mark.asyncio
async def test_primary_removal_with_same_parser_fallback_keeps_cached_extractor(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.parent.mkdir(parents=True, exist_ok=True)
    settings.agy_auth_token_source.write_bytes(b"same-token")
    created: list[_FakeExtractor] = []

    def factory(**kwargs):
        extractor = _FakeExtractor(**kwargs)
        created.append(extractor)
        return extractor

    configured = ConfiguredAgyExtractor(settings, extractor_factory=factory)
    await configured.extract("first", _Result)
    settings.agy_auth_token_source.unlink()

    await configured.extract("fallback", _Result)
    assert len(created) == 1
    assert created[0].calls == 2


@pytest.mark.asyncio
async def test_concurrent_parser_refresh_builds_one_extractor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.parent.mkdir(parents=True, exist_ok=True)
    settings.agy_auth_token_source.write_bytes(b"token-a")
    created: list[_FakeExtractor] = []

    def factory(**kwargs):
        extractor = _FakeExtractor(**kwargs)
        created.append(extractor)
        return extractor

    configured = ConfiguredAgyExtractor(settings, extractor_factory=factory)
    await configured.extract("prime cache", _Result)
    settings.agy_auth_token_source.write_bytes(b"token-b")
    await asyncio.gather(
        *(configured.extract(f"parallel-{index}", _Result) for index in range(8))
    )
    assert len(created) == 2
    assert created[1].calls == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic_code", "expected_reason"),
    [
        ("cli_auth_failed", "provider_auth_failed"),
        ("cli_usage_limit", None),
        ("cli_account_ineligible", None),
    ],
)
async def test_only_typed_auth_denial_defers_without_a_second_provider_call(
    tmp_path: Path, diagnostic_code: str, expected_reason: str | None
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.parent.mkdir(parents=True, exist_ok=True)
    settings.agy_auth_token_source.write_bytes(b"token")

    class FailingExtractor:
        calls = 0

        async def extract(self, *_args, **_kwargs):
            self.calls += 1
            raise AgyCliStatusError(diagnostic_code)

    extractor = FailingExtractor()
    configured = ConfiguredAgyExtractor(settings, extractor_factory=lambda **_: extractor)
    if expected_reason is not None:
        with pytest.raises(ProviderAuthenticationDeferred) as caught:
            await configured.extract("one provider call", _Result)
        assert caught.value.reason == expected_reason
        assert caught.value.diagnostic_code == diagnostic_code
    else:
        with pytest.raises(RetryableProviderError) as caught:
            await configured.extract("one provider call", _Result)
        assert caught.value is not None
        assert isinstance(caught.value, AgyCliStatusError)
        assert caught.value.diagnostic_code == diagnostic_code
    assert extractor.calls == 1


def test_provider_auth_deferred_constructor_keeps_missing_reason_compatibility() -> None:
    deferred = ProviderAuthenticationDeferred()
    assert deferred.reason == "provider_auth_missing"
    assert ProviderAuthenticationDeferred.reason == "provider_auth_missing"


@pytest.mark.asyncio
async def test_auth_denial_after_missing_profile_recovery_is_deferred_once(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.parent.mkdir(parents=True, exist_ok=True)
    settings.agy_auth_token_source.write_bytes(b"token")
    created = []

    class MissingProfileOnce:
        calls = 0

        async def extract(self, *_args, **_kwargs):
            self.calls += 1
            raise PermanentProviderError(
                "isolated AGY profile is missing antigravity-oauth-token"
            )

    class AuthDenied:
        calls = 0

        async def extract(self, *_args, **_kwargs):
            self.calls += 1
            raise AgyCliStatusError("cli_auth_failed")

    def factory(**_kwargs):
        extractor = MissingProfileOnce() if not created else AuthDenied()
        created.append(extractor)
        return extractor

    configured = ConfiguredAgyExtractor(settings, extractor_factory=factory)
    with pytest.raises(ProviderAuthenticationDeferred) as caught:
        await configured.extract("single recovery", _Result)

    assert caught.value.reason == "provider_auth_failed"
    assert [extractor.calls for extractor in created] == [1, 1]


def test_worker_startup_does_not_prepare_agy_profile_without_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "media_root": tmp_path / "media",
            "voice_provider": "disabled",
            "worker_concurrency": 1,
            "waha_enabled": False,
            "supplier_media_enabled": False,
        }
    )
    captured: dict[str, object] = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            captured["extractor"] = kwargs["intake"].extractor

        async def run_forever(self):
            await asyncio.Event().wait()

    class FakeOutboxWorker:
        def __init__(self, **_kwargs):
            pass

        async def run_forever(self):
            await asyncio.Event().wait()

    class TaskCollector:
        def __init__(self):
            self.coroutines = []

        def create_task(self, coroutine):
            self.coroutines.append(coroutine)

    monkeypatch.setattr(cli, "JobWorker", FakeWorker)
    monkeypatch.setattr(cli, "TelegramOutboxWorker", FakeOutboxWorker)
    tasks = TaskCollector()
    try:
        cli._start_worker_tasks(tasks, settings, object(), object())
        assert isinstance(captured["extractor"], ConfiguredStructuredExtractor)
        assert isinstance(captured["extractor"]._extractor, ConfiguredAgyExtractor)
        assert not settings.agy_home.exists()
    finally:
        for coroutine in tasks.coroutines:
            coroutine.close()
