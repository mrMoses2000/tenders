from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.approvals import payload_sha256
from procurement_bot.queue import enqueue_job, stable_key

_APPROVAL_ACTION = "start_research"
_JOB_KIND = "execute_research"
_STARTED_EVENT = "research_started"
_STARTABLE_CASE_STATUS = "ready"
_STARTABLE_RUN_STATUS = "planned"
_STARTED_OR_LATER_CASE_STATUSES = {
    "researching",
    "contacting",
    "evaluating",
    "report_ready",
    "selected",
    "closed",
    "cancelled",
}
_STARTED_OR_LATER_RUN_STATUSES = {
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
}


class ResearchStartDenied(RuntimeError):
    """The approval or persisted workflow state does not authorise this start."""


@dataclass(frozen=True, slots=True)
class ResearchStartResult:
    approval_id: UUID
    case_id: UUID
    research_run_id: UUID
    job_id: UUID
    plan_sha256: str
    case_status: str
    run_status: str
    newly_started: bool


@dataclass(frozen=True, slots=True)
class _StartPayload:
    case_id: UUID
    research_run_id: UUID
    plan_sha256: str
    canonical: dict[str, str]
    policy_authorization_id: UUID | None = None
    policy_authorization_sha256: str | None = None


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ResearchStartDenied(f"{field} is not valid JSON") from exc
        value = decoded
    if not isinstance(value, Mapping):
        raise ResearchStartDenied(f"{field} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ResearchStartDenied(f"{field} keys must be strings")
    return dict(value)


def _parse_uuid(value: object, *, field: str) -> UUID:
    if not isinstance(value, str):
        raise ResearchStartDenied(f"{field} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ResearchStartDenied(f"{field} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ResearchStartDenied(f"{field} must be a canonical UUID string")
    return parsed


def _parse_start_payload(value: object) -> _StartPayload:
    payload = _mapping(value, field="approval payload")
    required = {"case_id", "research_run_id", "plan_sha256"}
    policy_fields = {"policy_authorization_id", "policy_authorization_sha256"}
    if set(payload) not in (required, required | policy_fields):
        raise ResearchStartDenied(
            "start_research approval payload must contain exactly "
            "case_id, research_run_id, and plan_sha256"
        )
    plan_hash = payload["plan_sha256"]
    if not isinstance(plan_hash, str) or len(plan_hash) != 64 or any(
        char not in "0123456789abcdef" for char in plan_hash
    ):
        raise ResearchStartDenied("plan_sha256 must be a lowercase SHA-256 digest")
    case_id = _parse_uuid(payload["case_id"], field="case_id")
    run_id = _parse_uuid(payload["research_run_id"], field="research_run_id")
    canonical = {
        "case_id": str(case_id),
        "research_run_id": str(run_id),
        "plan_sha256": plan_hash,
    }
    if policy_fields.issubset(payload):
        policy_id = _parse_uuid(payload["policy_authorization_id"], field="policy_authorization_id")
        policy_hash = payload["policy_authorization_sha256"]
        if not isinstance(policy_hash, str) or len(policy_hash) != 64 or any(
            char not in "0123456789abcdef" for char in policy_hash
        ):
            raise ResearchStartDenied("policy_authorization_sha256 must be a SHA-256 digest")
        canonical.update(policy_authorization_id=str(policy_id),
                         policy_authorization_sha256=policy_hash)
        return _StartPayload(case_id, run_id, plan_hash, canonical, policy_id, policy_hash)
    return _StartPayload(case_id, run_id, plan_hash, canonical)


def _event_policy_data(payload: _StartPayload) -> dict[str, str]:
    if payload.policy_authorization_id is None:
        return {}
    return {"authorization_mode": "owner_research_profile",
            "policy_authorization_id": str(payload.policy_authorization_id),
            "policy_authorization_sha256": str(payload.policy_authorization_sha256)}


def _plan_hash(search_scope: object) -> str:
    scope = _mapping(search_scope, field="research run search_scope")
    persisted_hash = scope.get("plan_sha256")
    plan = scope.get("plan")
    if not isinstance(persisted_hash, str) or not isinstance(plan, Mapping):
        raise ResearchStartDenied("research run has no verifiable persisted plan")
    try:
        canonical_plan = json.dumps(
            dict(plan),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchStartDenied("research run plan is not lossless JSON") from exc
    actual_hash = hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
    if actual_hash != persisted_hash:
        raise ResearchStartDenied("persisted research plan hash is invalid")
    return persisted_hash


@asynccontextmanager
async def _connection(
    database: asyncpg.Pool | asyncpg.Connection,
) -> AsyncIterator[asyncpg.Connection]:
    acquire = getattr(database, "acquire", None)
    if acquire is None:
        yield database  # type: ignore[misc]
        return
    async with acquire() as connection:
        yield connection


async def start_approved_research(
    database: asyncpg.Pool | asyncpg.Connection,
    *,
    approval_id: UUID,
    allow_followup: bool = False,
) -> ResearchStartResult:
    """Consume one exact approval and enqueue an internal research job.

    This is a control-plane transaction only. It deliberately performs no
    provider, browser, Telegram, or WhatsApp network call.
    """

    if not isinstance(approval_id, UUID):
        raise TypeError("approval_id must be a UUID")

    async with _connection(database) as connection, connection.transaction():
        approval = await connection.fetchrow(
            """
            SELECT id,case_id,action_type,payload,payload_sha256,status,
                approved_by,expires_at,consumed_at,
                (expires_at IS NULL OR expires_at>now()) AS is_live,
                (expires_at IS NULL OR consumed_at<expires_at) AS consumed_when_live
            FROM action_approvals
            WHERE id=$1
            FOR UPDATE
            """,
            approval_id,
        )
        if approval is None:
            raise ResearchStartDenied("start_research approval does not exist")
        if approval["action_type"] != _APPROVAL_ACTION:
            raise ResearchStartDenied("approval is for a different action")
        payload = _parse_start_payload(approval["payload"])
        if approval["case_id"] != payload.case_id:
            raise ResearchStartDenied("approval case does not match its payload")
        if approval["payload_sha256"] != payload_sha256(payload.canonical):
            raise ResearchStartDenied("approval payload hash is invalid")
        if approval["approved_by"] is None:
            raise ResearchStartDenied("start_research approval has no approving user")

        run = await connection.fetchrow(
            """
            SELECT id,case_id,status,search_scope
            FROM research_runs
            WHERE id=$1
            FOR UPDATE
            """,
            payload.research_run_id,
        )
        if run is None:
            raise ResearchStartDenied("approved research run does not exist")
        if run["case_id"] != payload.case_id:
            raise ResearchStartDenied("research run belongs to a different case")
        if _plan_hash(run["search_scope"]) != payload.plan_sha256:
            raise ResearchStartDenied("approval plan hash does not match the research run")

        case = await connection.fetchrow(
            "SELECT id,status FROM procurement_cases WHERE id=$1 FOR UPDATE",
            payload.case_id,
        )
        if case is None:
            raise ResearchStartDenied("approved procurement case does not exist")
        policy_snapshot = None
        if payload.policy_authorization_id is not None:
            from procurement_bot.owner_autonomy import (
                ResearchPolicyDenied,
                validate_research_policy_binding,
            )

            try:
                policy_snapshot = await validate_research_policy_binding(
                    connection, authorization_id=payload.policy_authorization_id,
                    authorization_sha256=str(payload.policy_authorization_sha256),
                    case_id=payload.case_id, run_id=payload.research_run_id,
                    plan_sha256=payload.plan_sha256, approved_by=approval["approved_by"],
                )
            except ResearchPolicyDenied as exc:
                raise ResearchStartDenied(str(exc)) from exc

        job_key = stable_key("execute-research", payload.research_run_id)
        event_key = stable_key("workflow.research-started", payload.research_run_id)
        if approval["status"] == "consumed":
            if approval["consumed_at"] is None or not approval["consumed_when_live"]:
                raise ResearchStartDenied("approval consumption is invalid")
            return await _resolve_idempotent_start(
                connection,
                approval_id=approval_id,
                payload=payload,
                case_status=case["status"],
                run_status=run["status"],
                job_key=job_key,
                event_key=event_key,
            )
        if approval["status"] != "approved" or not approval["is_live"]:
            raise ResearchStartDenied("approval is not approved and unexpired")
        city_tier = None
        if policy_snapshot is not None:
            from procurement_bot.city_tier_runtime import activate_city_admission
            try:
                city_tier = await activate_city_admission(
                    connection, policy_snapshot, payload.research_run_id,
                )
            except ValueError as exc:
                raise ResearchStartDenied(str(exc)) from exc
        if case["status"] != _STARTABLE_CASE_STATUS and not (
            allow_followup and case["status"] in {"evaluating", "report_ready"}
        ) and not (
            city_tier is not None
            and case["status"] in {"researching", "contacting", "evaluating", "report_ready"}
        ):
            raise ResearchStartDenied("procurement case is not ready for research")
        if run["status"] != _STARTABLE_RUN_STATUS:
            raise ResearchStartDenied("research run is not in planned state")

        consumed = await connection.execute(
            """
            UPDATE action_approvals
            SET status='consumed',consumed_at=now()
            WHERE id=$1 AND status='approved'
              AND (expires_at IS NULL OR expires_at>now())
            """,
            approval_id,
        )
        if consumed != "UPDATE 1":
            raise ResearchStartDenied("approval changed before it could be consumed")
        case_updated = await connection.execute(
            """
            UPDATE procurement_cases
            SET status='researching',updated_at=now()
            WHERE id=$1 AND (status='ready' OR ($2 AND status IN ('evaluating','report_ready'))
                OR ($3 AND status IN ('researching','contacting','evaluating','report_ready')))
            """,
            payload.case_id,
            allow_followup,
            city_tier is not None,
        )
        if case_updated != "UPDATE 1":
            raise ResearchStartDenied("procurement case changed before research start")
        run_updated = await connection.execute(
            """
            UPDATE research_runs
            SET status='running',started_at=COALESCE(started_at,now()),error_code=''
            WHERE id=$1 AND status='planned'
            """,
            payload.research_run_id,
        )
        if run_updated != "UPDATE 1":
            raise ResearchStartDenied("research run changed before research start")

        await connection.execute(
            """INSERT INTO case_pipeline_runtime(case_id,research_run_id,enabled)
            VALUES($1,$2,true) ON CONFLICT(case_id) DO UPDATE
            SET research_run_id=EXCLUDED.research_run_id,enabled=true,paused=false,
                next_tick_at=now(),wake_reason='approved_research',updated_at=now()""",
            payload.case_id, payload.research_run_id,
        )
        event_data = {
            "approval_id": str(approval_id),
            "research_run_id": str(payload.research_run_id),
            "plan_sha256": payload.plan_sha256,
            **_event_policy_data(payload),
        }
        if policy_snapshot is not None:
            # This persists the honest checkpoint from actual approved start.
            # Dispatching the existing partial report at this time is a later hook.
            await connection.execute(
                """UPDATE case_pipeline_runtime SET research_policy_snapshot_id=$2,
                active_city_tier=$5,report_checkpoint_at=COALESCE(report_checkpoint_at,
                (SELECT started_at FROM research_runs WHERE id=$3)+$4*interval '1 hour')
                WHERE case_id=$1""", payload.case_id, policy_snapshot["id"],
                payload.research_run_id,
                policy_snapshot["snapshot"]["policy"]["report_checkpoint_hours"],
                city_tier or 0,
            )
            if city_tier is not None:
                await connection.execute(
                    """INSERT INTO research_city_report_checkpoints(case_id,snapshot_id,
                    start_run_id,started_at,due_at) SELECT $1,$2,id,started_at,
                    started_at+interval '4 hours' FROM research_runs WHERE id=$3
                    ON CONFLICT(case_id,snapshot_id) DO NOTHING""", payload.case_id,
                    policy_snapshot["id"], payload.research_run_id,
                )
        event_inserted = await connection.fetchval(
            """
            INSERT INTO workflow_events(
                case_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES ($1,$2,'user',$3,$4,$5::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            payload.case_id,
            _STARTED_EVENT,
            str(approval["approved_by"]),
            event_key,
            event_data,
        )
        if event_inserted is None:
            raise ResearchStartDenied("research start event already exists unexpectedly")
        job_id = await enqueue_job(
            connection,
            kind=_JOB_KIND,
            payload={"research_run_id": str(payload.research_run_id)},
            idempotency_key=job_key,
        )
        if job_id is None:
            raise ResearchStartDenied("research execution job already exists unexpectedly")

        return ResearchStartResult(
            approval_id=approval_id,
            case_id=payload.case_id,
            research_run_id=payload.research_run_id,
            job_id=job_id,
            plan_sha256=payload.plan_sha256,
            case_status="researching",
            run_status="running",
            newly_started=True,
        )


async def _resolve_idempotent_start(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID,
    payload: _StartPayload,
    case_status: str,
    run_status: str,
    job_key: str,
    event_key: str,
) -> ResearchStartResult:
    if case_status not in _STARTED_OR_LATER_CASE_STATUSES:
        raise ResearchStartDenied("consumed approval has no started case state")
    if run_status not in _STARTED_OR_LATER_RUN_STATUSES:
        raise ResearchStartDenied("consumed approval has no started research run state")
    event = await connection.fetchrow(
        """
        SELECT event_type,data
        FROM workflow_events
        WHERE idempotency_key=$1
        """,
        event_key,
    )
    expected_event_data = {
        "approval_id": str(approval_id),
        "research_run_id": str(payload.research_run_id),
        "plan_sha256": payload.plan_sha256,
        **_event_policy_data(payload),
    }
    if (
        event is None
        or event["event_type"] != _STARTED_EVENT
        or _mapping(event["data"], field="workflow event data") != expected_event_data
    ):
        raise ResearchStartDenied("consumed approval has no matching workflow event")
    job = await connection.fetchrow(
        "SELECT id,kind,payload FROM jobs WHERE idempotency_key=$1",
        job_key,
    )
    expected_job_payload = {"research_run_id": str(payload.research_run_id)}
    if (
        job is None
        or job["kind"] != _JOB_KIND
        or _mapping(job["payload"], field="research job payload") != expected_job_payload
    ):
        raise ResearchStartDenied("consumed approval has no matching research job")
    return ResearchStartResult(
        approval_id=approval_id,
        case_id=payload.case_id,
        research_run_id=payload.research_run_id,
        job_id=job["id"],
        plan_sha256=payload.plan_sha256,
        case_status=case_status,
        run_status=run_status,
        newly_started=False,
    )
