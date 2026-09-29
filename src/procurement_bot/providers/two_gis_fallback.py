from __future__ import annotations

import logging

from pydantic import ValidationError

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.browser import (
    BrowserProviderError,
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
_MALFORMED_AGY_OUTPUT = frozenset({
    "AGY browser CLI returned an invalid structured output",
    "AGY browser result_json is invalid JSON",
    "AGY browser result_json must be an object",
})
_MALFORMED_LOCALITY_OUTPUT = frozenset({
    *_MALFORMED_AGY_OUTPUT,
    "AGY browser result_json must be a string",
})
_LOCALITY_PROVIDER_FAILURES = _MALFORMED_LOCALITY_OUTPUT | frozenset({
    "configured AGY executable could not start",
    "AGY browser CLI output exceeded 1 MiB",
})
_RETRYABLE_CARD_EVIDENCE = frozenset({
    "2GIS contact must cite its firm card and contact evidence",
})


class TwoGisFallbackProvider:
    """Retry operational 2GIS browser failures through the Chrome-backed CLI."""

    def __init__(self, primary: BrowserResearchPort, fallback: BrowserResearchPort) -> None:
        self.primary = primary
        self.fallback = fallback

    async def resolve_locality(self, task: LocalityResearchTask) -> LocalityBrowserResult:
        try:
            result = await self.primary.resolve_locality(task)
        except BrowserProviderError as exc:
            LOGGER.warning(
                "2GIS primary locality browser failure (%s); retrying with Codex Chrome",
                type(exc).__name__,
            )
        except RetryableProviderError:
            LOGGER.warning("2GIS primary locality provider failed; retrying with Codex Chrome")
        except PermanentProviderError as exc:
            if str(exc) not in _LOCALITY_PROVIDER_FAILURES:
                raise
            LOGGER.warning(
                "2GIS primary locality output/provider failed; retrying with Codex Chrome"
            )
        except ValidationError:
            LOGGER.warning(
                "2GIS primary locality result failed schema validation; retrying with Codex Chrome"
            )
        else:
            if not isinstance(result, LocalityBrowserResult):
                LOGGER.warning(
                    "2GIS primary locality result had an invalid type; retrying with Codex Chrome"
                )
            elif result.candidates:
                return result
            else:
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
            if (plan.source != ResearchSource.TWO_GIS
                    or (exc.access_issue not in _ACCESS_FAILURES
                        and str(exc) not in _RETRYABLE_CARD_EVIDENCE)):
                raise
            LOGGER.warning(
                "2GIS AGY browser/card evidence failed; retrying with Codex Chrome: %s",
                str(exc),
            )
        except RetryableProviderError as exc:
            if plan.source != ResearchSource.TWO_GIS:
                raise
            LOGGER.warning("2GIS AGY browser failure; retrying with Codex Chrome: %s", exc)
        except PermanentProviderError as exc:
            if plan.source != ResearchSource.TWO_GIS or str(exc) not in _MALFORMED_AGY_OUTPUT:
                raise
            LOGGER.warning("2GIS AGY output malformed; retrying with Codex Chrome")
        except ValidationError:
            if plan.source != ResearchSource.TWO_GIS:
                raise
            LOGGER.warning("2GIS AGY result failed schema validation; retrying with Codex Chrome")
        return await self.fallback.search_suppliers(plan)
