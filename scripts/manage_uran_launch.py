"""Fail-closed preflight and native pause/resume controls for the Uran Energo run."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.approvals import payload_sha256
from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.parallel_research import (
    _RETRYABLE_TECHNICAL_FAILURE_PREFIXES,
    ParallelResearchRunExecutor,
)
from procurement_bot.queue import stable_key
from procurement_bot.research import LocalityConstraint
from procurement_bot.research_scope import load_effective_scope
from procurement_bot.research_workflow import (
    ResearchStartDenied,
    _parse_start_payload,
    _resolve_idempotent_start,
)
from procurement_bot.search_profiles import (
    PROFILE_MODEL_VERSION,
    PROFILE_PROMPT_VERSION,
    _authorized_scope_revision,
    _effective_prompt_version,
    load_search_item_snapshot,
)
from scripts.run_uran_three_item_pilot import (
    CASE_ID,
    RUN_ID,
    SCHEDULER_LOCK_KEY,
    _canonical_hash,
    _capture_baseline,
)

EXPECTED_BASELINE_SHA256 = "eb650a33901b338c8229a284ffa63dd07d86c9f913476b0872ac2644f9848003"
PREFERRED_AREA = "Северное Кольцо"
MAX_ACTIVE_GROUPS = 3
MAX_BROWSER_CLI_CONCURRENCY = 1
CITYWIDE_MAX_BATCHES = 80


class LaunchRefusal(RuntimeError):
    """A launch safety guard failed; no lifecycle change should be made."""


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LaunchRefusal(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise LaunchRefusal(f"{field} is not an object")
    return value


def validate_identity(case_id: UUID, run_id: UUID) -> None:
    if case_id != CASE_ID or run_id != RUN_ID:
        raise LaunchRefusal("launch utility is restricted to its hardcoded case and run")


def validate_groups(groups: list[dict[str, Any]], *, require_paused: bool) -> dict[int, UUID]:
    by_line = {int(group["line_number"]): group for group in groups}
    if len(groups) != 55 or len(by_line) != 55 or set(by_line) != set(range(1, 56)):
        raise LaunchRefusal("expected exactly 55 distinct item groups on lines 1 through 55")
    terminal_states = {"succeeded", "partial", "failed"}
    nonterminal = [group for group in groups if group["status"] not in terminal_states]
    if require_paused:
        if not nonterminal:
            raise LaunchRefusal("all 55 groups are terminal; nothing remains to resume")
        if any(group["status"] != "paused" for group in nonterminal):
            raise LaunchRefusal("all nonterminal groups must be paused before resume")
    valid_pause_states = {"paused", "queued", "researching", *terminal_states}
    if not require_paused and any(group["status"] not in valid_pause_states for group in groups):
        raise LaunchRefusal("pause accepts only active, paused, or terminal groups")
    return {line: by_line[line]["id"] for line in range(1, 56)}


def validate_retry_recovery(run: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    if run["status"] != "partial" or run["case_status"] != "report_ready":
        raise LaunchRefusal(
            "failed-checkpoint recovery requires this partial run and report-ready case"
        )
    if any(group["status"] not in {"succeeded", "partial", "failed"} for group in groups):
        raise LaunchRefusal("failed-checkpoint recovery requires all item groups terminal")


def validate_worker_limits(settings: Any) -> None:
    if int(settings.research_active_groups) > MAX_ACTIVE_GROUPS:
        raise LaunchRefusal("research_active_groups must be at most 3")
    if int(settings.browser_cli_concurrency) != MAX_BROWSER_CLI_CONCURRENCY:
        raise LaunchRefusal("browser_cli_concurrency must equal 1")
    required = {
        "staged_research_enabled": True,
        "staged_research_primary": "codex",
        "semantic_supplier_reply_enabled": True,
        "research_auto_prepare_outreach": True,
        "waha_enabled": True,
    }
    for name, expected in required.items():
        if getattr(settings, name, None) != expected:
            raise LaunchRefusal(f"{name} must be set to {expected!r} for the approved run path")
    if int(settings.research_max_batches) < CITYWIDE_MAX_BATCHES:
        raise LaunchRefusal("research_max_batches must be at least 80")


async def _read_target(connection: asyncpg.Connection) -> dict[str, Any]:
    run = await connection.fetchrow(
        """SELECT run.id,run.case_id,run.status,run.search_scope,
                  case_row.status AS case_status,case_row.owner_user_id,case_row.city
           FROM research_runs run JOIN procurement_cases case_row ON case_row.id=run.case_id
           WHERE run.id=$1""",
        RUN_ID,
    )
    if run is None:
        raise LaunchRefusal("allowlisted research run was not found")
    validate_identity(run["case_id"], run["id"])
    valid_run_statuses = {"running", "succeeded", "partial", "failed", "cancelled"}
    valid_case_statuses = {
        "researching",
        "contacting",
        "evaluating",
        "report_ready",
        "selected",
        "closed",
        "cancelled",
    }
    if run["status"] not in valid_run_statuses or run["case_status"] not in valid_case_statuses:
        raise LaunchRefusal("the allowlisted case or run is not in a resumable lifecycle state")
    group_rows = await connection.fetch(
        """SELECT grp.id,grp.request_item_id,grp.status,grp.reason,item.line_number
           FROM research_item_groups grp JOIN request_items item
             ON item.id=grp.request_item_id
           WHERE grp.research_run_id=$1 ORDER BY item.line_number""",
        RUN_ID,
    )
    groups = [dict(row) for row in group_rows]
    group_ids = validate_groups(groups, require_paused=False)
    envelope = _json_object(run["search_scope"], "frozen run scope")
    plan = _json_object(envelope.get("plan"), "frozen plan")
    if envelope.get("contract_version") != 1 or set(envelope) != {
        "contract_version",
        "plan_sha256",
        "plan",
    }:
        raise LaunchRefusal("frozen plan envelope has an unknown contract")
    calculated = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if envelope.get("plan_sha256") != calculated:
        raise LaunchRefusal("frozen plan hash does not match its payload")
    return {
        "run": dict(run),
        "is_active": run["status"] == "running"
        and run["case_status"] in {"researching", "contacting"},
        "groups": groups,
        "group_ids": group_ids,
        "plan": plan,
        "plan_sha256": calculated,
    }


def _validate_consumed_approval(row: Any, target: dict[str, Any]) -> Any:
    if (
        row["action_type"] != "start_research"
        or row["status"] != "consumed"
        or row["approved_by"] is None
        or row["consumed_at"] is None
        or not row["consumed_when_live"]
    ):
        raise LaunchRefusal("original start_research approval was not validly consumed")
    try:
        payload = _parse_start_payload(row["payload"])
    except ResearchStartDenied as exc:
        raise LaunchRefusal("original start_research approval payload is invalid") from exc
    if (
        payload.case_id != CASE_ID
        or payload.research_run_id != RUN_ID
        or payload.plan_sha256 != target["plan_sha256"]
        or row["case_id"] != CASE_ID
        or row["payload_sha256"] != payload_sha256(payload.canonical)
    ):
        raise LaunchRefusal("consumed approval does not match this case, run, and frozen plan")
    return payload


async def _verify_consumed_start_approval(
    connection: asyncpg.Connection, target: dict[str, Any]
) -> dict[str, Any]:
    rows = await connection.fetch(
        """SELECT id,case_id,action_type,payload,payload_sha256,status,approved_by,
                  consumed_at,(expires_at IS NULL OR consumed_at<expires_at)
                      AS consumed_when_live
           FROM action_approvals
           WHERE case_id=$1 AND action_type='start_research' AND status='consumed'
             AND payload->>'research_run_id'=$2
           ORDER BY consumed_at,id""",
        CASE_ID,
        str(RUN_ID),
    )
    if len(rows) != 1:
        raise LaunchRefusal("expected exactly one consumed start_research approval for this run")
    row = rows[0]
    payload = _validate_consumed_approval(row, target)
    try:
        await _resolve_idempotent_start(
            connection,
            approval_id=row["id"],
            payload=payload,
            case_status=target["run"]["case_status"],
            run_status=target["run"]["status"],
            job_key=stable_key("execute-research", RUN_ID),
            event_key=stable_key("workflow.research-started", RUN_ID),
        )
    except ResearchStartDenied as exc:
        raise LaunchRefusal(
            "consumed start approval has no matching workflow event and job"
        ) from exc
    return {
        "approval_id": str(row["id"]),
        "approved_by": str(row["approved_by"]),
        "consumed_at": row["consumed_at"].isoformat(),
        "payload_sha256": row["payload_sha256"],
    }


async def _guard_scheduler_scope(
    connection: asyncpg.Connection, group_ids: dict[int, UUID], *, mode: str
) -> dict[str, int]:
    running_jobs = int(
        await connection.fetchval(
            """SELECT count(*) FROM jobs
               WHERE status='running'
                 AND NOT ($1='pause' AND (
                   (kind='parallel_research_task'
                    AND payload->>'research_run_id'=$2)
                   OR (kind='prepare_supplier_outreach'
                       AND payload->>'case_id'=$3)
                 ))""",
            mode,
            str(RUN_ID),
            str(CASE_ID),
        )
        or 0
    )
    if running_jobs and mode != "pause":
        raise LaunchRefusal("a foreign or non-drainable worker job is already running")

    target_active_research_jobs = int(
        await connection.fetchval(
            """SELECT count(*) FROM jobs
               WHERE kind='parallel_research_task'
                 AND payload->>'research_run_id'=$1
                 AND status IN ('pending','retry','running')
                 AND attempts<max_attempts""",
            str(RUN_ID),
        )
        or 0
    )
    if target_active_research_jobs and mode == "retry_failed":
        raise LaunchRefusal("target research jobs are still runnable or in flight")

    foreign_runnable = int(
        await connection.fetchval(
            """SELECT count(*) FROM jobs job
               WHERE job.status IN ('pending','retry') AND job.attempts<job.max_attempts
                 AND (job.kind='enqueue_whatsapp_send' OR NOT COALESCE((
                   (job.kind IN ('parallel_research_task','execute_research')
                    AND job.payload->>'research_run_id'=$1)
                   OR (job.kind='prepare_supplier_outreach'
                       AND job.payload->>'case_id'=$2)
                 ),false))""",
            str(RUN_ID),
            str(CASE_ID),
        )
        or 0
    )
    if foreign_runnable and mode != "pause":
        raise LaunchRefusal("foreign runnable jobs or WhatsApp-send jobs exist")

    foreign_active_groups = bool(
        await connection.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM research_item_groups grp
                 JOIN research_runs run ON run.id=grp.research_run_id
                 JOIN procurement_cases case_row ON case_row.id=run.case_id
                 WHERE grp.status IN ('queued','researching') AND run.status='running'
                   AND case_row.status IN ('researching','contacting')
                   AND grp.id<>ALL($1::uuid[])
               )""",
            list(group_ids.values()),
        )
    )
    if foreign_active_groups and mode != "pause":
        raise LaunchRefusal("foreign active research groups exist")

    runnable_outbox = int(
        await connection.fetchval(
            """SELECT count(*) FROM outbox_events
               WHERE status IN ('pending','retry') AND available_at<=now()"""
        )
        or 0
    )
    sending_outbox = int(
        await connection.fetchval("SELECT count(*) FROM outbox_events WHERE status='sending'") or 0
    )
    if (runnable_outbox or sending_outbox) and mode != "pause":
        raise LaunchRefusal("runnable or in-flight outbox events exist")
    live_leases = int(
        await connection.fetchval(
            """SELECT count(*) FROM research_branch_checkpoints
               WHERE research_run_id=$1 AND owner_worker IS NOT NULL AND lease_until>now()""",
            RUN_ID,
        )
        or 0
    ) + int(
        await connection.fetchval(
            """SELECT count(*) FROM research_lanes lane
               JOIN research_item_groups grp ON grp.id=lane.group_id
               WHERE grp.research_run_id=$1 AND lane.owner_worker IS NOT NULL
                 AND lane.lease_until>now()""",
            RUN_ID,
        )
        or 0
    )
    if live_leases and mode != "pause":
        raise LaunchRefusal("a live target research lease exists; wait for the worker to drain")
    return {
        "other_running_jobs": running_jobs,
        "target_active_research_jobs": target_active_research_jobs,
        "foreign_runnable_jobs": foreign_runnable,
        "foreign_active_groups": int(foreign_active_groups),
        "runnable_outbox_events": runnable_outbox,
        "sending_outbox_events": sending_outbox,
        "live_target_research_leases": live_leases,
    }


