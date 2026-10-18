import asyncio
import fcntl
import json
import os
from pathlib import Path

import pytest

from procurement_bot.providers.codex_chrome import CodexChromeExecutor
from procurement_bot.providers.provider_diagnostics import (
    ProviderAttemptDiagnostics,
    safe_error_code,
)
from procurement_bot.providers.resource_wait import BrowserResourceWaitTimeout, bounded_resource


@pytest.mark.asyncio
async def test_resource_wait_timeout_does_not_release_another_owner():
    resource = asyncio.Semaphore(1)
    await resource.acquire()
    with pytest.raises(BrowserResourceWaitTimeout):
        async with bounded_resource(resource, 0.01):
            pytest.fail("must not acquire")
    assert resource.locked()
    resource.release()


@pytest.mark.asyncio
async def test_cancellation_releases_acquired_resource():
    resource = asyncio.Semaphore(1)
    entered = asyncio.Event()

    async def owned():
        async with bounded_resource(resource, 1):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owned())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not resource.locked()


@pytest.mark.asyncio
async def test_chrome_profile_wait_has_deadline(tmp_path):
    path = tmp_path / "chrome.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        executor = CodexChromeExecutor(
            executable=Path("/bin/true"),
            chrome_lock_path=path,
            resource_wait_seconds=0.01,
        )
        with pytest.raises(BrowserResourceWaitTimeout):
            async with executor._chrome_owner_lock():
                pytest.fail("another process owns the profile")
    finally:
        os.close(descriptor)


def test_receipt_updates_one_file_and_retains_stage_elapsed(tmp_path):
    attempt = ProviderAttemptDiagnostics(
        tmp_path,
        operation="search_suppliers",
        source="olx",
        item_index=2,
        model="gpt-6-luna",
    )
    attempt.stage("resource_wait")
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["status"] == "running"
    attempt.stage("browser_cli")
    attempt.finish(status="failed", error="cli_timeout")
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    receipt = json.loads(files[0].read_text())
    assert receipt["finished_at"] is not None
    assert receipt["stage_elapsed_seconds"]["resource_wait"] >= 0
    assert (
        safe_error_code(BrowserResourceWaitTimeout("arbitrary secret")) == "resource_wait_timeout"
    )
    assert safe_error_code(asyncio.CancelledError()) == "cancelled"
