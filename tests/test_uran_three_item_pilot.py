from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.queue import ClaimedJob
from scripts.run_uran_three_item_pilot import (
    CASE_ID,
    PILOT_LINES,
    RUN_ID,
    PilotRefusal,
    _capture_baseline,
    _execute_claimed,
    _guard_scheduler_scope,
    _pause_selected,
    _retry_blocked_selected,
    _write_baseline,
    build_parser,
    claim_targeted_job,
    validate_group_partition,
    validate_hardcoded_target,
    validate_retry_blocked_options,
)


def test_pilot_scope_is_hardcoded_to_exact_case_and_run() -> None:
    validate_hardcoded_target(CASE_ID, RUN_ID)
    with pytest.raises(PilotRefusal, match="hardcoded case and run"):
        validate_hardcoded_target(uuid4(), RUN_ID)
    with pytest.raises(PilotRefusal, match="hardcoded case and run"):
        validate_hardcoded_target(CASE_ID, uuid4())


def test_group_partition_allows_terminal_pilot_groups_but_requires_other_52_paused() -> None:
    groups = [
        {
            "id": uuid4(),
            "line_number": line_number,
            "status": (
                "failed" if line_number == 6 else "partial" if line_number in {7, 10} else "paused"
            ),
        }
        for line_number in range(1, 56)
    ]

    assert set(validate_group_partition(groups)) == set(PILOT_LINES)

    groups[0]["status"] = "queued"
    with pytest.raises(PilotRefusal, match="52 non-pilot groups"):
        validate_group_partition(groups)


