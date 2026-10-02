from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from procurement_bot.cli import _BROWSER_HEAVY_JOB_KINDS, _excluded_job_kinds_for_worker
from procurement_bot.config import Settings
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.mini_app import control_research
from procurement_bot.providers.browser import StructuredBrowserProvider
from procurement_bot.queue import ClaimedJob
from procurement_bot.research import build_research_plan
from procurement_bot.research_execution import ResearchExecutionRejected, _request_hash
from procurement_bot.research_repository import recompute_plan_sha256
from procurement_bot.worker import JobWorker


def test_worker_pool_reserves_only_browser_cli_budget_for_heavy_jobs() -> None:
    default_budget = Settings(
        _env_file=None,
        worker_concurrency=4,
        browser_cli_concurrency=1,
        parallel_research_enabled=True,
    )
    assert [_excluded_job_kinds_for_worker(default_budget, index) for index in range(4)] == [
        (),
        _BROWSER_HEAVY_JOB_KINDS,
        _BROWSER_HEAVY_JOB_KINDS,
        _BROWSER_HEAVY_JOB_KINDS,
    ]

    two_slots = Settings(
        _env_file=None,
        worker_concurrency=4,
        browser_cli_concurrency=2,
        staged_research_enabled=True,
        staged_research_primary="codex",
    )
    assert [_excluded_job_kinds_for_worker(two_slots, index) for index in range(4)] == [
        (),
        (),
        _BROWSER_HEAVY_JOB_KINDS,
        _BROWSER_HEAVY_JOB_KINDS,
    ]


def test_single_worker_and_nonparallel_pools_keep_legacy_job_routing() -> None:
    single_worker = Settings(
        _env_file=None,
        worker_concurrency=1,
        browser_cli_concurrency=1,
        parallel_research_enabled=True,
    )
    assert _excluded_job_kinds_for_worker(single_worker, 0) == ()

    legacy_pool = Settings(
        _env_file=None,
        worker_concurrency=4,
        browser_cli_concurrency=1,
        parallel_research_enabled=False,
        staged_research_enabled=False,
    )
    assert [_excluded_job_kinds_for_worker(legacy_pool, index) for index in range(4)] == [
        (),
        (),
        (),
        (),
    ]


def test_runtime_cursor_preserves_approved_hash_but_changes_browser_hash() -> None:
    plan = build_research_plan(
        ParsedProcurementRequest(
            city="Алматы",
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(name="Валик", quantity=5, unit="шт")],
        )
    )
    branch = next(row for row in plan.source_plans if row.source.value == "2gis")
    original = plan.model_dump(mode="json")
    cursor = branch.model_copy(
        update={
            "batch_candidate_limit": 2,
            "excluded_external_ids": ["70000001234567"],
        }
    )
    plan.source_plans[plan.source_plans.index(branch)] = cursor
    assert plan.model_dump(mode="json") == original
    assert recompute_plan_sha256(plan) == plan.plan_sha256
    assert _request_hash(cursor) != _request_hash(branch)
    prepared = StructuredBrowserProvider(object()).prepare_supplier_search(cursor)
    assert prepared.request_sha256 == _request_hash(cursor)


@pytest.mark.asyncio
async def test_worker_routes_durable_child_with_lease_identity() -> None:
    captured = []

    class Executor:
        async def execute_task(self, payload, *, job_id, worker_id):
            captured.append((payload, job_id, worker_id))
            return None

    worker = object.__new__(JobWorker)
    worker.worker_id = "test:worker"
    worker.research_executor = Executor()
    payload = {"research_run_id": str(uuid4()), "task_id": str(uuid4())}
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload=payload,
        attempts=1,
        max_attempts=2,
        idempotency_key="parallel-test",
        locked_by=worker.worker_id,
        lease_expires_at=datetime.now(UTC),
    )
    await worker._dispatch(job)
    assert captured == [(payload, job.id, worker.worker_id)]


@pytest.mark.asyncio
async def test_worker_drops_result_fenced_by_operator_pause() -> None:
    class Executor:
        async def execute_task(self, payload, *, job_id, worker_id):
            raise ResearchExecutionRejected("research task lease or generation was lost")

    class Pool:
        async def fetchval(self, sql, *args):
            assert "status='paused'" in sql
            return True

    worker = object.__new__(JobWorker)
    worker.worker_id = "test:paused"
    worker.research_executor = Executor()
    worker.pool = Pool()
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload={"research_run_id": str(uuid4()), "task_id": str(uuid4())},
        attempts=1,
        max_attempts=2,
        idempotency_key="paused-test",
        locked_by=worker.worker_id,
        lease_expires_at=datetime.now(UTC),
    )
    await worker._dispatch(job)


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_research_control_requires_owned_active_run(monkeypatch, owned) -> None:
    from procurement_bot.parallel_research import ParallelResearchRunExecutor

    owner_id, case_id, run_id = uuid4(), uuid4(), uuid4()
    calls = []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield self

        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchval(self, sql, *args):
            assert "c.owner_user_id=$2" in sql and "run.status='running'" in sql
            assert args == (case_id, owner_id)
            return run_id if owned else None

        async def execute(self, sql, *args):
            assert "audit_log" in sql and "metadata" in sql
            calls.append("audit")

    class Request:
        app = {"pool": Pool(), "settings": SimpleNamespace(parallel_research_enabled=True)}
        match_info = {"case_id": str(case_id)}

        def __getitem__(self, key):
            assert key == "owner_id"
            return owner_id

        async def json(self):
            return {"action": "pause"}

    async def pause(self, actual_run):
        assert actual_run == run_id
        calls.append("pause")

    monkeypatch.setattr(ParallelResearchRunExecutor, "pause_run", pause)
    response = await control_research(Request())
    assert response.status == (200 if owned else 409)
    assert calls == (["pause", "audit"] if owned else [])


@pytest.mark.asyncio
async def test_research_control_retries_technical_failures_for_owner(monkeypatch) -> None:
    from procurement_bot.parallel_research import ParallelResearchRunExecutor

    owner_id, case_id, run_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    audited = []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield self

        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchval(self, sql, *args):
            assert "c.owner_user_id=$2" in sql
            assert args == (case_id, owner_id)
            return run_id

        async def execute(self, sql, *args):
            assert "audit_log" in sql
            audited.append(args)

    class Request:
        app = {"pool": Pool(), "settings": SimpleNamespace(parallel_research_enabled=True)}
        match_info = {"case_id": str(case_id)}

        def __getitem__(self, key):
            assert key == "owner_id"
            return owner_id

        async def json(self):
            return {"action": "retry_failed"}

    async def retry_failed(self, actual_run):
        assert actual_run == run_id
        return [task_id]

    monkeypatch.setattr(ParallelResearchRunExecutor, "retry_failed", retry_failed)
    response = await control_research(Request())
    assert response.status == 200
    assert audited[0][1] == "research_retry_failed"
    assert audited[0][2] == str(run_id)
    assert audited[0][3]["task_ids"] == [str(task_id)]
