from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.research import ResearchPlan, ResearchSource, SourcePlan

ResearchRunStatus = Literal["planned", "running", "succeeded", "partial", "failed"]
TerminalResearchRunStatus = Literal["succeeded", "partial", "failed"]

_SOURCE_PROVIDERS = {
    ResearchSource.OWN_DATABASE: "own_database",
    # The current browser boundary models the public 2GIS web surface, not its API.
    ResearchSource.TWO_GIS: "2gis_web",
    ResearchSource.WEB: "web",
}


class ResearchPersistenceConflict(RuntimeError):
    """An idempotency key already represents different immutable research input."""


class ResearchRunNotFound(LookupError):
    """The requested research run does not exist."""


class InvalidResearchRunTransition(RuntimeError):
    """The persisted run cannot move to the requested lifecycle state."""


@dataclass(frozen=True, slots=True)
class PersistedResearchRun:
    id: UUID
    case_id: UUID
    status: ResearchRunStatus
    plan_sha256: str
    query_ids: tuple[UUID, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class ResearchRunLifecycle:
    id: UUID
    status: ResearchRunStatus
    error_code: str


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("research plan must contain lossless finite JSON values") from exc


def recompute_plan_sha256(plan: ResearchPlan) -> str:
    unsigned = plan.model_dump(mode="json", exclude={"plan_sha256"})
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def research_run_idempotency_key(case_id: UUID, plan_sha256: str) -> str:
    if not isinstance(case_id, UUID):
        raise TypeError("case_id must be a UUID")
    if len(plan_sha256) != 64 or any(char not in "0123456789abcdef" for char in plan_sha256):
        raise ValueError("plan_sha256 must be a lowercase SHA-256 digest")
    digest = hashlib.sha256(f"{case_id}:{plan_sha256}".encode()).hexdigest()
    return f"research-run:{digest}"


def research_job_payload(run_id: UUID) -> dict[str, str]:
    """Return the complete worker payload; large plan data remains in PostgreSQL."""

    if not isinstance(run_id, UUID):
        raise TypeError("run_id must be a UUID")
    return {"research_run_id": str(run_id)}


def _run_payload(plan: ResearchPlan) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "plan_sha256": plan.plan_sha256,
        "plan": plan.model_dump(mode="json", exclude={"plan_sha256"}),
    }


def _query_payload(branch: SourcePlan, ordinal: int) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "branch_ordinal": ordinal,
        "item_index": branch.item_index,
        "source": branch.source.value,
        "purpose": branch.purpose.value,
        "query_terms": list(branch.query_terms),
        "locality": branch.locality.model_dump(mode="json"),
        "minimum_unique_candidates": branch.minimum_unique_candidates,
        "result_ttl_hours": branch.result_ttl_hours,
        "empty_page_limit": branch.empty_page_limit,
        "blocked_reason": branch.blocked_reason,
    }


