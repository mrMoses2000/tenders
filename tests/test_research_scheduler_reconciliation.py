from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.queue import claim_job, complete_job
from procurement_bot.research import build_research_plan
from procurement_bot.research_repository import mark_research_running, persist_research_plan


class _NoCallBrowser:
    def __init__(self) -> None:
        self.calls = 0

    async def search_suppliers(self, plan):
        self.calls += 1
        raise AssertionError("scheduler reconciliation must not call a provider")


async def _research_pool(schema: str, *, item_count: int = 1):
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init_connection(connection: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
                format="text",
            )

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    user_id, case_id = uuid4(), uuid4()
    await pool.execute(
        "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
        user_id,
        920_000_000 + int(user_id.hex[:8], 16),
        "Research scheduler test",
    )
    await pool.execute(
        "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
        "VALUES($1,$2,$3,$4,'researching')",
        case_id,
        user_id,
        "Checkpoint reconciliation",
        "Алматы",
    )
    items = [
        ProcurementItem(name=f"Gauze roll {index}", quantity=10, unit="roll")
        for index in range(1, item_count + 1)
    ]
    for line_number, item in enumerate(items, start=1):
        await pool.execute(
            "INSERT INTO request_items(case_id,line_number,name,quantity,unit,status) "
            "VALUES($1,$2,$3,$4,$5,'ready')",
            case_id,
            line_number,
            item.name,
            item.quantity,
            item.unit,
        )
    async with pool.acquire() as connection:
        persisted = await persist_research_plan(
            connection,
            case_id=case_id,
            plan=build_research_plan(
                ParsedProcurementRequest(
                    city="Алматы", search_scope=SearchScope.CITYWIDE, items=items
                )
            ),
            requested_by=user_id,
        )
        await mark_research_running(connection, persisted.id)
    return admin, pool, persisted.id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_terminal_job_pointer_is_repaired_without_losing_checkpoint_state() -> None:
    schema = f"test_scheduler_repair_{uuid4().hex}"
    admin, pool, run_id = await _research_pool(schema)
    browser = _NoCallBrowser()
    try:
        executor = ParallelResearchRunExecutor(pool, browser)
        await executor.execute(run_id)
        checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE research_run_id=$1 "
            "AND current_job_id IS NOT NULL ORDER BY id LIMIT 1",
            run_id,
        )
        assert checkpoint is not None
        cursor_state = {"current": "opaque-cursor", "seen": ["opaque-cursor"]}
        receipts = [{"batch_no": 2, "external_id": "listing-hash-only"}]
        exclusions = ["listing-hash-only"]
        await pool.execute(
            """UPDATE jobs SET status='succeeded',locked_at=NULL,locked_by=NULL,
                lease_expires_at=NULL WHERE id=$1""",
            checkpoint["current_job_id"],
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='queued',owner_generation=7,
                batch_attempts=3,current_batch_no=4,search_cursor_state=$2::jsonb,
                candidate_receipts=$3::jsonb,excluded_external_ids=$4::jsonb,
                candidates_persisted=2,lease_until=NULL WHERE id=$1""",
            checkpoint["id"],
            cursor_state,
            receipts,
            exclusions,
        )

        assert await executor.reconcile_runnable_checkpoints(run_id) == 1

        repaired = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE id=$1", checkpoint["id"]
        )
        assert repaired["status"] == "queued"
        assert repaired["owner_generation"] == 8
        assert repaired["current_batch_no"] == 4
        assert repaired["batch_attempts"] == 3
        assert repaired["search_cursor_state"] == cursor_state
        assert repaired["candidate_receipts"] == receipts
        assert repaired["excluded_external_ids"] == exclusions
        assert repaired["candidates_persisted"] == 2
        assert repaired["current_job_id"] != checkpoint["current_job_id"]
        assert await pool.fetchval(
            "SELECT status FROM jobs WHERE id=$1", repaired["current_job_id"]
        ) == "pending"
        async with pool.acquire() as connection, connection.transaction():
            progress = await executor.load_research_progress(connection, run_id)
        assert progress["checkpoints_queued"] >= 1
        assert progress["live_jobs"] >= 1
        assert progress["wait_reason"] == "queue_job_active_or_scheduled"
        assert not progress["stalled"]
        assert browser.calls == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_target_met_queued_checkpoints_finish_without_provider_page() -> None:
    schema = f"test_scheduler_target_{uuid4().hex}"
    admin, pool, run_id = await _research_pool(schema)
    browser = _NoCallBrowser()
    try:
        executor = ParallelResearchRunExecutor(pool, browser)
        await executor.execute(run_id)
        checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE research_run_id=$1 "
            "AND current_job_id IS NOT NULL ORDER BY id LIMIT 1",
            run_id,
        )
        assert checkpoint is not None
        await pool.execute(
            "UPDATE research_item_groups SET eligible_suppliers=20 WHERE id=$1",
            checkpoint["group_id"],
        )
        await pool.execute(
            """UPDATE jobs SET status='succeeded',locked_at=NULL,locked_by=NULL,
                lease_expires_at=NULL WHERE id=$1""",
            checkpoint["current_job_id"],
        )
        await pool.execute(
            "UPDATE research_branch_checkpoints SET status='queued',lease_until=NULL WHERE id=$1",
            checkpoint["id"],
        )

        assert await executor.reconcile_runnable_checkpoints(run_id) >= 1

        statuses = await pool.fetch(
            "SELECT status,stop_reason FROM research_branch_checkpoints WHERE group_id=$1",
            checkpoint["group_id"],
        )
        assert all(row["status"] == "skipped" for row in statuses)
        assert all(row["stop_reason"] == "target_reached" for row in statuses)
        assert browser.calls == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_page_quantum_yields_to_waiting_item_group() -> None:
    schema = f"test_scheduler_fairness_{uuid4().hex}"
    admin, pool, run_id = await _research_pool(schema, item_count=4)
    browser = _NoCallBrowser()
    worker_id = "scheduler-fairness-test"
    try:
        executor = ParallelResearchRunExecutor(pool, browser)
        await executor.execute(run_id)
        queued_group = await pool.fetchrow(
            "SELECT id FROM research_item_groups WHERE research_run_id=$1 "
            "AND status='queued' ORDER BY created_at,id LIMIT 1",
            run_id,
        )
        assert queued_group is not None
        old_job = await claim_job(pool, worker_id=worker_id, lease_seconds=60)
        assert old_job is not None
        checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE id=$1",
            old_job.payload["task_id"],
        )
        generation = int(checkpoint["owner_generation"]) + 1
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='researching',
                owner_generation=$2,owner_worker=$3,lease_until=$4 WHERE id=$1""",
            checkpoint["id"],
            generation,
            worker_id,
            old_job.lease_expires_at,
        )
        await pool.execute(
            "UPDATE research_lanes SET status='researching',owner_worker=$2,lease_until=$3 "
            "WHERE id=$1",
            checkpoint["lane_id"],
            worker_id,
            old_job.lease_expires_at,
        )

        await executor._persist_batch_result(
            run_id,
            checkpoint["id"],
            old_job.id,
            worker_id,
            generation,
            checkpoint,
            candidates_persisted=0,
            own_database_candidates=0,
            pages_examined=1,
            external_ids=["next-page-card"],
            next_cursor="next-page-cursor",
            exhausted=False,
            stop_reason="",
        )

        assert await complete_job(pool, old_job.id, worker_id)
        group_states = await pool.fetch(
            "SELECT id,status,scheduler_last_served_at FROM research_item_groups "
            "WHERE research_run_id=$1 ORDER BY created_at,id",
            run_id,
        )
        assert sum(row["status"] == "researching" for row in group_states) == 3
        assert sum(row["status"] == "queued" for row in group_states) == 1
        assert next(row for row in group_states if row["id"] == queued_group["id"])[
            "status"
        ] == "researching"
        yielded_group = next(row for row in group_states if row["id"] == checkpoint["group_id"])
        assert yielded_group["status"] == "queued"
        assert yielded_group["scheduler_last_served_at"] is not None
        assert browser.calls == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
