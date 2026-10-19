"""Opt-in research consent, immutable intake bindings and owner-only source visibility.

No provider calls, outbound policy changes, or accounting writes belong here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from procurement_bot.approvals import payload_sha256
from procurement_bot.dialogue import SearchScopePolicy, evaluate_missing_blockers
from procurement_bot.intake import IntakeResult, ParsedProcurementRequest, ProcurementItem


class ResearchPolicyDenied(ValueError):
    """Consent or immutable source state does not authorize this operation."""


class ResearchAutomationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordered_cities: tuple[str, ...] = Field(min_length=1, max_length=5)
    search_scope: Literal["citywide"] = "citywide"
    ambiguity_policy: Literal["clear_items_only"] = "clear_items_only"
    report_checkpoint_hours: int = Field(default=4, ge=1, le=24)
    buyer_kind: Literal["physical_person"] = "physical_person"

    @field_validator("ordered_cities")
    @classmethod
    def valid_cities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not city.strip() or len(city) > 160 or city != city.strip() for city in values):
            raise ValueError("city names must be nonempty trimmed text")
        if len({city.casefold() for city in values}) != len(values):
            raise ValueError("cities must be distinct and ordered")
        return values


class ResearchProfileEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: UUID | None = None
    policy: ResearchAutomationPolicy
    consent_reference: str = Field(min_length=1, max_length=500)
    consent_text: str = Field(min_length=1, max_length=4000)
    research_autostart: Literal[True]
    expected_profile_id: UUID | None = None


async def _owner_scope(
    connection: asyncpg.Connection, owner_id: UUID, customer_id: UUID | None,
) -> None:
    if not await connection.fetchval("SELECT active FROM users WHERE id=$1", owner_id):
        raise ResearchPolicyDenied("active authenticated owner required")
    if customer_id is not None and not await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM customers WHERE id=$1 AND owner_user_id=$2 AND active)",
        customer_id, owner_id,
    ):
        raise ResearchPolicyDenied("project does not belong to this owner")


async def replace_research_profile(
    connection: asyncpg.Connection, *, owner_id: UUID, customer_id: UUID | None,
    policy: ResearchAutomationPolicy, consent_reference: str, consent_text: str,
    research_autostart: bool, expected_profile_id: UUID | None = None,
) -> UUID:
    """Owner-authenticated enrollment. A changed policy requires the exact old episode ID."""
    if not consent_reference.strip() or not consent_text.strip():
        raise ResearchPolicyDenied("explicit consent reference and text are required")
    async with connection.transaction():
        await _owner_scope(connection, owner_id, customer_id)
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"owner-research-profile:{owner_id}:{customer_id}",
        )
        active = await connection.fetchrow(
            "SELECT * FROM owner_research_automation_profiles WHERE owner_user_id=$1 "
            "AND customer_id IS NOT DISTINCT FROM $2 AND revoked_at IS NULL FOR UPDATE",
            owner_id, customer_id,
        )
        policy_json = policy.model_dump(mode="json")
        if active is not None:
            if (active["policy"] == policy_json
                    and active["consent_reference"] == consent_reference
                    and active["consent_text"] == consent_text
                    and active["research_autostart"] == research_autostart):
                return active["id"]
            if expected_profile_id != active["id"]:
                raise ResearchPolicyDenied("replace requires the exact active profile episode")
            await connection.execute(
                "UPDATE owner_research_automation_profiles SET revoked_at=now(),revoked_by=$2 "
                "WHERE id=$1", active["id"], owner_id,
            )
        elif expected_profile_id is not None:
            raise ResearchPolicyDenied("active profile changed or was revoked")
        version = await connection.fetchval(
            "SELECT COALESCE(max(version),0)+1 FROM owner_research_automation_profiles "
            "WHERE owner_user_id=$1 AND customer_id IS NOT DISTINCT FROM $2",
            owner_id, customer_id,
        )
        return await connection.fetchval(
            """INSERT INTO owner_research_automation_profiles(owner_user_id,customer_id,
            version,research_autostart,policy,policy_sha256,consent_reference,consent_text)
            VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8) RETURNING id""",
            owner_id, customer_id, version, research_autostart, policy_json,
            payload_sha256(policy_json), consent_reference, consent_text,
        )


async def revoke_research_profile(
    connection: asyncpg.Connection, *, owner_id: UUID, profile_id: UUID,
) -> bool:
    # Shares the profile row lock with snapshot/start validation.
    return await connection.execute(
        "UPDATE owner_research_automation_profiles SET revoked_at=now(),revoked_by=$2 "
        "WHERE id=$1 AND owner_user_id=$2 AND revoked_at IS NULL", profile_id, owner_id,
    ) == "UPDATE 1"


def item_search_issues(item: ProcurementItem) -> list[str]:
    issues = list(item.ambiguous_fields)
    if not item.name:
        issues.append("name_missing")
    if item.confidence < 0.6:
        issues.append("low_confidence")
    for index, requirement in enumerate(item.requirements):
        if requirement.ambiguous or requirement.confidence < 0.6:
            issues.append(f"requirements.{index}.ambiguous")
    return list(dict.fromkeys(issues))


@dataclass(frozen=True)
class IntakePolicyApplication:
    result: IntakeResult
    profile_id: UUID
    snapshot: dict[str, Any]
    source_hash: str
    applied_hash: str


async def apply_intake_research_policy(
    connection: asyncpg.Connection, *, owner_id: UUID, customer_id: UUID | None,
    case_id: UUID | None, result: IntakeResult,
) -> IntakePolicyApplication | None:
    """One bounded readiness reevaluation, before persisting/queuing initial intake.

    No retroactive enrollment of existing cases without a snapshot. Never resolve
    a customer or owner from document text, filenames, or delivery destinations.
    """
    previous = None
    if case_id is not None:
        previous = await connection.fetchrow(
            "SELECT profile_id FROM case_research_policy_snapshots WHERE case_id=$1 "
            "ORDER BY intake_version DESC LIMIT 1", case_id,
        )
        if previous is None:
            return None
    profile = await connection.fetchrow(
        """SELECT p.* FROM owner_research_automation_profiles p JOIN users u
        ON u.id=p.owner_user_id AND u.active WHERE p.owner_user_id=$1
        AND p.customer_id IS NOT DISTINCT FROM $2 AND p.research_autostart
        AND p.revoked_at IS NULL FOR SHARE OF p,u""", owner_id, customer_id,
    )
    if profile is None or (previous and previous["profile_id"] != profile["id"]):
        return None
    policy = ResearchAutomationPolicy.model_validate(profile["policy"])
    if payload_sha256(profile["policy"]) != profile["policy_sha256"]:
        raise ResearchPolicyDenied("profile policy hash drift")
    source = result.request
    defaults: dict[str, str] = {}
    conflicts: list[str] = []
    for field, expected in {"city": policy.ordered_cities[0],
                            "search_scope": policy.search_scope}.items():
        actual = getattr(source, field)
        if actual is None:
            defaults[field] = expected
        elif actual != expected:
            conflicts.append(field)
    # Keep explicit conflicting facts intact; manual research may use them.
    applied = ParsedProcurementRequest.model_validate(
        {**source.model_dump(mode="json"), **defaults}
    )
    report = evaluate_missing_blockers(
        applied, SearchScopePolicy.require_explicit(), allow_partial_items=True,
    )
    resolved = result.model_copy(update={
        "request": applied,
        "applied_defaults": {**result.applied_defaults, **defaults},
        "blockers": [blocker.model_dump(mode="json") for blocker in report.blockers],
        "clarifications": report.questions,
        "ready_to_search": report.ready_to_search,
    })
    source_hash = payload_sha256(source.model_dump(mode="json"))
    applied_hash = payload_sha256(applied.model_dump(mode="json"))
    snapshot = {
        "contract_version": 1, "profile_id": str(profile["id"]),
        "profile_version": profile["version"], "policy": profile["policy"],
        "profile_policy_sha256": profile["policy_sha256"],
        "consent_sha256": payload_sha256({"reference": profile["consent_reference"],
                                         "text": profile["consent_text"]}),
        "source_payload_sha256": source_hash, "applied_payload_sha256": applied_hash,
        "source_geography": {key: source.model_dump(mode="json")[key]
                             for key in ("city", "search_scope", "search_area_text")},
        "applied_defaults": defaults, "conflicts": conflicts,
        "automatic_start_eligible": report.ready_to_search and not conflicts,
        "items": [{"item_index": index, "issues": item_search_issues(item),
                   "source_reference": item.source_reference}
                  for index, item in enumerate(applied.items)],
    }
    return IntakePolicyApplication(resolved, profile["id"], snapshot, source_hash, applied_hash)


async def capture_research_snapshot(
    connection: asyncpg.Connection, *, application: IntakePolicyApplication,
    case_id: UUID, owner_id: UUID, customer_id: UUID | None, intake_version: int,
) -> UUID:
    parse = await connection.fetchrow(
        "SELECT id,normalized_payload FROM case_parse_versions "
        "WHERE case_id=$1 AND version=$2 AND status='applied' FOR SHARE",
        case_id, intake_version,
    )
    if parse is None or payload_sha256(parse["normalized_payload"]) != application.applied_hash:
        raise ResearchPolicyDenied("snapshot applied intake drift")
    snapshot = {**application.snapshot, "case_id": str(case_id),
                "owner_id": str(owner_id), "customer_id": str(customer_id) if customer_id else None,
                "parse_version_id": str(parse["id"]), "intake_version": intake_version}
    return await connection.fetchval(
        """INSERT INTO case_research_policy_snapshots(case_id,parse_version_id,intake_version,
        profile_id,owner_user_id,customer_id,source_payload_sha256,applied_payload_sha256,
        snapshot,snapshot_sha256) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10) RETURNING id""",
        case_id, parse["id"], intake_version, application.profile_id, owner_id, customer_id,
        application.source_hash, application.applied_hash, snapshot, payload_sha256(snapshot),
    )



async def _existing_case_adoption_facts(
    connection: asyncpg.Connection, *, case_id: UUID, owner_id: UUID,
) -> tuple[dict[str, Any], Any, Any, list[Any]]:
    case = await connection.fetchrow(
        """SELECT id,owner_user_id,customer_id,status,city,search_area_text,
        delivery_address FROM procurement_cases WHERE id=$1 AND owner_user_id=$2
        FOR SHARE""", case_id, owner_id,
    )
    if case is None or case["status"] != "ready":
        raise ResearchPolicyDenied("only this owner's ready case can be adopted")
    parse = await connection.fetchrow(
        """SELECT id,version,normalized_payload FROM case_parse_versions
        WHERE case_id=$1 AND status='applied' FOR SHARE""", case_id,
    )
    if parse is None:
        raise ResearchPolicyDenied("applied canonical parse is required")
    source_hash = payload_sha256(parse["normalized_payload"])
    request = ParsedProcurementRequest.model_validate(parse["normalized_payload"])
    report = evaluate_missing_blockers(
        request, SearchScopePolicy.require_explicit(), allow_partial_items=True,
    )
    items = await connection.fetch(
        """SELECT id,line_number,name,quantity,unit FROM request_items
        WHERE case_id=$1 ORDER BY line_number FOR SHARE""", case_id,
    )
    if len(items) != len(request.items):
        raise ResearchPolicyDenied("canonical parse and stable item projection differ")
    item_facts = []
    for index, (row, parsed_item) in enumerate(zip(items, request.items, strict=True)):
        if (row["line_number"] != index + 1 or row["name"] != parsed_item.name
                or row["quantity"] != parsed_item.quantity
                or row["unit"] != (parsed_item.unit or "")):
            raise ResearchPolicyDenied("canonical parse and stable item identity differ")
        item_facts.append({
            "item_id": str(row["id"]), "line_number": row["line_number"],
            "name": row["name"],
            "quantity": str(row["quantity"]) if row["quantity"] is not None else None,
            "unit": row["unit"], "item_index": index,
        })
    if not report.ready_to_search or request.city != case["city"]:
        raise ResearchPolicyDenied("case is not ready or its canonical geography differs")
    if await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM research_runs WHERE case_id=$1)", case_id,
    ):
        raise ResearchPolicyDenied("case already has research runs")
    if await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM case_research_policy_snapshots WHERE case_id=$1)",
        case_id,
    ):
        raise ResearchPolicyDenied("case already has an owner research snapshot")
    profile = await connection.fetchrow(
        """SELECT p.id,p.version,p.policy,p.policy_sha256,p.consent_reference,p.consent_text
        FROM owner_research_automation_profiles p JOIN users u
        ON u.id=p.owner_user_id AND u.active
        WHERE p.owner_user_id=$1 AND p.customer_id IS NOT DISTINCT FROM $2
          AND p.research_autostart AND p.revoked_at IS NULL FOR SHARE OF p,u""",
        owner_id, case["customer_id"],
    )
    if profile is None or payload_sha256(profile["policy"]) != profile["policy_sha256"]:
        raise ResearchPolicyDenied("an active, valid owner consent profile is required")
    policy = ResearchAutomationPolicy.model_validate(profile["policy"])
    if (request.city != policy.ordered_cities[0]
            or request.search_scope != policy.search_scope):
        raise ResearchPolicyDenied("canonical geography must match the first owner policy tier")
    facts = {
        "case_id": str(case_id), "owner_id": str(owner_id),
        "customer_id": str(case["customer_id"]) if case["customer_id"] else None,
        "case_status": case["status"], "parse_version_id": str(parse["id"]),
        "intake_version": parse["version"], "source_payload_sha256": source_hash,
        "profile_id": str(profile["id"]), "profile_version": profile["version"],
        "profile_policy_sha256": profile["policy_sha256"],
        "consent_sha256": payload_sha256({"reference": profile["consent_reference"],
                                           "text": profile["consent_text"]}),
        "city": request.city, "search_scope": request.search_scope,
        "items": item_facts,
    }
    facts["preflight_sha256"] = payload_sha256(facts)
    return facts, case, (parse, profile, request, report), items


async def preflight_existing_case_adoption(
    connection: asyncpg.Connection, *, case_id: UUID, owner_id: UUID,
) -> dict[str, Any]:
    """Read-only preflight: never rewrites case, parse, or request-item projections."""
    facts, _case, details, _items = await _existing_case_adoption_facts(
        connection, case_id=case_id, owner_id=owner_id,
    )
    parse, profile, request, report = details
    return {
        "case_id": str(case_id), "intake_version": parse["version"],
        "source_payload_sha256": facts["source_payload_sha256"],
        "preflight_sha256": facts["preflight_sha256"],
        "profile_id": str(profile["id"]), "profile_policy_sha256": profile["policy_sha256"],
        "city": request.city, "search_scope": request.search_scope,
        "ordered_cities": list(
            ResearchAutomationPolicy.model_validate(profile["policy"]).ordered_cities
        ),
        "ready_to_search": report.ready_to_search, "items": facts["items"],
    }


async def adopt_existing_case_for_research(
    connection: asyncpg.Connection, *, case_id: UUID, owner_id: UUID,
    expected_preflight_sha256: str, owner_assertion: str, adoption_reference: str,
) -> UUID:
    """Owner-attested opt-in for one unchanged imported case, without rewriting source rows."""
    if (not re.fullmatch(r"[0-9a-f]{64}", expected_preflight_sha256)
            or not owner_assertion.strip() or len(owner_assertion) > 2000
            or not adoption_reference.strip() or len(adoption_reference) > 500):
        raise ResearchPolicyDenied("exact preflight and owner adoption attestation are required")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"owner-case-adopt:{case_id}",
        )
        prior = await connection.fetchrow(
            """SELECT id,snapshot FROM case_research_policy_snapshots
            WHERE case_id=$1 AND owner_user_id=$2""", case_id, owner_id,
        )
        if prior is not None:
            adoption = prior["snapshot"].get("adoption", {})
            if adoption.get("preflight_sha256") == expected_preflight_sha256:
                return prior["id"]
            raise ResearchPolicyDenied("case already has a different owner research snapshot")
        facts, case, details, _items = await _existing_case_adoption_facts(
            connection, case_id=case_id, owner_id=owner_id,
        )
        if facts["preflight_sha256"] != expected_preflight_sha256:
            raise ResearchPolicyDenied("case or consent changed; rerun the read-only preflight")
        parse, profile, request, report = details
        applied_hash = facts["source_payload_sha256"]
        snapshot = {
            "contract_version": 1, "profile_id": str(profile["id"]),
            "profile_version": profile["version"], "policy": profile["policy"],
            "profile_policy_sha256": profile["policy_sha256"],
            "consent_sha256": facts["consent_sha256"],
            "source_payload_sha256": applied_hash, "applied_payload_sha256": applied_hash,
            "source_geography": {key: request.model_dump(mode="json")[key]
                                 for key in ("city", "search_scope", "search_area_text")},
            "applied_defaults": {}, "conflicts": [],
            "automatic_start_eligible": report.ready_to_search,
            "items": [{"item_index": row["item_index"], "item_id": row["item_id"],
                       "issues": item_search_issues(request.items[row["item_index"]]),
                       "source_reference": request.items[row["item_index"]].source_reference}
                      for row in facts["items"]],
            "adoption": {"preflight_sha256": expected_preflight_sha256,
                         "owner_assertion": owner_assertion.strip(),
                         "adoption_reference": adoption_reference.strip(),
                         "item_ids_sha256": payload_sha256({"items": facts["items"]})},
        }
        snapshot_id = await connection.fetchval(
            """INSERT INTO case_research_policy_snapshots(case_id,parse_version_id,intake_version,
            profile_id,owner_user_id,customer_id,source_payload_sha256,applied_payload_sha256,
            snapshot,snapshot_sha256) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)
            RETURNING id""",
            case_id, parse["id"], parse["version"], profile["id"], owner_id,
            case["customer_id"], applied_hash, applied_hash, snapshot, payload_sha256(snapshot),
        )
        from procurement_bot.queue import enqueue_job, stable_key
        await enqueue_job(
            connection, kind="prepare_research",
            payload={"case_id": str(case_id), "intake_version": parse["version"]},
            idempotency_key=stable_key(
                "owner-case-adoption", case_id, parse["id"], expected_preflight_sha256,
            ),
        )
        await connection.execute(
            """INSERT INTO workflow_events(case_id,event_type,actor_type,actor_id,
            idempotency_key,data) VALUES(
            $1,'owner_research_case_adopted','user',$2,$3,$4::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING""",
            case_id, str(owner_id),
            stable_key("owner-case-adoption-event", case_id, parse["id"]),
            {"snapshot_id": str(snapshot_id), "preflight_sha256": expected_preflight_sha256,
             "source_payload_sha256": applied_hash, "profile_id": str(profile["id"])},
        )
        return snapshot_id


async def load_valid_snapshot(
    connection: asyncpg.Connection, *, snapshot_id: UUID, case_id: UUID,
) -> asyncpg.Record:
    row = await connection.fetchrow(
        """SELECT s.*,p.policy,p.policy_sha256,p.version AS profile_version,
        p.consent_reference,p.consent_text,p.revoked_at,p.research_autostart,
        p.owner_user_id AS profile_owner,p.customer_id AS profile_customer,
        c.owner_user_id AS case_owner,c.customer_id AS case_customer,
        v.normalized_payload,v.status AS parse_status,v.version AS parse_version,u.active
        FROM case_research_policy_snapshots s
        JOIN owner_research_automation_profiles p ON p.id=s.profile_id
        JOIN procurement_cases c ON c.id=s.case_id JOIN users u ON u.id=c.owner_user_id
        JOIN case_parse_versions v ON v.id=s.parse_version_id
        WHERE s.id=$1 AND s.case_id=$2 FOR SHARE OF s,p,c,v,u""", snapshot_id, case_id,
    )
    if row is None:
        raise ResearchPolicyDenied("research policy snapshot absent or wrong case")
    snap = row["snapshot"]
    if (not row["active"] or row["revoked_at"] is not None or not row["research_autostart"]
            or row["owner_user_id"] != row["case_owner"]
            or row["owner_user_id"] != row["profile_owner"]
            or row["customer_id"] != row["case_customer"]
            or row["customer_id"] != row["profile_customer"]
            or row["parse_status"] != "applied" or row["parse_version"] != row["intake_version"]
            or payload_sha256(row["normalized_payload"]) != row["applied_payload_sha256"]
            or payload_sha256(snap) != row["snapshot_sha256"]
            or payload_sha256(row["policy"]) != row["policy_sha256"]
            or snap["profile_policy_sha256"] != row["policy_sha256"]
            or snap["profile_version"] != row["profile_version"]
            or snap["consent_sha256"] != payload_sha256({"reference": row["consent_reference"],
                                                        "text": row["consent_text"]})
            or not snap["automatic_start_eligible"]):
        raise ResearchPolicyDenied("research policy revoked, mismatched, or intake hash drift")
    ResearchAutomationPolicy.model_validate(row["policy"])
    return row


def build_profile_research_plan(
    request: ParsedProcurementRequest, *, snapshot: dict[str, Any],
    sourcing_adjustments: dict[int, Decimal | None] | None = None,
):
    """Research clear lines with original indices and explicit owner quantities."""
    from procurement_bot.research import build_research_plan
    from procurement_bot.research_repository import recompute_plan_sha256

    adjustments = sourcing_adjustments or {}
    selected: list[tuple[int, ProcurementItem]] = []
    for index, original in enumerate(request.items):
        if item_search_issues(original):
            continue
        if index in adjustments:
            adjustment = adjustments[index]
            if adjustment is None or original.quantity is None:
                continue
            remaining = original.quantity - adjustment
            if remaining <= 0:
                continue
            selected.append((index, original.model_copy(update={"quantity": remaining})))
        else:
            selected.append((index, original))
    if not selected:
        raise ResearchPolicyDenied("no clear items remain available for automatic research")
    if request.city != snapshot["policy"]["ordered_cities"][0]:
        raise ResearchPolicyDenied("initial plan must use exactly the first sourcing city")
    indices = [index for index, _ in selected]
    plan = build_research_plan(
        request.model_copy(update={"items": [item for _, item in selected]}),
        include_olx=True,
    )
    plan = plan.model_copy(update={"source_plans": [
        branch.model_copy(update={"item_index": indices[branch.item_index]})
        for branch in plan.source_plans
    ]})
    return plan.model_copy(update={"plan_sha256": recompute_plan_sha256(plan)})


async def bind_research_policy(
    connection: asyncpg.Connection, *, snapshot_id: UUID, case_id: UUID,
    run_id: UUID, plan_sha256: str,
) -> dict[str, str]:
    row = await load_valid_snapshot(connection, snapshot_id=snapshot_id, case_id=case_id)
    run = await connection.fetchrow(
        "SELECT case_id,search_scope FROM research_runs WHERE id=$1 FOR SHARE", run_id,
    )
    from procurement_bot.research_workflow import ResearchStartDenied, _plan_hash
    if run is None or run["case_id"] != case_id:
        raise ResearchPolicyDenied("research run does not belong to the snapshotted case")
    try:
        actual_plan_sha256 = _plan_hash(run["search_scope"])
    except ResearchStartDenied as exc:
        raise ResearchPolicyDenied("research run has no valid persisted plan") from exc
    scope = run["search_scope"]
    if isinstance(scope, str):
        import json
        scope = json.loads(scope)
    plan = scope.get("plan") if isinstance(scope, dict) else None
    first_city = row["snapshot"]["policy"]["ordered_cities"][0]
    if (actual_plan_sha256 != plan_sha256 or not isinstance(plan, dict)
            or plan.get("city") != first_city):
        raise ResearchPolicyDenied("research plan hash or initial city differs from policy")
    instruction_ids = scope.get("sourcing_instruction_ids", [])
    if not isinstance(instruction_ids, list) or any(
        not isinstance(value, str) for value in instruction_ids
    ):
        raise ResearchPolicyDenied("research sourcing instruction binding is malformed")
    binding = {"snapshot_id": str(snapshot_id), "snapshot_sha256": row["snapshot_sha256"],
               "case_id": str(case_id), "research_run_id": str(run_id),
               "plan_sha256": plan_sha256, "parse_version_id": str(row["parse_version_id"]),
               "applied_payload_sha256": row["applied_payload_sha256"],
               "sourcing_instruction_ids": instruction_ids}
    digest = payload_sha256(binding)
    authorization = await connection.fetchrow(
        """INSERT INTO research_policy_authorizations(snapshot_id,research_run_id,plan_sha256,
        binding,binding_sha256) VALUES($1,$2,$3,$4::jsonb,$5) ON CONFLICT DO NOTHING
        RETURNING id,binding_sha256""", snapshot_id, run_id, plan_sha256, binding, digest,
    )
    if authorization is None:
        authorization = await connection.fetchrow(
            "SELECT id,binding_sha256 FROM research_policy_authorizations "
            "WHERE snapshot_id=$1 AND research_run_id=$2", snapshot_id, run_id,
        )
    if authorization is None or authorization["binding_sha256"] != digest:
        raise ResearchPolicyDenied("policy snapshot already bound to a different plan")
    return {"policy_authorization_id": str(authorization["id"]),
            "policy_authorization_sha256": digest}


async def validate_research_policy_binding(
    connection: asyncpg.Connection, *, authorization_id: UUID, authorization_sha256: str,
    case_id: UUID, run_id: UUID, plan_sha256: str, approved_by: UUID,
) -> asyncpg.Record:
    auth = await connection.fetchrow(
        "SELECT * FROM research_policy_authorizations WHERE id=$1 FOR SHARE", authorization_id,
    )
    if (auth is None or auth["research_run_id"] != run_id
            or auth["plan_sha256"] != plan_sha256
            or auth["binding_sha256"] != authorization_sha256
            or payload_sha256(auth["binding"]) != authorization_sha256):
        raise ResearchPolicyDenied("research policy authorization/plan hash mismatch")
    row = await load_valid_snapshot(connection, snapshot_id=auth["snapshot_id"], case_id=case_id)
    run = await connection.fetchrow(
        "SELECT case_id,search_scope FROM research_runs WHERE id=$1 FOR SHARE", run_id,
    )
    from procurement_bot.research_workflow import ResearchStartDenied, _plan_hash
    if run is None or run["case_id"] != case_id:
        raise ResearchPolicyDenied("authorized run no longer belongs to the snapshotted case")
    try:
        actual_plan_sha256 = _plan_hash(run["search_scope"])
    except ResearchStartDenied as exc:
        raise ResearchPolicyDenied("authorized run plan is invalid") from exc
    if actual_plan_sha256 != plan_sha256:
        raise ResearchPolicyDenied("authorized run plan changed after binding")
    scope = run["search_scope"]
    if isinstance(scope, str):
        import json
        scope = json.loads(scope)
    instruction_ids = scope.get("sourcing_instruction_ids", [])
    expected = {"snapshot_id": str(row["id"]), "snapshot_sha256": row["snapshot_sha256"],
                "case_id": str(case_id), "research_run_id": str(run_id),
                "plan_sha256": plan_sha256, "parse_version_id": str(row["parse_version_id"]),
                "applied_payload_sha256": row["applied_payload_sha256"],
                "sourcing_instruction_ids": instruction_ids}
    if auth["binding"] != expected or approved_by != row["owner_user_id"]:
        raise ResearchPolicyDenied("research policy owner or exact binding mismatch")
    return row


async def autostart_snapshotted_research(
    pool: asyncpg.Pool, *, case_id: UUID, intake_version: int,
) -> bool:
    """Prepare/request/approve/start atomically through the canonical native helpers.

    Return false for unsnapshotted/manual or explicitly conflicting source cases.
    Denied stale policy state rolls the whole attempt back for the manual caller.
    """
    from procurement_bot.approvals import approve_request, request_approval
    from procurement_bot.queue import enqueue_outbox, stable_key
    from procurement_bot.research_repository import persist_research_plan
    from procurement_bot.research_workflow import start_approved_research

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"owner-research-start:{case_id}",
        )
        snapshot = await connection.fetchrow(
            "SELECT id,snapshot FROM case_research_policy_snapshots "
            "WHERE case_id=$1 AND intake_version=$2", case_id, intake_version,
        )
        if snapshot is None or not snapshot["snapshot"]["automatic_start_eligible"]:
            return False
        valid = await load_valid_snapshot(connection, snapshot_id=snapshot["id"], case_id=case_id)
        request = ParsedProcurementRequest.model_validate(valid["normalized_payload"])
        instructions = await connection.fetch(
            """SELECT d.id,d.request_item_id,d.disposition,d.quantity,i.line_number,
            i.name,i.quantity AS original_quantity,i.unit
            FROM item_sourcing_dispositions d JOIN request_items i
            ON i.id=d.request_item_id AND i.case_id=d.case_id
            WHERE d.case_id=$1 AND d.owner_user_id=$2 AND d.applies_to_sourcing
              AND d.source_parse_version_id=$3 AND d.source_payload_sha256=$4
            ORDER BY d.request_item_id""",
            case_id, valid["owner_user_id"], valid["parse_version_id"],
            valid["applied_payload_sha256"],
        )
        adjustments: dict[int, Decimal | None] = {}
        instruction_ids: list[str] = []
        for instruction in instructions:
            index = instruction["line_number"] - 1
            if (index < 0 or index >= len(request.items)
                    or request.items[index].name != instruction["name"]
                    or request.items[index].quantity != instruction["original_quantity"]
                    or request.items[index].unit != instruction["unit"]):
                raise ResearchPolicyDenied("source-bound item identity drifted before planning")
            adjustments[index] = (
                None if instruction["disposition"] == "skip" else instruction["quantity"]
            )
            instruction_ids.append(str(instruction["id"]))
        plan = build_profile_research_plan(
            request, snapshot=valid["snapshot"], sourcing_adjustments=adjustments,
        )
        persisted = await persist_research_plan(
            connection, case_id=case_id, plan=plan, requested_by=valid["owner_user_id"],
            sourcing_instruction_ids=instruction_ids,
        )
        binding = await bind_research_policy(
            connection, snapshot_id=valid["id"], case_id=case_id,
            run_id=persisted.id, plan_sha256=plan.plan_sha256,
        )
        payload = {"case_id": str(case_id), "research_run_id": str(persisted.id),
                   "plan_sha256": plan.plan_sha256, **binding}
        # request_approval intentionally reissues consumed grants for ordinary
        # manual actions. Here a consumed exact start must resolve to its old ID.
        approval_id = await connection.fetchval(
            "SELECT id FROM action_approvals WHERE case_id=$1 AND action_type='start_research' "
            "AND payload_sha256=$2 AND status IN ('requested','approved','consumed') "
            "ORDER BY requested_at DESC LIMIT 1", case_id, payload_sha256(payload),
        )
        if approval_id is None:
            approval_id = await request_approval(
                connection, case_id=case_id, action_type="start_research", payload=payload,
                requested_by=valid["owner_user_id"],
            )
        await approve_request(connection, approval_id=approval_id,
                              approved_by=valid["owner_user_id"])
        await start_approved_research(connection, approval_id=approval_id)
        telegram_id = await connection.fetchval("SELECT telegram_id FROM users WHERE id=$1",
                                                valid["owner_user_id"])
        await enqueue_outbox(
            connection, event_type="telegram.send_message",
            payload={"chat_id": int(telegram_id), "text":
                     "Поиск запущен по вашему согласию на автоматическое исследование. "
                     "Разрешение на сообщения поставщикам оформляется отдельно."},
            idempotency_key=stable_key("telegram.research-plan", persisted.id),
        )
        return True


async def guard_profile_research_work(
    pool: asyncpg.Pool, *, case_id: UUID | None = None, run_id: UUID | None = None,
) -> bool:
    """Pause opted-in work on the next native job boundary after revocation/drift.

    Manual work retains its existing guards. An already running provider request
    is not retroactively interrupted; later queued batches/ticks are fenced.
    """
    from procurement_bot.queue import stable_key
    from procurement_bot.research_workflow import ResearchStartDenied, _plan_hash

    async with pool.acquire() as connection, connection.transaction():
        runtime = await connection.fetchrow(
            """SELECT rt.case_id,rt.research_run_id,rt.research_policy_snapshot_id,
            a.id AS authorization_id,a.binding_sha256,a.plan_sha256,r.search_scope,
            c.owner_user_id FROM case_pipeline_runtime rt
            JOIN procurement_cases c ON c.id=rt.case_id
            JOIN research_runs r ON r.id=COALESCE($2::uuid,rt.research_run_id)
            LEFT JOIN research_policy_authorizations a ON a.research_run_id=r.id
            WHERE ($1::uuid IS NULL OR rt.case_id=$1)
            AND ($2::uuid IS NULL OR r.case_id=rt.case_id AND r.id=$2)
            AND rt.research_policy_snapshot_id IS NOT NULL""", case_id, run_id,
        )
        if runtime is None:
            return True
        try:
            if runtime["authorization_id"] is None:
                raise ResearchPolicyDenied("profile runtime has no exact run authorization")
            if _plan_hash(runtime["search_scope"]) != runtime["plan_sha256"]:
                raise ResearchPolicyDenied("research policy runtime plan drift")
            await validate_research_policy_binding(
                connection, authorization_id=runtime["authorization_id"],
                authorization_sha256=runtime["binding_sha256"],
                case_id=runtime["case_id"], run_id=run_id or runtime["research_run_id"],
                plan_sha256=runtime["plan_sha256"], approved_by=runtime["owner_user_id"],
            )
        except (ResearchPolicyDenied, ResearchStartDenied) as exc:
            await connection.execute(
                "UPDATE case_pipeline_runtime SET paused=true, "
                "wake_reason='research_policy_review', updated_at=now() "
                "WHERE case_id=$1", runtime["case_id"],
            )
            await connection.execute(
                """INSERT INTO workflow_events(case_id,event_type,actor_type,actor_id,
                idempotency_key,data) VALUES($1,'research_policy_paused','worker',
                'owner-research-guard',$2,$3::jsonb) ON CONFLICT(idempotency_key) DO NOTHING""",
                runtime["case_id"], stable_key("workflow.research-policy-paused",
                                             runtime["authorization_id"], runtime["case_id"]),
                {"reason": str(exc), "authorization_mode": "owner_research_profile"},
            )
            return False
    return True


async def record_sourcing_disposition(
    connection: asyncpg.Connection, *, owner_id: UUID, case_id: UUID, item_id: UUID,
    artifact_id: UUID | None, artifact_sha256: str,
    disposition: Literal["bought", "skip", "reduce_quantity"],
    quantity: Decimal | None, unit: str, owner_assertion: str, exact_match_rationale: str,
    idempotency_key: str, source_kind: Literal["telegram_artifact", "operator_attestation"]
    = "telegram_artifact", source_reference: str | None = None,
    applies_to_sourcing: bool = False, expected_source_payload_sha256: str | None = None,
) -> UUID:
    """Record owner evidence; only an explicit parse-bound instruction changes plan scope.

    Operator-attested hashes are owner-declared, not independently file-verified.
    Neither evidence nor sourcing instructions create accounting records.
    """
    if disposition not in {"bought", "skip", "reduce_quantity"}:
        raise ResearchPolicyDenied("unsupported sourcing disposition")
    if not all(value.strip() for value in (unit, owner_assertion, exact_match_rationale,
                                           idempotency_key)):
        raise ResearchPolicyDenied("verbatim assertion, exact rationale, unit and key required")
    if (not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
            or len(owner_assertion) > 4000 or len(exact_match_rationale) > 2000):
        raise ResearchPolicyDenied("invalid source hash or oversized owner evidence")
    if source_kind == "telegram_artifact":
        if artifact_id is None or source_reference is not None:
            raise ResearchPolicyDenied("Telegram evidence requires its native artifact id")
    elif source_kind == "operator_attestation":
        if (artifact_id is not None or not source_reference or len(source_reference) > 500
                or not source_reference.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in source_reference)):
            raise ResearchPolicyDenied(
                "operator evidence requires a plain source reference and no artifact id"
            )
    else:
        raise ResearchPolicyDenied("unsupported sourcing evidence kind")
    async with connection.transaction():
        item = await connection.fetchrow(
            """SELECT i.id,i.line_number,i.name,i.quantity,i.unit,c.customer_id
            FROM request_items i JOIN procurement_cases c ON c.id=i.case_id
            JOIN users u ON u.id=c.owner_user_id AND u.active
            WHERE i.id=$1 AND c.id=$2 AND c.owner_user_id=$3 FOR SHARE OF i,c,u""",
            item_id, case_id, owner_id,
        )
        artifact = None
        if source_kind == "telegram_artifact":
            artifact = await connection.fetchval(
                """SELECT a.sha256 FROM source_artifacts a JOIN incoming_attachments t
                ON t.id=a.attachment_id JOIN messages m ON m.id=t.message_id
                WHERE a.id=$1 AND m.user_id=$2 AND (m.id IN
                (SELECT source_message_id FROM procurement_cases WHERE id=$3) OR EXISTS
                (SELECT 1 FROM dialogue_turns d WHERE d.message_id=m.id AND d.case_id=$3))
                FOR SHARE OF a,m""", artifact_id, owner_id, case_id,
            )
        if item is None or (source_kind == "telegram_artifact" and artifact != artifact_sha256):
            raise ResearchPolicyDenied("exact case item and source owner/hash binding required")
        if (unit != item["unit"] or quantity is not None and
                (not quantity.is_finite() or quantity <= 0 or item["quantity"] is None
                 or quantity > item["quantity"])):
            raise ResearchPolicyDenied("exact compatible unit and bounded quantity required")
        if disposition != "skip" and quantity is None:
            raise ResearchPolicyDenied("bought/reduction quantity required")

        parse_version_id = None
        source_payload_sha256 = None
        if applies_to_sourcing:
            if not isinstance(expected_source_payload_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_source_payload_sha256,
            ):
                raise ResearchPolicyDenied("current canonical parse hash is required")
            parse = await connection.fetchrow(
                """SELECT id,normalized_payload FROM case_parse_versions
                WHERE case_id=$1 AND status='applied' FOR SHARE""", case_id,
            )
            if (parse is None or payload_sha256(parse["normalized_payload"])
                    != expected_source_payload_sha256):
                raise ResearchPolicyDenied(
                    "canonical parse changed; refresh the case before applying"
                )
            parsed = ParsedProcurementRequest.model_validate(parse["normalized_payload"])
            index = item["line_number"] - 1
            if (index < 0 or index >= len(parsed.items)
                    or parsed.items[index].name != item["name"]
                    or parsed.items[index].quantity != item["quantity"]
                    or parsed.items[index].unit != item["unit"]):
                raise ResearchPolicyDenied("stable item mapping differs from canonical source")
            if item_search_issues(parsed.items[index]):
                raise ResearchPolicyDenied(
                    "ambiguous or candidate items cannot change automatic sourcing scope"
                )
            parse_version_id = parse["id"]
            source_payload_sha256 = expected_source_payload_sha256
        elif expected_source_payload_sha256 is not None:
            raise ResearchPolicyDenied(
                "parse hash is valid only for an explicit sourcing instruction"
            )

        facts = {"case_id": str(case_id), "item_id": str(item_id),
                 "source_kind": source_kind,
                 "source_sha256_basis": "telegram_verified" if source_kind == "telegram_artifact"
                 else "owner_declared",
                 "artifact_id": str(artifact_id) if artifact_id else None,
                 "source_reference": source_reference, "artifact_sha256": artifact_sha256,
                 "disposition": disposition,
                 "quantity": str(quantity) if quantity is not None else None,
                 "unit": unit, "owner_assertion": owner_assertion,
                 "exact_match_rationale": exact_match_rationale, "owner_id": str(owner_id),
                 "applies_to_sourcing": applies_to_sourcing,
                 "source_parse_version_id": str(parse_version_id) if parse_version_id else None,
                 "source_payload_sha256": source_payload_sha256}
        digest = payload_sha256(facts)
        row = await connection.fetchrow(
            """INSERT INTO item_sourcing_dispositions(case_id,request_item_id,owner_user_id,
            source_kind,source_artifact_id,source_reference,artifact_sha256,source_sha256_basis,
            source_parse_version_id,source_payload_sha256,applies_to_sourcing,disposition,quantity,
            unit,owner_assertion,exact_match_rationale,evidence_sha256,idempotency_key)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT DO NOTHING RETURNING id,evidence_sha256""",
            case_id, item_id, owner_id, source_kind, artifact_id, source_reference, artifact_sha256,
            facts["source_sha256_basis"], parse_version_id, source_payload_sha256,
            applies_to_sourcing, disposition, quantity, unit, owner_assertion,
            exact_match_rationale, digest, idempotency_key,
        )
        if row is None:
            row = await connection.fetchrow(
                "SELECT id,evidence_sha256 FROM item_sourcing_dispositions "
                "WHERE idempotency_key=$1", idempotency_key,
            )
        if row is None or row["evidence_sha256"] != digest:
            raise ResearchPolicyDenied("sourcing disposition replay differs")
        return row["id"]

def _source_pointer(value: str | None) -> str | None:
    # Extraction locators are useful; filesystem locations and credential URLs
    # are not owner-facing source pointers. Original artifacts stay server-side.
    if value and not value.startswith(("/", "~", "\\")) and "://" not in value:
        if len(value) > 2 and value[1:3] in {":/", ":\\"}:
            return None
        return value
    return None


async def load_case_source_visibility(
    connection: asyncpg.Connection, *, case_id: UUID, owner_id: UUID,
) -> dict[str, Any]:
    """Minimal read projection, including manual cases; never return a raw request."""
    case = await connection.fetchrow(
        """SELECT c.id,c.city,c.delivery_address,c.status,p.normalized_payload,p.version
        FROM procurement_cases c LEFT JOIN case_parse_versions p
        ON p.case_id=c.id AND p.status='applied' WHERE c.id=$1 AND c.owner_user_id=$2""",
        case_id, owner_id,
    )
    if case is None:
        raise ResearchPolicyDenied("case not found for this owner")
    request = (ParsedProcurementRequest.model_validate(case["normalized_payload"])
               if case["normalized_payload"] is not None else None)
    items = await connection.fetch(
        """SELECT i.id,i.line_number,i.name,i.quantity,i.unit,i.status,
        (SELECT sum(p.purchased_quantity) FROM purchase_items p
         WHERE p.request_item_id=i.id) AS purchased_quantity
        FROM request_items i WHERE i.case_id=$1 ORDER BY i.line_number""", case_id,
    )
    snapshot = await connection.fetchrow(
        """SELECT s.id,s.snapshot,p.revoked_at FROM case_research_policy_snapshots s
        JOIN owner_research_automation_profiles p ON p.id=s.profile_id
        WHERE s.case_id=$1 AND s.intake_version=$2""", case_id, case["version"],
    )
    dispositions = await connection.fetch(
        """SELECT id,request_item_id,source_kind,source_artifact_id,source_reference,
        artifact_sha256,source_sha256_basis,source_parse_version_id,source_payload_sha256,
        applies_to_sourcing,disposition,quantity,unit,owner_assertion,exact_match_rationale,created_at
        FROM item_sourcing_dispositions WHERE case_id=$1 AND owner_user_id=$2
        ORDER BY created_at,id""", case_id, owner_id,
    )
    runtime = await connection.fetchrow(
        "SELECT active_city_tier,report_checkpoint_at FROM case_pipeline_runtime WHERE case_id=$1",
        case_id,
    )
    authorized = await connection.fetchrow(
        """SELECT a.binding FROM research_policy_authorizations a
        JOIN research_runs r ON r.id=a.research_run_id
        WHERE r.case_id=$1 ORDER BY r.created_at DESC LIMIT 1""", case_id,
    )
    bound_instruction_ids = set()
    if authorized is not None:
        binding = authorized["binding"]
        bound_instruction_ids = set(binding.get("sourcing_instruction_ids", []))
    output_items = []
    for row in items:
        index = row["line_number"] - 1
        normalized = (request.items[index] if request is not None
                      and 0 <= index < len(request.items) else None)
        aligned = normalized is not None and normalized.name == row["name"]
        issues = item_search_issues(normalized) if aligned else ["source_projection_unavailable"]
        evidence = [dict(fact) for fact in dispositions if fact["request_item_id"] == row["id"]]
        stage = "ready_for_search" if not issues and case["status"] not in {
            "draft", "needs_clarification"} else "needs_review"
        if row["purchased_quantity"] is not None and row["quantity"] is not None and (
            row["purchased_quantity"] >= row["quantity"]
        ):
            stage = "bought"
        elif any(fact["applies_to_sourcing"] and fact["disposition"] == "skip"
                 for fact in evidence):
            stage = "owner_excluded_from_sourcing"
        elif evidence:
            stage = "evidence_only"
        output_items.append({
            "item_id": str(row["id"]), "line_number": row["line_number"],
            "item_index": index, "stage": stage, "issues": issues,
            "source_reference": _source_pointer(normalized.source_reference) if aligned else None,
            "requirements": [{"name": req.name, "value": req.expected_value,
                              "ambiguous": req.ambiguous or req.confidence < 0.6,
                              "source_reference": _source_pointer(req.source_reference)}
                             for req in normalized.requirements] if aligned else [],
            "sourcing_evidence": [
                {**fact, "source_reference": _source_pointer(fact["source_reference"]),
                 "source_sha256": fact["artifact_sha256"],
                 "actual_skip": bool(fact["applies_to_sourcing"]
                                      and fact["disposition"] == "skip")}
                for fact in evidence
            ],
            "disposition_applied_to_plan": any(
                str(fact["id"]) in bound_instruction_ids for fact in evidence
            ),
        })
    return {
        "case_id": str(case_id), "intake_version": case["version"],
        "source_payload_sha256": (
            payload_sha256(case["normalized_payload"])
            if case["normalized_payload"] is not None else None
        ),
        "warnings": request.warnings if request else [],
        "ambiguous_fields": request.ambiguous_fields if request else [],
        "sourcing_city": case["city"], "delivery_destination": case["delivery_address"],
        "ordered_sourcing_cities": snapshot["snapshot"]["policy"]["ordered_cities"]
        if snapshot else [],
        "research_consent": "revoked" if snapshot and snapshot["revoked_at"] else
        "profile_bound" if snapshot else "manual",
        "active_city_tier": runtime["active_city_tier"] if runtime else None,
        "report_checkpoint_at": runtime["report_checkpoint_at"] if runtime else None,
        "checkpoint_dispatch_integrated": False,
        "sequential_cities_integrated": False,
        "items": output_items,
    }