def _query_idempotency_key(run_id: UUID, ordinal: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{ordinal}".encode()).hexdigest()
    return f"research-query:{digest}"


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ResearchPersistenceConflict("persisted JSON must be an object")
        return decoded
    if isinstance(value, Mapping):
        return dict(value)
    raise ResearchPersistenceConflict("persisted JSON must be an object")


def _assert_equal(field: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ResearchPersistenceConflict(f"persisted {field} differs from requested value")


async def _persist_query(
    connection: asyncpg.Connection,
    *,
    run_id: UUID,
    ordinal: int,
    branch: SourcePlan,
) -> UUID:
    query_id = uuid4()
    provider = _SOURCE_PROVIDERS[branch.source]
    parameters = _query_payload(branch, ordinal)
    locality = branch.locality
    latitude = Decimal(str(locality.point.latitude)) if locality.point is not None else None
    longitude = Decimal(str(locality.point.longitude)) if locality.point is not None else None
    search_area = locality.raw_area_text or locality.canonical_place_name or ""
    idempotency_key = _query_idempotency_key(run_id, ordinal)
    row = await connection.fetchrow(
        """
        INSERT INTO research_queries(
            id,research_run_id,provider,query_text,city,search_area_text,
            latitude,longitude,radius_meters,parameters,idempotency_key
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NULL,$9::jsonb,$10)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING *
        """,
        query_id,
        run_id,
        provider,
        branch.rendered_query,
        locality.city,
        search_area,
        latitude,
        longitude,
        parameters,
        idempotency_key,
    )
    if row is None:
        row = await connection.fetchrow(
            "SELECT * FROM research_queries WHERE idempotency_key=$1",
            idempotency_key,
        )
    if row is None:
        raise RuntimeError("research query disappeared after idempotent insert")
    expected = {
        "research_run_id": run_id,
        "provider": provider,
        "query_text": branch.rendered_query,
        "city": locality.city,
        "search_area_text": search_area,
        "latitude": latitude,
        "longitude": longitude,
        "radius_meters": None,
        "parameters": parameters,
    }
    for field, value in expected.items():
        actual = _mapping(row[field]) if field == "parameters" else row[field]
        _assert_equal(field, actual, value)
    return row["id"]


async def persist_research_plan(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    plan: ResearchPlan,
    requested_by: UUID | None = None,
) -> PersistedResearchRun:
    """Atomically persist one immutable plan and every source branch.

    Retries return the existing run. Reusing any derived idempotency key for changed
    data fails closed instead of silently accepting a partially different plan.
    """

    actual_hash = recompute_plan_sha256(plan)
    if actual_hash != plan.plan_sha256:
        raise ResearchPersistenceConflict("plan_sha256 does not match the plan payload")
    idempotency_key = research_run_idempotency_key(case_id, plan.plan_sha256)
    search_scope = _run_payload(plan)
    async with connection.transaction():
        proposed_id = uuid4()
        row = await connection.fetchrow(
            """
            INSERT INTO research_runs(
                id,case_id,status,requested_by,search_scope,idempotency_key
            )
            VALUES ($1,$2,'planned',$3,$4::jsonb,$5)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            proposed_id,
            case_id,
            requested_by,
            search_scope,
            idempotency_key,
        )
        created = row is not None
        if row is None:
            row = await connection.fetchrow(
                "SELECT * FROM research_runs WHERE idempotency_key=$1",
                idempotency_key,
            )
        if row is None:
            raise RuntimeError("research run disappeared after idempotent insert")
        _assert_equal("case_id", row["case_id"], case_id)
        _assert_equal("requested_by", row["requested_by"], requested_by)
        _assert_equal("search_scope", _mapping(row["search_scope"]), search_scope)
        run_id: UUID = row["id"]
        query_ids = tuple(
            [
                await _persist_query(
                    connection,
                    run_id=run_id,
                    ordinal=ordinal,
                    branch=branch,
                )
                for ordinal, branch in enumerate(plan.source_plans)
            ]
        )
    return PersistedResearchRun(
        id=run_id,
        case_id=case_id,
        status=row["status"],
        plan_sha256=plan.plan_sha256,
        query_ids=query_ids,
        created=created,
    )


async def mark_research_running(
    connection: asyncpg.Connection,
    run_id: UUID,
) -> ResearchRunLifecycle:
    row = await connection.fetchrow(
        """
        UPDATE research_runs
        SET status='running',started_at=COALESCE(started_at,now()),error_code=''
        WHERE id=$1 AND status IN ('planned','running')
        RETURNING id,status,error_code
        """,
        run_id,
    )
    if row is not None:
        return ResearchRunLifecycle(row["id"], row["status"], row["error_code"])
    return await _resolve_idempotent_transition(connection, run_id, "running", "")


async def complete_research_run(
    connection: asyncpg.Connection,
    run_id: UUID,
    *,
    status: TerminalResearchRunStatus,
    error_code: str = "",
) -> ResearchRunLifecycle:
    if status not in {"succeeded", "partial", "failed"}:
        raise ValueError("terminal status must be succeeded, partial, or failed")
    normalized_error = error_code.strip()
    if status == "succeeded" and normalized_error:
        raise ValueError("a succeeded research run cannot have an error_code")
    if status == "failed" and not normalized_error:
        raise ValueError("a failed research run requires an error_code")
    if len(normalized_error) > 200:
        raise ValueError("error_code exceeds 200 characters")
    row = await connection.fetchrow(
        """
        UPDATE research_runs
        SET status=$2,completed_at=COALESCE(completed_at,now()),error_code=$3
        WHERE id=$1 AND status='running'
        RETURNING id,status,error_code
        """,
        run_id,
        status,
        normalized_error,
    )
    if row is not None:
        return ResearchRunLifecycle(row["id"], row["status"], row["error_code"])
    return await _resolve_idempotent_transition(
        connection, run_id, status, normalized_error
    )


async def _resolve_idempotent_transition(
    connection: asyncpg.Connection,
    run_id: UUID,
    target_status: ResearchRunStatus,
    error_code: str,
) -> ResearchRunLifecycle:
    row = await connection.fetchrow(
        "SELECT id,status,error_code FROM research_runs WHERE id=$1",
        run_id,
    )
    if row is None:
        raise ResearchRunNotFound(str(run_id))
    if row["status"] == target_status and row["error_code"] == error_code:
        return ResearchRunLifecycle(row["id"], row["status"], row["error_code"])
    if row["status"] == target_status:
        raise ResearchPersistenceConflict(
            "terminal research status was retried with a different error_code"
        )
    raise InvalidResearchRunTransition(
        f"cannot move research run from {row['status']} to {target_status}"
    )
