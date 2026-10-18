from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from procurement_bot.errors import RetryableProviderError


class BrowserResourceWaitTimeout(RetryableProviderError):
    diagnostic_code = "resource_wait_timeout"


@asynccontextmanager
async def bounded_resource(resource: asyncio.Semaphore, timeout_seconds: float):
    """Timeout covers acquisition only; cancellation always releases ownership."""
    try:
        async with asyncio.timeout(timeout_seconds):
            await resource.acquire()
    except TimeoutError as exc:
        raise BrowserResourceWaitTimeout("browser resource wait timed out") from exc
    try:
        yield
    finally:
        resource.release()