async def _validate_scope(connection: asyncpg.Connection, target: dict[str, Any]) -> dict[str, Any]:
    source_plans = target["plan"].get("source_plans")
    if not isinstance(source_plans, list) or not source_plans:
        raise LaunchRefusal("frozen plan has no source plans")
    locality_payload = (
        source_plans[0].get("locality") if isinstance(source_plans[0], dict) else None
    )
    if not isinstance(locality_payload, dict):
        raise LaunchRefusal("frozen source plan has no locality")
    effective = await load_effective_scope(
        connection,
        RUN_ID,
        LocalityConstraint.model_validate(locality_payload),
    )
    revision = effective.revision
    if (
        revision is None
        or revision.scope.value != "citywide"
        or revision.preferred_area_text != PREFERRED_AREA
        or revision.authorized_by != target["run"]["owner_user_id"]
        or revision.base_plan_sha256 != target["plan_sha256"]
    ):
        raise LaunchRefusal("recorded owner-authorized Northern Ring scope revision is missing")
    return revision.to_context()


async def _profile_readiness(
    connection: asyncpg.Connection, target: dict[str, Any], *, staged_enabled: bool
) -> dict[str, int]:
    current = 0
    needs_native_generation = 0
    for _line, group_id in target["group_ids"].items():
        item_id = next(
            group["request_item_id"] for group in target["groups"] if group["id"] == group_id
        )
        snapshot = await load_search_item_snapshot(connection, RUN_ID, item_id)
        effective_prompt = _effective_prompt_version(
            PROFILE_PROMPT_VERSION, _authorized_scope_revision(snapshot.context)
        )
        row = await connection.fetchrow(
            """SELECT profile.id,profile.profile_version,profile.snapshot_sha256,
                      profile.plan_sha256,profile.model_version,profile.prompt_version,
                      count(intent.id)::int AS intent_count,
                      count(intent.id) FILTER (
                          WHERE intent.scope_revision=profile.profile_version
                      )::int AS current_intents
               FROM research_search_profiles profile
               LEFT JOIN research_search_intents intent ON intent.profile_id=profile.id
               WHERE profile.research_run_id=$1 AND profile.request_item_id=$2
               GROUP BY profile.id ORDER BY profile.profile_version DESC LIMIT 1""",
            RUN_ID,
            item_id,
        )
        valid = bool(
            row is not None
            and row["snapshot_sha256"] == snapshot.snapshot_sha256
            and row["plan_sha256"] == snapshot.plan_sha256
            and row["model_version"] == PROFILE_MODEL_VERSION
            and row["prompt_version"] == effective_prompt
            and int(row["intent_count"]) > 0
            and int(row["current_intents"]) == int(row["intent_count"])
        )
        if valid:
            current += 1
        else:
            needs_native_generation += 1
    if needs_native_generation and not staged_enabled:
        raise LaunchRefusal("some items need current profiles, but staged research is disabled")
    return {
        "valid_current_profiles": current,
        "native_profile_generation_needed": needs_native_generation,
    }