def test_retry_blocked_option_requires_bounded_selected_resume() -> None:
    parser = build_parser()
    snapshot = parser.parse_args(["--retry-blocked-selected"])
    with pytest.raises(PilotRefusal, match="requires --resume-selected"):
        validate_retry_blocked_options(
            snapshot.mode, snapshot.max_jobs, snapshot.retry_blocked_selected
        )

    prepare = parser.parse_args(
        ["--prepare", "--max-jobs", "1", "--retry-blocked-selected"]
    )
    with pytest.raises(PilotRefusal, match="requires --resume-selected"):
        validate_retry_blocked_options(
            prepare.mode, prepare.max_jobs, prepare.retry_blocked_selected
        )

    unbounded = parser.parse_args(["--resume-selected", "--retry-blocked-selected"])
    with pytest.raises(PilotRefusal, match="positive --max-jobs"):
        validate_retry_blocked_options(
            unbounded.mode, unbounded.max_jobs, unbounded.retry_blocked_selected
        )

    bounded = parser.parse_args(
        ["--resume-selected", "--max-jobs", "2", "--retry-blocked-selected"]
    )
    validate_retry_blocked_options(
        bounded.mode, bounded.max_jobs, bounded.retry_blocked_selected
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_targeted_claim_leaves_nonpilot_research_and_ordinary_jobs_pending(
    monkeypatch,
    tmp_path,
) -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    import scripts.run_uran_three_item_pilot as pilot

    schema = f"test_uran_pilot_claim_{uuid4().hex}"
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
        max_size=2,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    case_id, run_id = uuid4(), uuid4()
    monkeypatch.setattr(pilot, "CASE_ID", case_id)
    monkeypatch.setattr(pilot, "RUN_ID", run_id)
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        user_id = await pool.fetchval(
            "INSERT INTO users(telegram_id) VALUES($1) RETURNING id",
            1_000_000_000 + int(uuid4().hex[:8], 16),
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,status) VALUES($1,$2,'researching')",
            case_id,
            user_id,
        )
        await pool.execute(
            "INSERT INTO research_runs(id,case_id,status,idempotency_key) "
            "VALUES($1,$2,'running',$3)",
            run_id,
            case_id,
            f"pilot-run:{run_id}",
        )
        item_ids = [uuid4(), uuid4()]
        await pool.executemany(
            "INSERT INTO request_items(id,case_id,line_number,name) VALUES($1,$2,$3,$4)",
            [(item_ids[0], case_id, 6, "Pilot item"), (item_ids[1], case_id, 7, "Other item")],
        )
        remaining_items = [
            (uuid4(), case_id, line_number, f"Item {line_number}")
            for line_number in range(1, 56)
            if line_number not in {6, 7}
        ]
        await pool.executemany(
            "INSERT INTO request_items(id,case_id,line_number,name) VALUES($1,$2,$3,$4)",
            remaining_items,
        )
        query_ids = [uuid4(), uuid4()]
        await pool.executemany(
            """INSERT INTO research_queries(id,research_run_id,provider,query_text,idempotency_key)
               VALUES($1,$2,'own_database','pilot query',$3)""",
            [
                (query_ids[0], run_id, f"pilot-query:{query_ids[0]}"),
                (query_ids[1], run_id, f"pilot-query:{query_ids[1]}"),
            ],
        )
        group_ids = [uuid4(), uuid4()]
        lane_ids = [uuid4(), uuid4()]
        checkpoint_ids = [uuid4(), uuid4()]
        await pool.executemany(
            """INSERT INTO research_item_groups(id,research_run_id,request_item_id,status)
               VALUES($1,$2,$3,'researching')""",
            [(group_ids[0], run_id, item_ids[0]), (group_ids[1], run_id, item_ids[1])],
        )
        await pool.executemany(
            "INSERT INTO research_lanes(id,group_id,lane_no,status) VALUES($1,$2,0,'researching')",
            [(lane_ids[0], group_ids[0]), (lane_ids[1], group_ids[1])],
        )
        await pool.executemany(
            """INSERT INTO research_branch_checkpoints(
                   id,research_run_id,group_id,lane_id,query_id,lane_no,status
               ) VALUES($1,$2,$3,$4,$5,0,'queued')""",
            [
                (checkpoint_ids[0], run_id, group_ids[0], lane_ids[0], query_ids[0]),
                (checkpoint_ids[1], run_id, group_ids[1], lane_ids[1], query_ids[1]),
            ],
        )
        pilot_job_id, other_job_id, ordinary_job_id = uuid4(), uuid4(), uuid4()
        await pool.executemany(
            """INSERT INTO jobs(id,kind,payload,idempotency_key)
               VALUES($1,$2,$3::jsonb,$4)""",
            [
                (
                    pilot_job_id,
                    "parallel_research_task",
                    {"research_run_id": str(run_id), "task_id": str(checkpoint_ids[0])},
                    f"pilot-job:{pilot_job_id}",
                ),
                (
                    other_job_id,
                    "parallel_research_task",
                    {"research_run_id": str(run_id), "task_id": str(checkpoint_ids[1])},
                    f"other-job:{other_job_id}",
                ),
                (ordinary_job_id, "process_intake", {}, f"ordinary-job:{ordinary_job_id}"),
            ],
        )
        await pool.executemany(
            "UPDATE research_branch_checkpoints SET current_job_id=$2 WHERE id=$1",
            [(checkpoint_ids[0], pilot_job_id), (checkpoint_ids[1], other_job_id)],
        )

        with pytest.raises(PilotRefusal, match="runnable research job"):
            await _guard_scheduler_scope(pool, {6: group_ids[0]})

        # Once its job is no longer runnable, the active foreign group must
        # still block the pilot even though it has no queued child job.
        await pool.execute("UPDATE jobs SET status='dead' WHERE id=$1", other_job_id)
        with pytest.raises(PilotRefusal, match="active research group"):
            await _guard_scheduler_scope(pool, {6: group_ids[0]})

        # Paused out-of-scope groups (the other 52 pilot lines) are permitted.
        await pool.execute(
            "UPDATE research_item_groups SET status='paused' WHERE id=$1", group_ids[1]
        )
        await _guard_scheduler_scope(pool, {6: group_ids[0]})
        await pool.execute(
            "UPDATE research_item_groups SET status='researching' WHERE id=$1", group_ids[1]
        )
        await pool.execute("UPDATE jobs SET status='pending' WHERE id=$1", other_job_id)

        claimed = await claim_targeted_job(
            pool,
            case_id=case_id,
            run_id=run_id,
            group_ids=[group_ids[0]],
            worker_id="uran-pilot:test",
        )

        assert claimed is not None and claimed.id == pilot_job_id
        assert await pool.fetchval("SELECT status FROM jobs WHERE id=$1", other_job_id) == "pending"
        assert (
            await pool.fetchval("SELECT status FROM jobs WHERE id=$1", ordinary_job_id) == "pending"
        )

        baseline = await _capture_baseline(pool, {6: group_ids[0]})
        assert baseline["case_id"] == str(case_id)
        assert len(baseline["baseline_hash"]) == 64
        baseline_path = _write_baseline(baseline, tmp_path / "pilot-baseline.json")
        saved_baseline = baseline_path.read_text()
        assert '"nonpilot_state":' not in saved_baseline
        assert '"selected_receipts":' not in saved_baseline
        assert '"evidence_counts":' not in saved_baseline

        await pool.execute(
            """UPDATE research_branch_checkpoints
               SET search_cursor_state='{"current":"page-2","seen":["page-1"]}'::jsonb,
                   source_progress_token=$2
               WHERE id=$1""",
            checkpoint_ids[0],
            "a" * 64,
        )
        await pool.execute(
            "UPDATE jobs SET status='succeeded',completed_at=now() WHERE id=$1",
            pilot_job_id,
        )
        await _pause_selected(pool, {6: group_ids[0]})
        paused_checkpoint = await pool.fetchrow(
            """SELECT status,search_cursor_state,source_progress_token,current_job_id
               FROM research_branch_checkpoints WHERE id=$1""",
            checkpoint_ids[0],
        )
        assert paused_checkpoint["status"] == "paused"
        assert paused_checkpoint["search_cursor_state"] == {
            "current": "page-2",
            "seen": ["page-1"],
        }
        assert paused_checkpoint["source_progress_token"] == "a" * 64
        assert paused_checkpoint["current_job_id"] is None
        assert (
            await pool.fetchval("SELECT status FROM research_item_groups WHERE id=$1", group_ids[1])
            == "researching"
        )

        # The optional retry targets only matching provider-validation blocks
        # in the selected groups and preserves the resumable branch state.
        await pool.execute(
            """UPDATE research_branch_checkpoints
               SET status='blocked',stop_reason='provider_failure:ValidationError:bad result',
                   batch_attempts=3,current_batch_no=9,owner_generation=4,
                   owner_worker='stale-worker',lease_until=now()+interval '1 minute',
                   completed_at=now(),current_job_id=$2,last_error_code='validation_failure',
                   search_cursor_state='{"current":"keep-page","seen":["prior-page"]}'::jsonb,
                   excluded_external_ids='["keep-id"]'::jsonb,
                   candidate_receipts='[{"receipt":"keep"}]'::jsonb,
                   semantic_intent_ordinal=2
               WHERE id=$1""",
            checkpoint_ids[0],
            pilot_job_id,
        )
        await pool.execute(
            """UPDATE research_lanes SET status='blocked',generation=5,
                   owner_worker='stale-worker',lease_until=now()+interval '1 minute',
                   error_code='validation_failure' WHERE id=$1""",
            lane_ids[0],
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints
               SET status='blocked',
                   stop_reason='provider_failure:RetryableProviderError:cli_timeout',
                   batch_attempts=2,owner_generation=7
               WHERE id=$1""",
            checkpoint_ids[1],
        )
        await pool.execute(
            "UPDATE research_lanes SET status='blocked',generation=8,error_code='cli_timeout' "
            "WHERE id=$1",
            lane_ids[1],
        )
        outside_group_id, outside_lane_id, outside_query_id = uuid4(), uuid4(), uuid4()
        outside_checkpoint_id = uuid4()
        outside_item_id = await pool.fetchval(
            "SELECT id FROM request_items WHERE case_id=$1 AND line_number=8", case_id
        )
        await pool.execute(
            "INSERT INTO research_item_groups(id,research_run_id,request_item_id,status) "
            "VALUES($1,$2,$3,'paused')",
            outside_group_id,
            run_id,
            outside_item_id,
        )
        await pool.execute(
            "INSERT INTO research_lanes(id,group_id,lane_no,status) "
            "VALUES($1,$2,0,'blocked')",
            outside_lane_id,
            outside_group_id,
        )
        await pool.execute(
            "INSERT INTO research_queries(id,research_run_id,provider,query_text,idempotency_key) "
            "VALUES($1,$2,'own_database','outside query',$3)",
            outside_query_id,
            run_id,
            f"outside-query:{outside_query_id}",
        )
        await pool.execute(
            """INSERT INTO research_branch_checkpoints(
                   id,research_run_id,group_id,lane_id,query_id,lane_no,status,stop_reason
               ) VALUES($1,$2,$3,$4,$5,0,'blocked',
                   'provider_failure:ValidationError:outside selected scope')""",
            outside_checkpoint_id,
            run_id,
            outside_group_id,
            outside_lane_id,
            outside_query_id,
        )
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", pilot.SCHEDULER_LOCK_KEY)
            reset_count = await _retry_blocked_selected(
                connection, {"selected_group_ids": {6: group_ids[0], 7: group_ids[1]}}
            )
        assert reset_count == 1
        reset_checkpoint = await pool.fetchrow(
            """SELECT status,stop_reason,batch_attempts,current_batch_no,completed_at,
                      current_job_id,owner_generation,owner_worker,lease_until,last_error_code,
                      search_cursor_state,source_progress_token,excluded_external_ids,
                      candidate_receipts,semantic_intent_ordinal
               FROM research_branch_checkpoints WHERE id=$1""",
            checkpoint_ids[0],
        )
        assert reset_checkpoint["status"] == "paused"
        assert reset_checkpoint["stop_reason"] == ""
        assert reset_checkpoint["batch_attempts"] == 0
        assert reset_checkpoint["current_batch_no"] == 9
        assert reset_checkpoint["completed_at"] is None
        assert reset_checkpoint["current_job_id"] is None
        assert reset_checkpoint["owner_generation"] == 5
        assert reset_checkpoint["owner_worker"] is None
        assert reset_checkpoint["lease_until"] is None
        assert reset_checkpoint["last_error_code"] == "validation_failure"
        assert reset_checkpoint["search_cursor_state"] == {
            "current": "keep-page",
            "seen": ["prior-page"],
        }
        assert reset_checkpoint["source_progress_token"] == "a" * 64
        assert reset_checkpoint["excluded_external_ids"] == ["keep-id"]
        assert reset_checkpoint["candidate_receipts"] == [{"receipt": "keep"}]
        assert reset_checkpoint["semantic_intent_ordinal"] == 2
        reset_lane = await pool.fetchrow(
            "SELECT status,generation,error_code,owner_worker,lease_until "
            "FROM research_lanes WHERE id=$1",
            lane_ids[0],
        )
        assert dict(reset_lane) == {
            "status": "paused",
            "generation": 6,
            "error_code": "",
            "owner_worker": None,
            "lease_until": None,
        }
        untouched_checkpoints = await pool.fetch(
            "SELECT id,status,stop_reason,batch_attempts,owner_generation "
            "FROM research_branch_checkpoints WHERE id=ANY($1::uuid[])",
            [checkpoint_ids[1], outside_checkpoint_id],
        )
        assert {row["id"]: dict(row) for row in untouched_checkpoints} == {
            checkpoint_ids[1]: {
                "id": checkpoint_ids[1],
                "status": "blocked",
                "stop_reason": "provider_failure:RetryableProviderError:cli_timeout",
                "batch_attempts": 2,
                "owner_generation": 7,
            },
            outside_checkpoint_id: {
                "id": outside_checkpoint_id,
                "status": "blocked",
                "stop_reason": "provider_failure:ValidationError:outside selected scope",
                "batch_attempts": 0,
                "owner_generation": 0,
            },
        }
        untouched_lanes = await pool.fetch(
            "SELECT id,status,generation,error_code FROM research_lanes "
            "WHERE id=ANY($1::uuid[])",
            [lane_ids[1], outside_lane_id],
        )
        assert {row["id"]: (row["status"], row["generation"], row["error_code"])
                for row in untouched_lanes} == {
            lane_ids[1]: ("blocked", 8, "cli_timeout"),
            outside_lane_id: ("blocked", 0, ""),
        }
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_cancelled_claim_is_released_before_runner_pauses(monkeypatch) -> None:
    import scripts.run_uran_three_item_pilot as pilot

    started = asyncio.Event()
    released: list[tuple[ClaimedJob, str, bool]] = []

    class HangingExecutor:
        async def execute_task(self, payload, *, job_id, worker_id):
            started.set()
            await asyncio.Event().wait()

        async def renew_task_lease(self, task_id, job_id, worker_id):
            return True

    async def release_job(pool, job, error_code, *, retryable):
        released.append((job, error_code, retryable))

    monkeypatch.setattr(pilot, "fail_job", release_job)
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload={"task_id": str(uuid4())},
        attempts=1,
        max_attempts=3,
        idempotency_key="pilot-cancel-test",
        locked_by="pilot-test",
        lease_expires_at=None,
    )
    task = asyncio.create_task(
        _execute_claimed(
            object(),
            HangingExecutor(),
            job,
            "pilot-test",
            deadline_seconds=1200,
            lease_seconds=60,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert released == [(job, "pilot_cancelled", True)]
