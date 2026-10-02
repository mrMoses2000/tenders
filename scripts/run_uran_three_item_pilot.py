"""Run a tightly scoped, resumable three-item semantic research pilot.

The default invocation is a read-only snapshot. ``--prepare`` is required before
the first run and explicitly resets only the three allowlisted item groups.
``--retry-blocked-selected`` opts into resetting selected provider-validation
failures during a bounded resume.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.cli import _build_research_executor
from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.providers.browser import BrowserProviderError
from procurement_bot.queue import ClaimedJob, complete_job, fail_job, renew_job_lease

CASE_ID = UUID("889f7d8e-5b6d-4906-ac34-5074f9cbb924")
RUN_ID = UUID("9729892b-b8f0-474a-a75a-1844ce4b6d21")
PILOT_LINES = (6, 7, 10)
PILOT_LINE_SET = frozenset(PILOT_LINES)
HEAVY_JOB_KINDS = ("parallel_research_task", "enrich_2gis_contact", "resolve_locality")
SCHEDULER_LOCK_KEY = 731940251
PREPARED_REASON = "uran_three_item_pilot_prepared"
PAUSED_REASON = "uran_three_item_pilot_paused"
ACTIVE_REASON = "uran_three_item_pilot_active"
MAX_JOBS = 6
MAX_JOB_DEADLINE_SECONDS = 1_200


class PilotRefusal(RuntimeError):
    """A guard failed; the runner must make no pilot changes."""


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_hardcoded_target(case_id: UUID, run_id: UUID) -> None:
    if case_id != CASE_ID or run_id != RUN_ID:
        raise PilotRefusal("pilot runner is restricted to its hardcoded case and run")


def validate_group_partition(groups: list[dict[str, Any]]) -> dict[int, UUID]:
    """Require exactly the three pilot lines plus 52 paused untouched groups."""

    by_line = {int(group["line_number"]): group for group in groups}
    if len(groups) != 55 or len(by_line) != 55 or set(by_line) != set(range(1, 56)):
        raise PilotRefusal("expected exactly 55 distinct item groups on lines 1 through 55")
    if any(
        by_line[line]["status"] != "paused" for line in range(1, 56) if line not in PILOT_LINE_SET
    ):
        raise PilotRefusal("all 52 non-pilot groups must already be paused")
    return {line: by_line[line]["id"] for line in PILOT_LINES}


def _pilot_worker_id() -> str:
    return f"uran-pilot:{socket.gethostname()}:{os.getpid()}"


async def _read_target(connection: asyncpg.Connection) -> dict[str, Any]:
    run = await connection.fetchrow(
        """SELECT r.id,r.case_id,r.status,r.search_scope,c.status AS case_status
           FROM research_runs r JOIN procurement_cases c ON c.id=r.case_id
           WHERE r.id=$1""",
        RUN_ID,
    )
    if run is None:
        raise PilotRefusal("allowlisted research run was not found")
    validate_hardcoded_target(run["case_id"], run["id"])
    if run["status"] != "running" or run["case_status"] not in {
        "researching",
        "contacting",
    }:
        raise PilotRefusal("the allowlisted case and run must both be active")

    group_rows = await connection.fetch(
        """SELECT grp.id,grp.request_item_id,grp.status,grp.reason,i.line_number
           FROM research_item_groups grp
           JOIN request_items i ON i.id=grp.request_item_id
           WHERE grp.research_run_id=$1 ORDER BY i.line_number""",
        RUN_ID,
    )
    groups = [dict(row) for row in group_rows]
    selected = validate_group_partition(groups)
    selected_reasons = {
        group["reason"] for group in groups if group["line_number"] in PILOT_LINE_SET
    }
    return {
        "run": dict(run),
        "groups": groups,
        "selected_group_ids": selected,
        "selected_reasons": selected_reasons,
    }


async def _guard_scheduler_scope(
    connection: asyncpg.Connection, selected_group_ids: dict[int, UUID]
) -> None:
    running = await connection.fetchval(
        """SELECT count(*) FROM jobs WHERE status='running' AND kind=ANY($1::text[])""",
        list(HEAVY_JOB_KINDS),
    )
    if running:
        raise PilotRefusal("a browser-heavy job is already running")
    other_runnable_job = await connection.fetchval(
        """SELECT EXISTS(
               SELECT 1 FROM jobs j
               JOIN research_branch_checkpoints cp ON cp.current_job_id=j.id
               JOIN research_item_groups grp ON grp.id=cp.group_id
               JOIN research_runs run ON run.id=grp.research_run_id
               JOIN procurement_cases c ON c.id=run.case_id
               WHERE j.kind='parallel_research_task'
                 AND j.status IN ('pending','retry')
                 AND j.attempts<j.max_attempts AND j.available_at<=now()
                 AND cp.status IN ('queued','researching')
                 AND grp.status IN ('queued','researching')
                 AND run.status='running'
                 AND c.status IN ('researching','contacting')
                 AND grp.id<>ALL($1::uuid[])
           )""",
        list(selected_group_ids.values()),
    )
    if other_runnable_job:
        raise PilotRefusal("another runnable research job exists outside the pilot groups")
    other_active = await connection.fetchval(
        """SELECT EXISTS(
               SELECT 1 FROM research_item_groups grp
               JOIN research_runs run ON run.id=grp.research_run_id
               JOIN procurement_cases c ON c.id=run.case_id
               WHERE grp.status IN ('queued','researching') AND run.status='running'
                 AND c.status IN ('researching','contacting')
                 AND grp.id<>ALL($1::uuid[])
           )""",
        list(selected_group_ids.values()),
    )
    if other_active:
        raise PilotRefusal("another active research group exists outside the pilot groups")


async def _capture_baseline(
    connection: asyncpg.Connection, selected_group_ids: dict[int, UUID]
) -> dict[str, Any]:
    run = await connection.fetchrow("SELECT search_scope FROM research_runs WHERE id=$1", RUN_ID)
    item_rows = await connection.fetch(
        """SELECT i.id,i.line_number,i.name,i.specification_text,i.quantity,i.unit,
                  financial.tender_unit_price_private,financial.currency,
                  COALESCE((SELECT jsonb_agg(jsonb_build_object(
                      'requirement_key',req.requirement_key,'operator',req.operator,
                      'expected_value',req.expected_value,'is_hard',req.is_hard
                  ) ORDER BY req.requirement_key,req.operator,req.expected_value::text,req.is_hard)
                  FROM item_requirements req WHERE req.request_item_id=i.id),'[]'::jsonb)
                  AS requirements
           FROM request_items i
           LEFT JOIN request_item_financials financial ON financial.request_item_id=i.id
           WHERE i.case_id=$1 ORDER BY i.line_number""",
        CASE_ID,
    )
    item_lines = [int(row["line_number"]) for row in item_rows]
    if item_lines != list(range(1, 56)):
        raise PilotRefusal("the allowlisted case must still have the original 55 item lines")
    query_rows = await connection.fetch(
        """SELECT id,provider,query_text,city,search_area_text,latitude,longitude,
                  radius_meters,parameters,idempotency_key
           FROM research_queries WHERE research_run_id=$1 ORDER BY id""",
        RUN_ID,
    )
    group_rows = await connection.fetch(
        """SELECT grp.id,grp.request_item_id,grp.status,grp.reason,grp.target_suppliers,
                  grp.branches_total,grp.branches_succeeded,grp.branches_failed,
                  grp.branches_blocked,grp.candidates_persisted,grp.own_database_candidates,
                  grp.found_leads,grp.eligible_suppliers,grp.qualified_price_suppliers,
                  grp.updated_at,
                  COALESCE(jsonb_agg(jsonb_build_object(
                      'id',cp.id,'status',cp.status,'current_batch_no',cp.current_batch_no,
                      'batch_attempts',cp.batch_attempts,'owner_generation',cp.owner_generation,
                      'current_job_id',cp.current_job_id,'excluded_external_ids',
                          cp.excluded_external_ids,'candidate_receipts',cp.candidate_receipts,
                      'search_cursor_state',cp.search_cursor_state,'source_progress_token',
                          cp.source_progress_token,'semantic_intent_ordinal',
                          cp.semantic_intent_ordinal,'updated_at',cp.updated_at
                  ) ORDER BY cp.lane_no,cp.query_id) FILTER (WHERE cp.id IS NOT NULL),'[]'::jsonb)
                  AS checkpoints,
                  COALESCE((SELECT jsonb_agg(jsonb_build_object(
                      'id',lane.id,'status',lane.status,'generation',lane.generation,
                      'owner_worker',lane.owner_worker,'lease_until',lane.lease_until,
                      'error_code',lane.error_code,'updated_at',lane.updated_at
                  ) ORDER BY lane.lane_no) FROM research_lanes lane
                    WHERE lane.group_id=grp.id),'[]'::jsonb) AS lanes
           FROM research_item_groups grp
           LEFT JOIN research_branch_checkpoints cp ON cp.group_id=grp.id
           WHERE grp.research_run_id=$1 GROUP BY grp.id ORDER BY grp.id""",
        RUN_ID,
    )
    immutable_items = [dict(row) for row in item_rows]
    immutable_queries = [dict(row) for row in query_rows]
    nonpilot = [
        dict(row) for row in group_rows if row["id"] not in set(selected_group_ids.values())
    ]
    selected_receipts = {
        str(row["id"]): [
            {
                "excluded_external_ids": checkpoint["excluded_external_ids"],
                "candidate_receipts": checkpoint["candidate_receipts"],
            }
            for checkpoint in row["checkpoints"]
        ]
        for row in group_rows
        if row["id"] in set(selected_group_ids.values())
    }
    query_ids = [row["id"] for row in query_rows]
    evidence_counts = await connection.fetch(
        """SELECT research_query_id,count(*)::int AS count
           FROM source_evidence WHERE research_query_id=ANY($1::uuid[])
           GROUP BY research_query_id""",
        query_ids,
    )
    evidence_by_query = {str(row["research_query_id"]): row["count"] for row in evidence_counts}
    static_hash = _canonical_hash(
        {
            "run_plan_sha256": run["search_scope"].get("plan_sha256"),
            "items": immutable_items,
            "queries": immutable_queries,
        }
    )
    return {
        "case_id": str(CASE_ID),
        "run_id": str(RUN_ID),
        "plan_sha256": run["search_scope"].get("plan_sha256"),
        "baseline_hash": static_hash,
        "nonpilot_state_hash": _canonical_hash(nonpilot),
        "nonpilot_state": nonpilot,
        "selected_receipts": selected_receipts,
        "evidence_counts": evidence_by_query,
    }


def _write_baseline(baseline: dict[str, Any], destination: Path | None = None) -> Path:
    if destination is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = Path("outputs") / f"uran-three-item-pilot-{stamp}-{uuid4().hex[:8]}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PilotRefusal("baseline path already exists; choose a new path")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    safe_baseline = {
        key: value
        for key, value in baseline.items()
        if key not in {"nonpilot_state", "selected_receipts", "evidence_counts"}
    }
    temporary.write_text(json.dumps(safe_baseline, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(destination)
    return destination


async def _preflight(
    connection: asyncpg.Connection, mode: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = await _read_target(connection)
    await _guard_scheduler_scope(connection, target["selected_group_ids"])
    reasons = target["selected_reasons"]
    selected_statuses = {
        group["status"] for group in target["groups"] if group["line_number"] in PILOT_LINE_SET
    }
    if mode == "prepare":
        if not selected_statuses.issubset({"paused", "partial", "failed"}):
            raise PilotRefusal("pilot groups must be paused or terminal before prepare")
        if reasons & {PREPARED_REASON, PAUSED_REASON, ACTIVE_REASON}:
            raise PilotRefusal("pilot is already prepared; use --resume-selected")
    if mode == "resume":
        if selected_statuses != {"paused"}:
            raise PilotRefusal("all pilot groups must be paused before resume")
        if not reasons.issubset({PREPARED_REASON, PAUSED_REASON}):
            raise PilotRefusal("pilot groups were not prepared by this runner")
    baseline = await _capture_baseline(connection, target["selected_group_ids"])
    return target, baseline


async def _retry_blocked_selected(
    connection: asyncpg.Connection, target: dict[str, Any]
) -> int:
    """Requeue only selected branches blocked by provider result validation."""

    selected_ids = list(target["selected_group_ids"].values())
    matched = await connection.fetchval(
        """SELECT count(*) FROM research_branch_checkpoints
           WHERE group_id=ANY($1::uuid[]) AND status='blocked'
             AND stop_reason LIKE 'provider_failure:ValidationError:%'""",
        selected_ids,
    )
    if not matched:
        return 0
    await connection.execute(
        """UPDATE research_lanes SET status='paused',generation=generation+1,
               owner_worker=NULL,lease_until=NULL,error_code='',updated_at=now()
           WHERE status='blocked' AND id IN (
               SELECT lane_id FROM research_branch_checkpoints
               WHERE group_id=ANY($1::uuid[]) AND status='blocked'
                 AND stop_reason LIKE 'provider_failure:ValidationError:%'
           )""",
        selected_ids,
    )
    await connection.execute(
        """UPDATE research_branch_checkpoints SET status='paused',stop_reason='',
               batch_attempts=0,completed_at=NULL,current_job_id=NULL,
               owner_generation=owner_generation+1,owner_worker=NULL,lease_until=NULL,
               updated_at=now()
           WHERE group_id=ANY($1::uuid[]) AND status='blocked'
             AND stop_reason LIKE 'provider_failure:ValidationError:%'""",
        selected_ids,
    )
    return int(matched)


def validate_retry_blocked_options(mode: str | None, max_jobs: int, enabled: bool) -> None:
    if not enabled:
        return
    if mode != "resume":
        raise PilotRefusal("--retry-blocked-selected requires --resume-selected")
    if max_jobs <= 0:
        raise PilotRefusal("--retry-blocked-selected requires a positive --max-jobs bound")


async def _prepare_selected(connection: asyncpg.Connection, target: dict[str, Any]) -> None:
    selected_ids = list(target["selected_group_ids"].values())
    await connection.execute(
        """UPDATE jobs SET status='dead',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
               last_error_code='pilot_reset_superseded',completed_at=now(),updated_at=now()
           WHERE id IN (SELECT current_job_id FROM research_branch_checkpoints
                        WHERE group_id=ANY($1::uuid[]) AND current_job_id IS NOT NULL)
             AND status IN ('pending','retry')""",
        selected_ids,
    )
    await connection.execute(
        """UPDATE research_item_groups SET status='paused',reason=$2,
               completed_at=NULL,updated_at=now() WHERE id=ANY($1::uuid[])""",
        selected_ids,
        PREPARED_REASON,
    )
    await connection.execute(
        """UPDATE research_lanes SET status='paused',generation=generation+1,
               owner_worker=NULL,lease_until=NULL,error_code='',updated_at=now()
           WHERE group_id=ANY($1::uuid[])""",
        selected_ids,
    )
    await connection.execute(
        """UPDATE research_branch_checkpoints SET status='queued',
               current_batch_no=0,batch_attempts=0,owner_generation=owner_generation+1,
               owner_worker=NULL,lease_until=NULL,current_job_id=NULL,last_error_code='',
               stop_reason='',pages_examined=0,search_cursor_state='{"current":null,"seen":[]}'::jsonb,
               source_progress_token=NULL,semantic_intent_ordinal=0,completed_at=NULL,
               updated_at=now()
           WHERE group_id=ANY($1::uuid[])""",
        selected_ids,
    )


async def _activate_selected(
    connection: asyncpg.Connection,
    target: dict[str, Any],
    executor: ParallelResearchRunExecutor,
    *,
    resuming: bool,
) -> None:
    selected_ids = list(target["selected_group_ids"].values())
    await connection.execute(
        """UPDATE research_item_groups SET status='researching',reason=$2,
               completed_at=NULL,updated_at=now() WHERE id=ANY($1::uuid[])""",
        selected_ids,
        ACTIVE_REASON,
    )
    await connection.execute(
        """UPDATE research_lanes SET status='queued',owner_worker=NULL,lease_until=NULL,
               updated_at=now() WHERE group_id=ANY($1::uuid[]) AND status='paused'""",
        selected_ids,
    )
    if resuming:
        await connection.execute(
            """UPDATE research_branch_checkpoints SET status='queued',stop_reason='',
                   completed_at=NULL,owner_generation=owner_generation+1,
                   owner_worker=NULL,lease_until=NULL,current_job_id=NULL,updated_at=now()
               WHERE group_id=ANY($1::uuid[]) AND status='paused'""",
            selected_ids,
        )
    for group_id in selected_ids:
        task = await connection.fetchrow(
            """SELECT id,lane_id,lane_no,current_batch_no,owner_generation,last_error_code
               FROM research_branch_checkpoints
               WHERE group_id=$1 AND status='queued' AND current_job_id IS NULL
               ORDER BY lane_no,query_id LIMIT 1 FOR UPDATE""",
            group_id,
        )
        if task is not None:
            group = await connection.fetchrow(
                "SELECT research_run_id FROM research_item_groups WHERE id=$1", group_id
            )
            await executor._enqueue_batch(connection, group["research_run_id"], task)


async def claim_targeted_job(
    pool: asyncpg.Pool,
    *,
    case_id: UUID,
    run_id: UUID,
    group_ids: list[UUID],
    worker_id: str,
    lease_seconds: int = 300,
) -> ClaimedJob | None:
    """Claim one due child job from the hard-scoped pilot groups only."""

    validate_hardcoded_target(case_id, run_id)
    if not group_ids:
        return None
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEDULER_LOCK_KEY)
        row = await connection.fetchrow(
            """WITH candidate AS (
                   SELECT j.id
                   FROM jobs j
                   JOIN research_branch_checkpoints cp ON cp.current_job_id=j.id
                   JOIN research_item_groups grp ON grp.id=cp.group_id
                   JOIN research_runs run ON run.id=grp.research_run_id
                   JOIN procurement_cases c ON c.id=run.case_id
                   JOIN request_items i ON i.id=grp.request_item_id
                   WHERE j.kind='parallel_research_task'
                     AND j.status IN ('pending','retry') AND j.attempts<j.max_attempts
                     AND j.available_at<=now()
                     AND cp.status='queued' AND grp.status='researching'
                     AND run.id=$1 AND run.case_id=$2 AND run.status='running'
                     AND c.id=$2 AND c.status IN ('researching','contacting')
                     AND grp.id=ANY($3::uuid[]) AND i.line_number=ANY($4::int[])
                   ORDER BY j.available_at,i.line_number,j.created_at,j.id
                   FOR UPDATE OF j SKIP LOCKED LIMIT 1
               )
               UPDATE jobs j SET status='running',attempts=j.attempts+1,
                   locked_at=now(),locked_by=$5,
                   lease_expires_at=now()+make_interval(secs => $6),updated_at=now()
               FROM candidate WHERE j.id=candidate.id RETURNING j.*""",
            run_id,
            case_id,
            group_ids,
            list(PILOT_LINES),
            worker_id,
            lease_seconds,
        )
    if row is None:
        return None
    return ClaimedJob(
        id=row["id"],
        kind=row["kind"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
        locked_by=row["locked_by"],
        lease_expires_at=row["lease_expires_at"],
    )


async def _heartbeat(
    pool: asyncpg.Pool,
    executor: ParallelResearchRunExecutor,
    job: ClaimedJob,
    worker_id: str,
    lease_seconds: int,
) -> None:
    interval = max(1, min(5, lease_seconds // 3))
    task_id = UUID(str(job.payload["task_id"]))
    while True:
        await asyncio.sleep(interval)
        if not await renew_job_lease(pool, job.id, worker_id, lease_seconds=lease_seconds):
            raise RetryableProviderError("pilot job lease was lost")
        if not await executor.renew_task_lease(task_id, job.id, worker_id):
            raise RetryableProviderError("pilot checkpoint lease was lost")


async def _execute_claimed(
    pool: asyncpg.Pool,
    executor: ParallelResearchRunExecutor,
    job: ClaimedJob,
    worker_id: str,
    *,
    deadline_seconds: int,
    lease_seconds: int,
) -> None:
    dispatch = asyncio.create_task(
        executor.execute_task(job.payload, job_id=job.id, worker_id=worker_id)
    )
    heartbeat = asyncio.create_task(_heartbeat(pool, executor, job, worker_id, lease_seconds))
    try:
        done, _ = await asyncio.wait(
            {dispatch, heartbeat},
            timeout=deadline_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("pilot job deadline exceeded")
        if heartbeat in done:
            await heartbeat
        await dispatch
        if not await complete_job(pool, job.id, worker_id):
            raise RetryableProviderError("pilot job lease was lost before completion")
    except asyncio.CancelledError:
        dispatch.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(dispatch, timeout=10)
        with suppress(Exception):
            await fail_job(pool, job, "pilot_cancelled", retryable=True)
        raise
    except Exception as exc:
        if not dispatch.done():
            dispatch.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(dispatch, timeout=10)
        retryable = not isinstance(exc, (PermanentProviderError, ValueError, KeyError))
        if isinstance(exc, BrowserProviderError):
            retryable = True
        await fail_job(pool, job, type(exc).__name__, retryable=retryable)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await heartbeat


async def _pause_selected(pool: asyncpg.Pool, selected_group_ids: dict[int, UUID]) -> None:
    group_ids = list(selected_group_ids.values())
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEDULER_LOCK_KEY)
        running = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM jobs j
                 JOIN research_branch_checkpoints cp ON cp.current_job_id=j.id
                 WHERE cp.group_id=ANY($1::uuid[]) AND j.status='running')""",
            group_ids,
        )
        if running:
            raise PilotRefusal("a pilot browser job is still running; refusing to pause unsafely")
        await connection.execute(
            """UPDATE jobs SET status='dead',locked_at=NULL,locked_by=NULL,
                   lease_expires_at=NULL,last_error_code='uran_pilot_paused',
                   completed_at=now(),updated_at=now()
               WHERE id IN (SELECT current_job_id FROM research_branch_checkpoints
                            WHERE group_id=ANY($1::uuid[]) AND current_job_id IS NOT NULL)
                 AND status IN ('pending','retry')""",
            group_ids,
        )
        await connection.execute(
            """UPDATE research_item_groups SET status='paused',reason=$2,
                   updated_at=now() WHERE id=ANY($1::uuid[])""",
            group_ids,
            PAUSED_REASON,
        )
        await connection.execute(
            """UPDATE research_lanes SET status='paused',owner_worker=NULL,lease_until=NULL,
                   updated_at=now() WHERE group_id=ANY($1::uuid[])
                   AND status IN ('queued','researching')""",
            group_ids,
        )
        await connection.execute(
            """UPDATE research_branch_checkpoints SET status='paused',
                   owner_generation=owner_generation+1,owner_worker=NULL,lease_until=NULL,
                   current_job_id=NULL,updated_at=now()
               WHERE group_id=ANY($1::uuid[]) AND status IN ('queued','researching')""",
            group_ids,
        )