async def _capture_preserved_state(
    connection: asyncpg.Connection, group_ids: dict[int, UUID]
) -> dict[str, Any]:
    rows = await connection.fetch(
        """SELECT cp.id,cp.current_batch_no,cp.excluded_external_ids,
                  cp.candidate_receipts,cp.search_cursor_state,cp.source_progress_token,
                  cp.semantic_intent_ordinal,cp.candidates_persisted,
                  cp.own_database_candidates,cp.max_batches
           FROM research_branch_checkpoints cp
           WHERE cp.group_id=ANY($1::uuid[]) ORDER BY cp.id""",
        list(group_ids.values()),
    )
    if len(rows) == 0:
        raise LaunchRefusal("the 55 item groups have no branch checkpoints")
    state = [dict(row) for row in rows]
    return {"state": state, "hash": _canonical_hash(state)}


async def _retryable_checkpoints(
    connection: asyncpg.Connection, group_ids: dict[int, UUID]
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """SELECT cp.id,cp.group_id,item.line_number,cp.stop_reason
           FROM research_branch_checkpoints cp
           JOIN research_item_groups grp ON grp.id=cp.group_id
           JOIN request_items item ON item.id=grp.request_item_id
           WHERE cp.research_run_id=$1 AND cp.status='failed'
             AND grp.status IN ('partial','failed')
             AND cp.stop_reason LIKE ANY($2::text[])
             AND NOT EXISTS (
               SELECT 1 FROM jobs prior_job
               WHERE prior_job.kind='parallel_research_task'
                 AND prior_job.payload->>'task_id'=cp.id::text
                 AND prior_job.payload->>'operator_retry'='true'
             )
           ORDER BY item.line_number,cp.lane_no,cp.query_id""",
        RUN_ID,
        [prefix + "%" for prefix in _RETRYABLE_TECHNICAL_FAILURE_PREFIXES],
    )
    valid_group_ids = set(group_ids.values())
    candidates = [dict(row) for row in rows]
    if any(row["group_id"] not in valid_group_ids for row in candidates):
        raise LaunchRefusal("a retryable checkpoint is outside the allowlisted 55 groups")
    return candidates


