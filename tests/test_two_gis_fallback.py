from __future__ import annotations

import pytest
from pydantic import ValidationError

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.browser import UnsafeBrowserResult
from procurement_bot.providers.two_gis_fallback import TwoGisFallbackProvider
from procurement_bot.research import ResearchSource


class _Primary:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def search_suppliers(self, _plan):
        raise self.error


class _Fallback:
    def __init__(self) -> None:
        self.calls = 0

    async def search_suppliers(self, _plan):
        self.calls += 1
        return "fallback-result"


class _Plan:
    def __init__(self, source: ResearchSource) -> None:
        self.source = source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issue", ["mcp_error", "browser_start_failed", "captcha", "access_denied", "login_wall"]
)
async def test_2gis_access_and_browser_failures_use_codex_fallback(issue: str) -> None:
    fallback = _Fallback()
    provider = TwoGisFallbackProvider(
        _Primary(UnsafeBrowserResult("browser unavailable", access_issue=issue)),
        fallback,
    )  # type: ignore[arg-type]

    assert await provider.search_suppliers(_Plan(ResearchSource.TWO_GIS)) == "fallback-result"
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_2gis_transient_or_malformed_agy_failures_use_fallback() -> None:
    errors = [
        RetryableProviderError("AGY browser CLI timeout"),
        PermanentProviderError("AGY browser result_json is invalid JSON"),
        ValidationError.from_exception_data("SupplierBrowserResult", []),
    ]
    for error in errors:
        fallback = _Fallback()
        provider = TwoGisFallbackProvider(_Primary(error), fallback)  # type: ignore[arg-type]
        assert await provider.search_suppliers(_Plan(ResearchSource.TWO_GIS)) == "fallback-result"
        assert fallback.calls == 1


@pytest.mark.asyncio
async def test_2gis_semantic_rejection_does_not_use_fallback() -> None:
    fallback = _Fallback()
    provider = TwoGisFallbackProvider(
        _Primary(UnsafeBrowserResult(
            "browser research cannot manufacture a supplier statement"
        )),
        fallback,
    )  # type: ignore[arg-type]
    with pytest.raises(UnsafeBrowserResult, match="supplier statement"):
        await provider.search_suppliers(_Plan(ResearchSource.TWO_GIS))


@pytest.mark.asyncio
async def test_2gis_missing_contact_card_evidence_rechecks_with_codex() -> None:
    fallback = _Fallback()
    provider = TwoGisFallbackProvider(
        _Primary(UnsafeBrowserResult(
            "2GIS contact must cite its firm card and contact evidence"
        )),
        fallback,
    )  # type: ignore[arg-type]
    assert await provider.search_suppliers(_Plan(ResearchSource.TWO_GIS)) == "fallback-result"
    assert fallback.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["unknown", "no_results"])
async def test_2gis_access_policy_and_unknown_issues_do_not_use_fallback(issue: str) -> None:
    fallback = _Fallback()
    provider = TwoGisFallbackProvider(
        _Primary(UnsafeBrowserResult("blocked", access_issue=issue)),
        fallback,
    )  # type: ignore[arg-type]
    with pytest.raises(UnsafeBrowserResult, match="blocked"):
        await provider.search_suppliers(_Plan(ResearchSource.TWO_GIS))


@pytest.mark.asyncio
async def test_non_2gis_failures_do_not_use_fallback() -> None:
    fallback = _Fallback()
    with pytest.raises(RetryableProviderError):
        await TwoGisFallbackProvider(
            _Primary(RetryableProviderError("timeout")), fallback,
        ).search_suppliers(_Plan(ResearchSource.WEB))
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_non_malformed_permanent_agy_errors_do_not_use_fallback() -> None:
    fallback = _Fallback()
    provider = TwoGisFallbackProvider(
        _Primary(PermanentProviderError("browser policy hash is stale")),
        fallback,
    )  # type: ignore[arg-type]
    with pytest.raises(PermanentProviderError, match="hash is stale"):
        await provider.search_suppliers(_Plan(ResearchSource.TWO_GIS))
    assert fallback.calls == 0