async def _verify_after(
    connection: asyncpg.Connection,
    before: dict[str, Any],
    selected_group_ids: dict[int, UUID],
) -> dict[str, Any]:
    after = await _capture_baseline(connection, selected_group_ids)
    if after["baseline_hash"] != before["baseline_hash"]:
        raise PilotRefusal("frozen 55-item/run/query baseline changed during pilot")
    if after["nonpilot_state_hash"] != before["nonpilot_state_hash"]:
        raise PilotRefusal("a non-pilot group or checkpoint changed during pilot")
    for group_id, original in before["selected_receipts"].items():
        current = after["selected_receipts"].get(group_id, [])
        if len(current) != len(original):
            raise PilotRefusal("pilot checkpoint set changed during run")
        for old, new in zip(original, current, strict=True):
            previous_ids = old["excluded_external_ids"]
            current_ids = new["excluded_external_ids"]
            if current_ids[: len(previous_ids)] != previous_ids:
                raise PilotRefusal("existing candidate exclusions were changed")
            old_receipts = old["candidate_receipts"]
            if new["candidate_receipts"][: len(old_receipts)] != old_receipts:
                raise PilotRefusal("existing candidate receipts were removed or rewritten")
    for query_id, old_count in before["evidence_counts"].items():
        if after["evidence_counts"].get(query_id, 0) < old_count:
            raise PilotRefusal("existing source evidence was removed")
    return after