async def _preflight(
    connection: asyncpg.Connection,
    *,
    mode: str,
    settings: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode != "pause":
        validate_worker_limits(settings)
    target = await _read_target(connection)
    if mode == "resume" and not target["is_active"]:
        raise LaunchRefusal("case or run is already terminal/inactive; nothing can be resumed")
    target["group_ids"] = validate_groups(target["groups"], require_paused=mode == "resume")
    retry_candidates: list[dict[str, Any]] = []
    if mode == "retry_failed":
        validate_retry_recovery(target["run"], target["groups"])
        retry_candidates = await _retryable_checkpoints(connection, target["group_ids"])
        if not retry_candidates:
            raise LaunchRefusal("no eligible retryable technical checkpoints remain")
        target["retry_candidates"] = retry_candidates
    workload = await _guard_scheduler_scope(connection, target["group_ids"], mode=mode)
    scope = await _validate_scope(connection, target)
    baseline = await _capture_baseline(connection, target["group_ids"])
    if baseline["baseline_hash"] != EXPECTED_BASELINE_SHA256:
        raise LaunchRefusal("the frozen 55-item/run/query baseline hash changed")
    approval = await _verify_consumed_start_approval(connection, target)
    profiles = (
        await _profile_readiness(
            connection, target, staged_enabled=bool(settings.staged_research_enabled)
        )
        if mode != "pause"
        else {"profile_check_skipped": 1}
    )
    preserved = await _capture_preserved_state(connection, target["group_ids"])
    return target, {
        "mode": mode,
        "case_id": str(CASE_ID),
        "run_id": str(RUN_ID),
        "baseline_hash": baseline["baseline_hash"],
        "plan_sha256": target["plan_sha256"],
        "scope_revision": scope,
        "consumed_start_approval": approval,
        "groups_total": len(target["groups"]),
        "group_statuses": {str(row["line_number"]): row["status"] for row in target["groups"]},
        "retryable_checkpoint_count": len(retry_candidates),
        "retry_group_lines": sorted({int(row["line_number"]) for row in retry_candidates}),
        "limits": {
            "max_active_groups": min(int(settings.research_active_groups), MAX_ACTIVE_GROUPS),
            "browser_cli_concurrency": int(settings.browser_cli_concurrency),
        },
        "workload": workload,
        "profiles": profiles,
        "checkpoint_state_hash": preserved["hash"],
        "checkpoint_state": preserved["state"],
    }


async def manage(mode: str) -> dict[str, Any]:
    if mode not in {"preflight", "pause", "resume", "retry_failed"}:
        raise ValueError("unsupported mode")
    settings = get_settings()
    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEDULER_LOCK_KEY)
                target, report = await _preflight(connection, mode=mode, settings=settings)
                if mode == "preflight":
                    report.pop("checkpoint_state", None)
                    return report
                if mode == "pause" and (
                    not target["is_active"]
                    or all(
                        group["status"] in {"succeeded", "partial", "failed"}
                        for group in target["groups"]
                    )
                ):
                    report.pop("checkpoint_state", None)
                    report.update(
                        {
                            "mode": "pause",
                            "pause_performed": False,
                            "no_op_reason": "case, run, or all groups are already terminal",
                        }
                    )
                    return report
                executor = ParallelResearchRunExecutor(
                    connection, None, max_active_groups=MAX_ACTIVE_GROUPS
                )
                if mode == "resume":
                    # Apply the authorized citywide search budget additively. This leaves
                    # batch numbers, cursors, exclusions, receipts, and evidence intact.
                    await connection.execute(
                        """UPDATE research_branch_checkpoints cp
                           SET max_batches=GREATEST(cp.max_batches,$2),updated_at=now()
                           FROM research_item_groups grp JOIN request_items item
                             ON item.id=grp.request_item_id
                           WHERE cp.group_id=grp.id AND grp.research_run_id=$1
                             AND item.line_number BETWEEN 1 AND 55""",
                        RUN_ID,
                        CITYWIDE_MAX_BATCHES,
                    )
                    await executor.resume_run(RUN_ID)
                elif mode == "retry_failed":
                    retried_checkpoint_ids = await executor.retry_failed(RUN_ID)
                    if len(retried_checkpoint_ids) != report["retryable_checkpoint_count"]:
                        raise LaunchRefusal(
                            "native retry eligibility changed after the operator preflight"
                        )
                else:
                    await executor.pause_run(RUN_ID)
                after = await _capture_preserved_state(connection, target["group_ids"])
                before_state = report.pop("checkpoint_state")
                if _preservation_projection(after["state"]) != _preservation_projection(
                    before_state
                ):
                    raise LaunchRefusal(
                        "lifecycle operation changed existing cursor or result state"
                    )
                statuses = await connection.fetch(
                    """SELECT item.line_number,grp.status FROM research_item_groups grp
                       JOIN request_items item ON item.id=grp.request_item_id
                       WHERE grp.research_run_id=$1 ORDER BY item.line_number""",
                    RUN_ID,
                )
                terminal_before = {
                    str(group["line_number"]): group["status"]
                    for group in target["groups"]
                    if group["status"] in {"succeeded", "partial", "failed"}
                }
                status_after = {str(row["line_number"]): row["status"] for row in statuses}
                recovered_lines = set(report.get("retry_group_lines", []))
                protected_terminal = {
                    line: status
                    for line, status in terminal_before.items()
                    if int(line) not in recovered_lines
                }
                if any(
                    status_after.get(line) != status
                    for line, status in protected_terminal.items()
                ):
                    raise LaunchRefusal("lifecycle operation changed an unrelated terminal group")
                if mode == "retry_failed" and any(
                    status_after.get(str(line)) not in {"queued", "researching"}
                    for line in recovered_lines
                ):
                    raise LaunchRefusal("native recovery did not admit eligible failed groups")
                report.update(
                    {
                        "mode": mode,
                        "checkpoint_state_hash_after": after["hash"],
                        "group_statuses_after": {
                            str(row["line_number"]): row["status"] for row in statuses
                        },
                        "checkpoint_cursors_and_results_preserved": True,
                        "terminal_groups_preserved": len(protected_terminal),
                        "recovered_group_lines": sorted(recovered_lines)
                        if mode == "retry_failed"
                        else [],
                        "retried_checkpoint_count": len(retried_checkpoint_ids)
                        if mode == "retry_failed"
                        else 0,
                        "pause_performed": mode == "pause",
                        "citywide_max_batches": CITYWIDE_MAX_BATCHES if mode == "resume" else None,
                    }
                )
                return report
    finally:
        await pool.close()


def _preservation_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "id",
        "current_batch_no",
        "excluded_external_ids",
        "candidate_receipts",
        "search_cursor_state",
        "source_progress_token",
        "semantic_intent_ordinal",
        "candidates_persisted",
        "own_database_candidates",
    )
    return [{key: row[key] for key in keys} for row in rows]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--pause", dest="mode", action="store_const", const="pause")
    modes.add_argument("--resume", dest="mode", action="store_const", const="resume")
    modes.add_argument("--retry-failed", dest="mode", action="store_const", const="retry_failed")
    parser.set_defaults(mode="preflight")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(asyncio.run(manage(args.mode)), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
