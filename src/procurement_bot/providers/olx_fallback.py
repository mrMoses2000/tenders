from __future__ import annotations

import logging

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
    "captcha", "access_denied", "login_wall", "mcp_error", "browser_start_failed",
})
_MALFORMED_AGY_OUTPUT = frozenset({
    "AGY browser CLI returned an invalid structured output",
    "AGY browser result_json is invalid JSON",
    "AGY browser result_json must be an object",
})


class OlxFallbackProvider:
    """Use the existing Chrome profile only when OLX Playwright access fails."""

    def __init__(self, primary: BrowserResearchPort, fallback: BrowserResearchPort) -> None:
        self.primary = primary
        self.fallback = fallback

    async def resolve_locality(self, task: LocalityResearchTask) -> LocalityBrowserResult:
        return await self.primary.resolve_locality(task)

    async def search_suppliers(self, plan: SourcePlan) -> SupplierBrowserResult:
        try:
            return await self.primary.search_suppliers(plan)
        except UnsafeBrowserResult as exc:
            if plan.source != ResearchSource.OLX or exc.access_issue not in _ACCESS_FAILURES:
                raise
            LOGGER.warning("OLX AGY access issue=%s; retrying with Codex Chrome", exc.access_issue)
        except RetryableProviderError as exc:
            if plan.source != ResearchSource.OLX or str(exc) != "AGY browser CLI timeout":
                raise
            LOGGER.warning("OLX AGY browser timeout; retrying with Codex Chrome")
        except PermanentProviderError as exc:
            if plan.source != ResearchSource.OLX or str(exc) not in _MALFORMED_AGY_OUTPUT:
                raise
            LOGGER.warning("OLX AGY browser output malformed; retrying with Codex Chrome")
        return await self.fallback.search_suppliers(plan)
