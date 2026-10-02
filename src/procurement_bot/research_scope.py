from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from procurement_bot.intake import SearchScope
from procurement_bot.research import LocalityConstraint

_SCHEDULER_LOCK_KEY = 731940251


class ScopeRevisionRejected(ValueError):
    """A scope revision failed its authorization or execution-state checks."""


class ScopeRevisionConflict(ScopeRevisionRejected):
    """An authorization reference was reused for a different revision payload."""


class FrozenPlanInvalid(ScopeRevisionRejected):
    """The persisted approved plan is malformed or no longer matches its hash."""


@dataclass(frozen=True, slots=True)
class ResearchScopeRevision:
    id: UUID
    research_run_id: UUID
    revision: int
    city: str
    scope: SearchScope
    preferred_area_text: str
    authorization_reference: str
    authorized_by: UUID
    base_plan_sha256: str
    revision_sha256: str
    created_at: datetime

    def to_context(self) -> dict[str, Any]:
        """Return JSON-safe provenance for an execution context."""
        return {
            "id": str(self.id),
            "revision": self.revision,
            "city": self.city,
            "scope": self.scope.value,
            "preferred_area_text": self.preferred_area_text,
            "authorization_reference": self.authorization_reference,
            "authorized_by": str(self.authorized_by),
            "base_plan_sha256": self.base_plan_sha256,
            "revision_sha256": self.revision_sha256,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EffectiveResearchScope:
    locality: LocalityConstraint
    revision: ResearchScopeRevision | None = None

    @property
    def preferred_area_text(self) -> str | None:
        return self.revision.preferred_area_text if self.revision else None

    def to_context(self) -> dict[str, Any]:
        return {
            "locality": self.locality.model_dump(mode="json"),
            "preferred_area_text": self.preferred_area_text,
            "scope_revision": self.revision.to_context() if self.revision else None,
        }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise FrozenPlanInvalid("approved research plan is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FrozenPlanInvalid("approved research plan has an unknown shape")
    return value


def _validated_base_plan(search_scope: object) -> tuple[str, dict[str, Any]]:
    envelope = _mapping(search_scope)
    if set(envelope) != {"contract_version", "plan_sha256", "plan"}:
        raise FrozenPlanInvalid("approved research plan envelope has an unknown shape")
    if envelope["contract_version"] != 1:
        raise FrozenPlanInvalid("unsupported approved research plan contract version")
    digest = envelope["plan_sha256"]
    plan = _mapping(envelope["plan"])
    if not isinstance(digest, str) or len(digest) != 64:
        raise FrozenPlanInvalid("approved research plan hash is missing")
    calculated = hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()
    if calculated != digest:
        raise FrozenPlanInvalid("approved research plan hash does not match its payload")
    return digest, plan


def _row_to_revision(row: Any) -> ResearchScopeRevision:
    return ResearchScopeRevision(
        id=row["id"],
        research_run_id=row["research_run_id"],
        revision=int(row["revision"]),
        city=row["city"],
        scope=SearchScope(row["scope"]),
        preferred_area_text=row["preferred_area_text"],
        authorization_reference=row["authorization_reference"],
        authorized_by=row["authorized_by"],
        base_plan_sha256=row["base_plan_sha256"],
        revision_sha256=row["revision_sha256"],
        created_at=row["created_at"],
    )


def _revision_payload(
    *,
    run_id: UUID,
    revision: int,
    city: str,
    preferred_area_text: str,
    authorization_reference: str,
    authorized_by: UUID,
    base_plan_sha256: str,
) -> dict[str, Any]:
    return {
        "research_run_id": str(run_id),
        "revision": revision,
        "city": city,
        "scope": SearchScope.CITYWIDE.value,
        "preferred_area_text": preferred_area_text,
        "authorization_reference": authorization_reference,
        "authorized_by": str(authorized_by),
        "base_plan_sha256": base_plan_sha256,
    }


def _verify_revision(row: Any, *, base_plan_sha256: str, city: str) -> ResearchScopeRevision:
    revision = _row_to_revision(row)
    if (
        revision.base_plan_sha256 != base_plan_sha256
        or revision.city.strip().casefold() != city.strip().casefold()
    ):
        raise FrozenPlanInvalid("scope revision does not match the frozen plan")
    expected = hashlib.sha256(
        _canonical_json(
            _revision_payload(
                run_id=revision.research_run_id,
                revision=revision.revision,
                city=revision.city,
                preferred_area_text=revision.preferred_area_text,
                authorization_reference=revision.authorization_reference,
                authorized_by=revision.authorized_by,
                base_plan_sha256=revision.base_plan_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    if expected != revision.revision_sha256:
        raise FrozenPlanInvalid("scope revision hash does not match its payload")
    return revision


async def append_citywide_scope_revision(
    connection: Any,
    run_id: UUID,
    authorized_by: UUID,
    authorization_reference: str,
    preferred_area_text: str,
) -> ResearchScopeRevision:
    """Append a citywide execution revision after a recorded human authorization.

    This function never edits the approved plan, queues work, or approves supplier
    contact. The caller supplies a human-provided authorization reference.
    """
    if not authorization_reference.strip():
        raise ValueError("authorization_reference must be nonempty")
    if not preferred_area_text.strip():
        raise ValueError("preferred_area_text must be nonempty")

    async with connection.transaction():
        # Match the parallel scheduler's lock so pause/resume/admission changes
        # cannot interleave with this authorization-state check.
        await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
        run = await connection.fetchrow(
            """SELECT run.id,run.search_scope,case_row.city,case_row.owner_user_id
            FROM research_runs run
            JOIN procurement_cases case_row ON case_row.id=run.case_id
            WHERE run.id=$1 FOR UPDATE OF run,case_row""",
            run_id,
        )
        if run is None:
            raise ScopeRevisionRejected("research run does not exist")
        if authorized_by != run["owner_user_id"]:
            raise ScopeRevisionRejected("scope revision must be authorized by the case owner")

        base_hash, plan = _validated_base_plan(run["search_scope"])
        plan_city = plan.get("city")
        case_city = run["city"].strip()
        if not isinstance(plan_city, str) or plan_city.strip().casefold() != case_city.casefold():
            raise FrozenPlanInvalid("frozen plan city differs from the procurement case city")
        if plan.get("scope") != SearchScope.SPECIFIC_AREA.value:
            raise ScopeRevisionRejected("citywide revision is only valid for a specific-area plan")
        source_plans = plan.get("source_plans")
        if not isinstance(source_plans, list) or not source_plans:
            raise FrozenPlanInvalid("frozen plan has no source plans")
        for source in source_plans:
            if not isinstance(source, dict) or not isinstance(source.get("locality"), dict):
                raise FrozenPlanInvalid("frozen source plan has no locality")
            locality = source["locality"]
            if (
                locality.get("scope") != SearchScope.SPECIFIC_AREA.value
                or not isinstance(locality.get("city"), str)
                or locality["city"].strip().casefold() != case_city.casefold()
            ):
                raise FrozenPlanInvalid("frozen source locality differs from the requested scope")

        existing = await connection.fetchrow(
            """SELECT * FROM research_scope_revisions
            WHERE research_run_id=$1 AND authorization_reference=$2""",
            run_id,
            authorization_reference.strip(),
        )
        if existing is not None:
            try:
                revision = _verify_revision(
                    existing, base_plan_sha256=base_hash, city=case_city
                )
            except FrozenPlanInvalid:
                raise
            if (
                revision.authorized_by != authorized_by
                or revision.preferred_area_text != preferred_area_text.strip()
                or revision.scope != SearchScope.CITYWIDE
            ):
                raise ScopeRevisionConflict(
                    "authorization reference already exists with a different payload"
                )
            return revision

        groups = await connection.fetchrow(
            """SELECT count(*) AS group_count,
                count(*) FILTER (WHERE status <> 'paused') AS nonpaused_count
            FROM research_item_groups WHERE research_run_id=$1""",
            run_id,
        )
        if int(groups["group_count"]) == 0 or int(groups["nonpaused_count"]) != 0:
            raise ScopeRevisionRejected("all research item groups must exist and be paused")
        active = await connection.fetchval(
            """SELECT EXISTS (
                SELECT 1 FROM jobs job
                WHERE job.status='running'
                  AND job.payload->>'research_run_id'=$1
            ) OR EXISTS (
                SELECT 1 FROM research_branch_checkpoints checkpoint
                WHERE checkpoint.research_run_id=$2 AND (
                    checkpoint.status='researching'
                    OR (checkpoint.owner_worker IS NOT NULL
                        AND checkpoint.lease_until > now())
                    OR EXISTS (
                        SELECT 1 FROM jobs job
                        WHERE job.id=checkpoint.current_job_id AND job.status='running'
                    )
                )
            ) OR EXISTS (
                SELECT 1 FROM research_lanes lane
                JOIN research_item_groups grp ON grp.id=lane.group_id
                WHERE grp.research_run_id=$2 AND (
                    lane.status='researching'
                    OR (lane.owner_worker IS NOT NULL AND lane.lease_until > now())
                )
            )""",
            str(run_id),
            run_id,
        )
        if active:
            raise ScopeRevisionRejected("running batch jobs or active research leases remain")

        next_revision = int(
            await connection.fetchval(
                """SELECT COALESCE(max(revision),0)+1 FROM research_scope_revisions
                WHERE research_run_id=$1""",
                run_id,
            )
        )
        payload = _revision_payload(
            run_id=run_id,
            revision=next_revision,
            city=case_city,
            preferred_area_text=preferred_area_text.strip(),
            authorization_reference=authorization_reference.strip(),
            authorized_by=authorized_by,
            base_plan_sha256=base_hash,
        )
        revision_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        row = await connection.fetchrow(
            """INSERT INTO research_scope_revisions(
                research_run_id,revision,city,scope,preferred_area_text,
                authorization_reference,authorized_by,base_plan_sha256,revision_sha256
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
            run_id,
            next_revision,
            case_city,
            SearchScope.CITYWIDE.value,
            preferred_area_text.strip(),
            authorization_reference.strip(),
            authorized_by,
            base_hash,
            revision_hash,
        )
        return _verify_revision(row, base_plan_sha256=base_hash, city=case_city)


async def load_effective_scope(
    connection: Any,
    run_id: UUID,
    frozen_locality: LocalityConstraint,
) -> EffectiveResearchScope:
    """Load the latest valid execution override, leaving the approved plan untouched."""
    if not isinstance(frozen_locality, LocalityConstraint):
        raise TypeError("frozen_locality must be a LocalityConstraint")
    row = await connection.fetchrow(
        """SELECT * FROM research_scope_revisions
        WHERE research_run_id=$1 ORDER BY revision DESC LIMIT 1""",
        run_id,
    )
    # Preserve legacy plans of every supported scope when they have no override.
    # The normal execution path validates the frozen plan and its digest itself.
    if row is None:
        return EffectiveResearchScope(locality=frozen_locality)
    base_hash, plan = _validated_base_plan(
        await connection.fetchval("SELECT search_scope FROM research_runs WHERE id=$1", run_id)
    )
    frozen_city = plan.get("city")
    if (
        not isinstance(frozen_city, str)
        or frozen_city.strip().casefold() != frozen_locality.city.strip().casefold()
        or plan.get("scope") != SearchScope.SPECIFIC_AREA.value
        or frozen_locality.scope != SearchScope.SPECIFIC_AREA
    ):
        raise FrozenPlanInvalid("requested locality does not match the frozen run city and scope")
    source_plans = plan.get("source_plans")
    if not isinstance(source_plans, list) or not any(
        isinstance(source, dict)
        and isinstance(source.get("locality"), dict)
        and isinstance(source["locality"].get("city"), str)
        and source["locality"]["city"].strip().casefold() == frozen_locality.city.strip().casefold()
        and source["locality"].get("scope") == frozen_locality.scope.value
        and source["locality"].get("raw_area_text") == frozen_locality.raw_area_text
        for source in source_plans
    ):
        raise FrozenPlanInvalid("requested locality is not present in the frozen source plans")
    revision = _verify_revision(
        row, base_plan_sha256=base_hash, city=frozen_locality.city
    )
    locality = LocalityConstraint(
        city=revision.city,
        scope=SearchScope.CITYWIDE,
        raw_area_text=None,
        canonical_place_id=None,
        canonical_place_name=None,
        point=None,
    )
    return EffectiveResearchScope(locality=locality, revision=revision)