async def _snapshot_only(pool: asyncpg.Pool) -> dict[str, Any]:
    async with pool.acquire() as connection:
        target = await _read_target(connection)
        baseline = await _capture_baseline(connection, target["selected_group_ids"])
        return {
            "mode": "read_only_snapshot",
            "case_id": str(CASE_ID),
            "run_id": str(RUN_ID),
            "pilot_lines": list(PILOT_LINES),
            "group_statuses": {
                str(group["line_number"]): group["status"] for group in target["groups"]
            },
            "baseline_hash": baseline["baseline_hash"],
            "nonpilot_state_hash": baseline["nonpilot_state_hash"],
        }


async def run_pilot(args: argparse.Namespace) -> dict[str, Any]:
    validate_retry_blocked_options(
        args.mode, args.max_jobs, getattr(args, "retry_blocked_selected", False)
    )
    settings = get_settings()
    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=4)
    executor: ParallelResearchRunExecutor | None = None
    try:
        if args.mode is None:
            if args.max_jobs:
                raise PilotRefusal("select --prepare or --resume-selected before running jobs")
            return await _snapshot_only(pool)
        if args.mode == "resume" and args.max_jobs == 0:
            raise PilotRefusal("--resume-selected requires a positive --max-jobs bound")
        working_settings = settings.model_copy(
            update={
                "browser_research_enabled": True,
                "parallel_research_enabled": True,
                "staged_research_enabled": True,
                "research_auto_prepare_outreach": False,
            }
        )
        if args.max_jobs:
            executor = _build_research_executor(working_settings, pool)
            if not isinstance(executor, ParallelResearchRunExecutor):
                raise PilotRefusal("parallel staged research executor is unavailable")

        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEDULER_LOCK_KEY)
            target, baseline = await _preflight(connection, args.mode)
            baseline_path = _write_baseline(baseline, args.baseline_path)
            retried_blocked_branches = 0
            if args.mode == "prepare":
                await _prepare_selected(connection, target)
            elif getattr(args, "retry_blocked_selected", False):
                retried_blocked_branches = await _retry_blocked_selected(connection, target)
            if args.max_jobs:
                assert executor is not None
                await _activate_selected(
                    connection, target, executor, resuming=args.mode == "resume"
                )

        processed = 0
        worker_id = _pilot_worker_id()
        if args.max_jobs:
            assert executor is not None
            try:
                while processed < args.max_jobs:
                    job = await claim_targeted_job(
                        pool,
                        case_id=CASE_ID,
                        run_id=RUN_ID,
                        group_ids=list(target["selected_group_ids"].values()),
                        worker_id=worker_id,
                        lease_seconds=working_settings.job_lease_seconds,
                    )
                    if job is None:
                        break
                    await _execute_claimed(
                        pool,
                        executor,
                        job,
                        worker_id,
                        deadline_seconds=args.job_deadline,
                        lease_seconds=working_settings.job_lease_seconds,
                    )
                    await executor.reconcile(RUN_ID)
                    processed += 1
            finally:
                await _pause_selected(pool, target["selected_group_ids"])

        async with pool.acquire() as connection:
            after = await _verify_after(connection, baseline, target["selected_group_ids"])
            group_rows = await connection.fetch(
                """SELECT i.line_number,grp.status,grp.reason
                   FROM research_item_groups grp JOIN request_items i
                     ON i.id=grp.request_item_id
                   WHERE grp.id=ANY($1::uuid[]) ORDER BY i.line_number""",
                list(target["selected_group_ids"].values()),
            )
        return {
            "mode": args.mode,
            "case_id": str(CASE_ID),
            "run_id": str(RUN_ID),
            "pilot_lines": list(PILOT_LINES),
            "processed_jobs": processed,
            "retried_blocked_branches": retried_blocked_branches,
            "baseline_path": str(baseline_path),
            "baseline_hash_before": baseline["baseline_hash"],
            "baseline_hash_after": after["baseline_hash"],
            "nonpilot_state_unchanged": True,
            "pilot_groups": [dict(row) for row in group_rows],
        }
    finally:
        await pool.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare", dest="mode", action="store_const", const="prepare")
    modes.add_argument("--resume-selected", dest="mode", action="store_const", const="resume")
    parser.add_argument(
        "--retry-blocked-selected",
        action="store_true",
        help=(
            "on bounded --resume-selected, retry only selected branches blocked by "
            "provider ValidationError"
        ),
    )
    parser.add_argument("--max-jobs", type=int, default=0, choices=range(0, MAX_JOBS + 1))
    parser.add_argument(
        "--job-deadline",
        type=int,
        default=MAX_JOB_DEADLINE_SECONDS,
        choices=range(30, MAX_JOB_DEADLINE_SECONDS + 1),
    )
    parser.add_argument("--baseline-path", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(run_pilot(args))
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
