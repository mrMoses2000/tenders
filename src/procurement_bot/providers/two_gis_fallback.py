from __future__ import annotations

import logging

from pydantic import ValidationError

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.browser import (
    BrowserResearchPort,
    LocalityBrowserResult,
    SupplierBrowserResult,
    UnsafeBrowserResult,
)
from procurement_bot.research import LocalityResearchTask, ResearchSource, SourcePlan

LOGGER = logging.getLogger(__name__)
_ACCESS_FAILURES = frozenset({
    "mcp_error", "browser_start_failed", "captcha", "access_denied", "login_wall",
})
_BROWSER_START_FAILURES = frozenset({
    "configured AGY executable could not start",
})
_LOCALITY_MALFORMED_OUTPUTS = frozenset({
    "AGY browser CLI returned an invalid structured output",
    "AGY browser result_json is invalid JSON",
    "AGY browser result_json must be an object",
    "AGY browser result_json must be a string",
    "AGY browser CLI output exceeded 1 MiB",
})


class TwoGisFallbackProvider:
    """Retry observed 2GIS/optional WEB browser failures through Chrome."""

    def __init__(
        self,
        primary: BrowserResearchPort,
        fallback: BrowserResearchPort,
        *,
        web_fallback_enabled: bool = False,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.web_fallback_enabled = web_fallback_enabled

    async def resolve_locality(self, task: LocalityResearchTask) -> LocalityBrowserResult:
        try:
            result = await self.primary.resolve_locality(task)
        except UnsafeBrowserResult as exc:
            if exc.access_issue not in _ACCESS_FAILURES:
                raise
            LOGGER.warning(
                "2GIS primary locality browser failure (%s); retrying with Codex Chrome",
                type(exc).__name__,
            )
        except RetryableProviderError as exc:
            if str(exc) not in {"AGY browser CLI timeout", "AGY browser CLI transport failed"}:
                raise
            LOGGER.warning("2GIS primary locality browser timed out; retrying with Codex Chrome")
        except PermanentProviderError as exc:
            if str(exc) not in _BROWSER_START_FAILURES | _LOCALITY_MALFORMED_OUTPUTS:
                raise
            LOGGER.warning(
                "2GIS primary locality provider failed; retrying with Codex Chrome"
            )
        except ValidationError:
            LOGGER.warning(
                "2GIS primary locality result failed schema validation; retrying with Codex Chrome"
            )
        else:
            if not isinstance(result, LocalityBrowserResult):
                raise PermanentProviderError("2GIS locality primary returned an invalid result")
            if result.candidates:
                return result
            LOGGER.warning(
                "2GIS primary locality lookup returned no candidates; "
                "retrying with Codex Chrome"
            )

        fallback_result = await self.fallback.resolve_locality(task)
        if not isinstance(fallback_result, LocalityBrowserResult):
            raise PermanentProviderError("2GIS locality fallback returned an invalid result")
        return fallback_result

    async def search_suppliers(self, plan: SourcePlan) -> SupplierBrowserResult:
        try:
            return await self.primary.search_suppliers(plan)
        except UnsafeBrowserResult as exc:
            source_allowed = (
                plan.source == ResearchSource.TWO_GIS
                or (plan.source == ResearchSource.WEB and self.web_fallback_enabled)
            )
            if not source_allowed or exc.access_issue not in _ACCESS_FAILURES:
                raise
            LOGGER.warning(
                "%s AGY browser access issue=%s; retrying with Codex Chrome",
                plan.source.value,
                exc.access_issue,
            )
        except RetryableProviderError as exc:
            source_allowed = (
                plan.source == ResearchSource.TWO_GIS
                or (plan.source == ResearchSource.WEB and self.web_fallback_enabled)
            )
            if not source_allowed or str(exc) != "AGY browser CLI timeout":
                raise
            LOGGER.warning(
                "%s AGY browser timed out; retrying with Codex Chrome",
                plan.source.value,
            )
        except TimeoutError:
            source_allowed = (
                plan.source == ResearchSource.TWO_GIS
                or (plan.source == ResearchSource.WEB and self.web_fallback_enabled)
            )
            if not source_allowed:
                raise
            LOGGER.warning(
                "%s AGY browser batch timed out; retrying with Codex Chrome",
                plan.source.value,
            )
        return await self.fallback.search_suppliers(plan)
