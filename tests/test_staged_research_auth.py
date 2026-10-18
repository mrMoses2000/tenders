from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_autonomous_pipeline import pipeline_db  # noqa: F401
from test_staged_research import _Connection, _plan, _Pool, _SeparateStagePrimary

from procurement_bot.errors import ProviderAuthenticationDeferred
from procurement_bot.providers.staged_research import StagedResearchProvider
from procurement_bot.worker import JobWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["discover_page", "read_card", "read_contacts"])
async def test_auth_wait_does_not_consume_cards_or_contact_attempts_or_fallback(stage):
    plan = _plan()
    connection = _Connection(plan.execution_query_id)

    class AuthFailingPrimary(_SeparateStagePrimary):
        async def execute(self, call):
            current = json.loads(call.untrusted_request_json)["execution_stage"]
            if current == stage:
                raise ProviderAuthenticationDeferred(reason="provider_auth_failed")
            return await super().execute(call)

    class ForbiddenFallback:
        async def execute(self, _call):
            raise AssertionError("auth wait cannot invoke a fallback")

    primary = AuthFailingPrimary()
    provider = StagedResearchProvider(_Pool(connection), legacy=object(), primary=primary,
                                      fallback=ForbiddenFallback(), separate_contacts=True)
    for _ in range(3):
        with pytest.raises(ProviderAuthenticationDeferred) as caught:
            await provider.search_suppliers(plan)
        assert caught.value.reason == "provider_auth_failed"
        assert all(card["attempts"] == card["contact_attempts"] == 0
                   and card["status"] == "pending" for card in connection.cards.values())
    assert len(connection.pages) == (0 if stage == "discover_page" else 1)
    if stage == "read_contacts":
        assert primary.stages.count("read_card") == 1  # facts survive the wait


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_rejected_auth_preserves_real_job_and_fenced_checkpoint_attempts(
    pipeline_db, monkeypatch,  # noqa: F811
):
    pool, _case, _owner, run, executor = pipeline_db

    class AuthRejectedBrowser:
        async def search_suppliers(self, _plan):
            raise ProviderAuthenticationDeferred(reason="provider_auth_failed")

    executor.browser = AuthRejectedBrowser()

    async def dispatch(_worker, job):
        await executor.execute_task(job.payload, job_id=job.id, worker_id=job.locked_by)

    monkeypatch.setattr(JobWorker, "_dispatch", dispatch)
    worker = object.__new__(JobWorker)
    worker.pool, worker.worker_id, worker.lease_seconds = pool, "rejected-auth-test", 300
    task = None
    for _ in range(16):
        assert await worker.run_once()
        task = await pool.fetchrow(
            "SELECT cp.id,cp.current_job_id FROM research_branch_checkpoints cp "
            "JOIN jobs j ON j.id=cp.current_job_id WHERE cp.research_run_id=$1 "
            "AND j.last_error_code='provider_auth_failed' ORDER BY cp.id LIMIT 1", run,
        )
        if task:
            break
    assert task is not None
    job = await pool.fetchrow("SELECT * FROM jobs WHERE id=$1", task["current_job_id"])
    checkpoint = await pool.fetchrow(
        "SELECT * FROM research_branch_checkpoints WHERE id=$1", task["id"],
    )
    assert job["status"] == "pending" and job["attempts"] == 0
    assert job["last_error_code"] == checkpoint["last_error_code"] == "provider_auth_failed"
    assert job["available_at"] > datetime.now(UTC) + timedelta(minutes=29)
    assert checkpoint["status"] == "queued" and checkpoint["batch_attempts"] == 0
    assert checkpoint["owner_worker"] is None and checkpoint["lease_until"] is None
    assert await pool.fetchval("SELECT count(*) FROM research_provider_waits") == 0
