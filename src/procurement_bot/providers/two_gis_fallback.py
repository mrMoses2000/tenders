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
_MALFORMED_AGY_OUTPUT = frozenset({
    "AGY browser CLI returned an invalid structured output",
    "AGY browser result_json is invalid JSON",
    "AGY browser result_json must be an object",
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
        return await self.primary.resolve_locality(task)

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
