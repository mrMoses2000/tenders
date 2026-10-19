"""Persisted, opt-in city discovery. All I/O here is native local persistence.

Provider calls, task leases and contact authorization remain with native workers.
The immutable admission binds an ephemeral plan to original source item identities.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.approvals import approve_request, payload_sha256, request_approval
from procurement_bot.cases import _requirement_text
from procurement_bot.city_tier_policy import City
from procurement_bot.intake import ParsedProcurementRequest
from procurement_bot.offer_comparison import compare_offer
from procurement_bot.offer_technical import load_case_technical_reviews, technical_states
from procurement_bot.owner_autonomy import (
    ResearchPolicyDenied,
    item_search_issues,
    load_valid_snapshot,
)
from procurement_bot.pipeline_runtime import RESEARCH_SCHEDULER_LOCK_KEY
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.research import ResearchPlan, build_research_plan
from procurement_bot.research_repository import persist_research_plan, recompute_plan_sha256

CITIES = ("Тараз", "Шымкент", "Алматы")
_ALIASES = dict(zip(CITIES, City, strict=True)) | {city.value: city for city in City}
_EXHAUSTED = {"source_exhausted", "own_database_reused", "semantic_intents_exhausted"}
LOGGER = logging.getLogger(__name__)


def enum_city(value: str) -> City:
    """The only alias boundary; no normalization of source or policy values."""
    try:
        return _ALIASES[value]
    except (KeyError, TypeError) as exc:
        raise ResearchPolicyDenied("unknown sourcing city") from exc


def city_route(snapshot: dict[str, Any]) -> bool:
    return snapshot["policy"]["ordered_cities"] == list(CITIES)


def validate_city_budget(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"target_suppliers", "max_batches"}:
        raise ResearchPolicyDenied("city discovery budget is missing or malformed")
    target, batches = value["target_suppliers"], value["max_batches"]
    if (type(target) is not int or not 1 <= target <= 100
            or type(batches) is not int or not 1 <= batches <= 100):
        raise ResearchPolicyDenied("city discovery budget is outside native limits")
    return {"target_suppliers": target, "max_batches": batches}


def city_budget_from_executor(executor: Any) -> dict[str, int] | None:
    target = getattr(executor, "target_suppliers", None)
    batches = getattr(executor, "max_batches", None)
    if type(target) is not int or type(batches) is not int:
        return None
    # The existing staged semantic-query executor may expand its native cap to 80.
    if getattr(executor, "profile_extractor", None) is not None:
        batches = max(batches, 80)
    try:
        return validate_city_budget({"target_suppliers": target, "max_batches": batches})
    except ResearchPolicyDenied:
        return None


def city_plan(request: ParsedProcurementRequest, tier: int, indices: list[int]) -> ResearchPlan:
    if type(tier) is not int or not 0 <= tier < len(CITIES):
        raise ResearchPolicyDenied("city tier is outside the authorized route")
    if (
        request.city != CITIES[0]
        or not indices
        or indices != sorted(set(indices))
        or any(type(i) is not int or i < 0 or i >= len(request.items) for i in indices)
        or any(item_search_issues(request.items[i]) for i in indices)
    ):
        raise ResearchPolicyDenied("invalid original item selection")
    # Copies only. Full original demand remains authoritative, including partial instructions.
    plan = build_research_plan(
        request.model_copy(
            update={
                "city": CITIES[tier],
                "items": [request.items[i] for i in indices],
            }
        ),
        include_olx=True,
    )
    plan = plan.model_copy(
        update={
            "source_plans": [
                branch.model_copy(update={"item_index": indices[branch.item_index]})
                for branch in plan.source_plans
            ]
        }
    )
    return plan.model_copy(update={"plan_sha256": recompute_plan_sha256(plan)})


async def _source_items(c: asyncpg.Connection, snapshot) -> tuple[ParsedProcurementRequest, list]:
    request = ParsedProcurementRequest.model_validate(snapshot["normalized_payload"])
    case = await c.fetchrow(
        "SELECT city,status FROM procurement_cases WHERE id=$1 FOR SHARE", snapshot["case_id"]
    )
    if (
        not city_route(snapshot["snapshot"])
        or request.city != CITIES[0]
        or case["city"] != CITIES[0]
        or case["status"] in {"selected", "closed", "cancelled"}
        or snapshot["snapshot"]["source_payload_sha256"] != snapshot["source_payload_sha256"]
        or snapshot["snapshot"]["applied_payload_sha256"] != snapshot["applied_payload_sha256"]
        or snapshot["snapshot"]["policy"]["report_checkpoint_hours"] != 4
    ):
        raise ResearchPolicyDenied("city route/source identity is not authorized")
    items = await c.fetch(
        "SELECT * FROM request_items WHERE case_id=$1 ORDER BY line_number FOR SHARE",
        snapshot["case_id"],
    )
    if len(items) != len(request.items):
        raise ResearchPolicyDenied("source item membership drift")
    requirements = await c.fetch(
        """SELECT r.* FROM item_requirements r JOIN request_items i ON i.id=r.request_item_id
        WHERE i.case_id=$1 ORDER BY r.requirement_key,r.source_pointer FOR SHARE OF r""",
        snapshot["case_id"],
    )
    for index, (row, item) in enumerate(zip(items, request.items, strict=True)):
        if (
            row["line_number"] != index + 1
            or row["name"] != (item.name or f"Позиция {index + 1}")
            or row["quantity"] != item.quantity
            or row["unit"] != (item.unit or "")
            or row["analogs_allowed"] != item.analogs_allowed
            or not _source_specification_matches(item, row["specification_text"])
        ):
            raise ResearchPolicyDenied("original item identity drift")
        actual = [
            {
                "name": r["requirement_key"],
                "operator": r["operator"],
                "expected": r["expected_value"],
                "hard": r["is_hard"],
                "source": r["source_pointer"],
            }
            for r in requirements
            if r["request_item_id"] == row["id"]
        ]
        expected = sorted(
            [
                {
                    "name": r.name,
                    "operator": "equals",
                    "expected": {"value": r.expected_value, "unit": r.unit},
                    "hard": r.hard_requirement,
                    "source": r.source_reference or "",
                }
                for r in item.requirements
            ],
            key=lambda r: (r["name"], r["source"]),
        )
        if actual != expected:
            raise ResearchPolicyDenied("source technical requirements drift")
    return request, list(items)


def _source_specification_matches(item, specification_text: str) -> bool:
    """Accept native rendering or the exact legacy operator-import projection."""
    if specification_text == "; ".join(
        _requirement_text(value) for value in item.requirements
    ):
        return True
    if len(item.requirements) != 1:
        return False
    requirement = item.requirements[0]
    return (
        requirement.name == "Техническая характеристика"
        and isinstance(requirement.expected_value, str)
        and specification_text == requirement.expected_value
    )


async def _held(c: asyncpg.Connection, case_id: UUID) -> bool:
    runtime = await c.fetchrow(
        "SELECT enabled,paused FROM case_pipeline_runtime WHERE case_id=$1 FOR UPDATE",
        case_id,
    )
    return runtime is not None and (not runtime["enabled"] or runtime["paused"])


async def _satisfied(c: asyncpg.Connection, snapshot, items: list) -> dict[UUID, dict]:
    """Full source-bound instructions or current source-backed comparable quotes only."""
    result = {}
    instructions = await c.fetch(
        """SELECT * FROM item_sourcing_dispositions WHERE case_id=$1 AND owner_user_id=$2
        AND applies_to_sourcing AND source_parse_version_id=$3 AND source_payload_sha256=$4""",
        snapshot["case_id"],
        snapshot["owner_user_id"],
        snapshot["parse_version_id"],
        snapshot["applied_payload_sha256"],
    )
    by_id = {item["id"]: item for item in items}
    for row in instructions:
        item = by_id.get(row["request_item_id"])
        if item and full_instruction(row, item):
            result[item["id"]] = {"instruction_id": str(row["id"])}
    requirements, reviews = await load_case_technical_reviews(c, snapshot["case_id"])
    offers = await c.fetch(
        """SELECT o.id AS offer_id,o.request_item_id,o.supplier_id,o.status AS offer_status,
        i.quantity,i.unit,f.currency AS tender_currency,s.snapshot,s.id AS quote_snapshot_id
        FROM offers o JOIN request_items i ON i.id=o.request_item_id
        JOIN quote_snapshot_current q ON q.offer_id=o.id AND q.request_item_id=i.id
        JOIN quote_snapshot_versions s ON s.id=q.snapshot_id AND s.offer_id=o.id
          AND s.request_item_id=i.id
        LEFT JOIN request_item_financials f ON f.request_item_id=i.id
        WHERE i.case_id=$1 AND o.status IN ('lead','confirm','exact')""",
        snapshot["case_id"],
    )
    # Require the latest whole-spec and every hard requirement review to carry
    # evidence actually attached to this offer; an evidence-free match is insufficient.
    backed = await c.fetch(
        """SELECT DISTINCT o.offer_id,o.source_evidence_id FROM offer_observations o
        JOIN offers f ON f.id=o.offer_id JOIN request_items i ON i.id=f.request_item_id
        JOIN source_evidence e ON e.id=o.source_evidence_id WHERE i.case_id=$1""",
        snapshot["case_id"],
    )
    evidence = {(str(r["offer_id"]), str(r["source_evidence_id"])) for r in backed}
    states = technical_states(offers, requirements, reviews)
    latest_reviews = {}
    for review in reviews:  # native helper orders newest first
        latest_reviews.setdefault((str(review["offer_id"]), str(review["requirement_id"])), review)
    for offer in offers:
        oid = str(offer["offer_id"])
        current_reviews = [r for (key, _), r in latest_reviews.items() if key == oid]
        if not current_reviews or any(
            (oid, str(r["source_evidence_id"])) not in evidence for r in current_reviews
        ):
            continue
        facts = offer["snapshot"]["facts"]
        if any(
            (oid, str(fact.get("source_evidence_id"))) not in evidence for fact in facts.values()
        ):
            continue
        row = {
            **dict(offer),
            **{name: fact["value"] for name, fact in facts.items()},
            "technical_state": states[oid],
            "source_type": facts.get("price_amount", {}).get("source_type"),
            "stock_source_type": facts.get("available_qty", {}).get("source_type"),
        }
        if compare_offer(row).rankable:
            result[offer["request_item_id"]] = {
                "offer_id": oid,
                "quote_snapshot_id": str(offer["quote_snapshot_id"]),
            }
    return result


def full_instruction(instruction, item) -> bool:
    quantity = instruction["quantity"]
    original = item["quantity"]
    disposition = instruction["disposition"]
    if instruction["unit"] != item["unit"]:
        return False
    if disposition == "skip":
        return quantity is None or quantity == original
    if original is None or quantity is None:
        return False
    if disposition == "bought":
        return quantity >= original
    # A full reduction is an explicit instruction to stop sourcing the original
    # quantity; a smaller reduction remains active original demand.
    return disposition == "reduce_quantity" and quantity == original


def discovery_outcome(
    checkpoints: list, *, complete: bool, unsafe: bool,
    successful_work_budget: dict[str, int] | None = None,
) -> str:
    """Classify only complete, safe native checkpoints and their persisted caps."""
    if not checkpoints:
        return "not_started"
    if not complete:
        return "incomplete"
    if any(row["status"] in {"queued", "researching"} or row["live_lease"]
           for row in checkpoints):
        return "incomplete"
    if any(row["status"] == "paused" for row in checkpoints):
        return "paused"
    if any(row["status"] == "blocked" for row in checkpoints):
        return "blocked"
    if any(row["status"] == "failed" for row in checkpoints):
        return "failed"
    if unsafe:
        return "incomplete"

    budget = (
        validate_city_budget(successful_work_budget)
        if successful_work_budget is not None
        else None
    )
    budget_reached = False
    for row in checkpoints:
        if row["last_error_code"]:
            return "incomplete"
        reason = row["stop_reason"]
        if row["status"] == "succeeded" and reason in _EXHAUSTED:
            continue
        if (budget is not None and reason == "target_reached"
                and row["status"] == "succeeded"
                and row.get("target_suppliers") == budget["target_suppliers"]
                and int(row.get("eligible_suppliers", 0)) >= budget["target_suppliers"]):
            budget_reached = True
            continue
        if (budget is not None and reason == "batch_limit_reached"
                and row["status"] == "succeeded"
                and row.get("max_batches") == budget["max_batches"]
                and int(row.get("current_batch_no", -1)) + 1 >= budget["max_batches"]):
            budget_reached = True
            continue
        return "incomplete"
    return "budget_complete" if budget_reached else "exhausted"


async def validate_city_admission(c: asyncpg.Connection, snapshot, run_id: UUID) -> Any:
    admission = await c.fetchrow(
        "SELECT * FROM research_city_admissions WHERE research_run_id=$1",
        run_id,
    )
    if admission is None:
        return None  # existing first-city policy authorizations retain their contract
    request, items = await _source_items(c, snapshot)
    binding = admission["binding"]
    indices = binding["item_indices"]
    plan = city_plan(request, admission["tier"], indices)
    budget = validate_city_budget(binding.get("successful_work_budget"))
    expected = _binding(snapshot, run_id, admission["tier"], plan, indices, items, budget)
    if admission["tier"] > 0:
        first = await c.fetchrow(
            "SELECT binding FROM research_city_admissions WHERE snapshot_id=$1 AND tier=0",
            snapshot["id"],
        )
        if (first is None or validate_city_budget(
                first["binding"].get("successful_work_budget")) != budget):
            raise ResearchPolicyDenied("city budget changed after the first admission")
    scope = await c.fetchval("SELECT search_scope FROM research_runs WHERE id=$1", run_id)
    if (
        admission["snapshot_id"] != snapshot["id"]
        or admission["case_id"] != snapshot["case_id"]
        or admission["city"] != CITIES[admission["tier"]]
        or admission["enum_city"] != enum_city(admission["city"]).value
        or binding != expected
        or payload_sha256(binding) != admission["binding_sha256"]
        or scope["plan_sha256"] != plan.plan_sha256
        or scope["plan"] != plan.model_dump(mode="json", exclude={"plan_sha256"})
    ):
        raise ResearchPolicyDenied("immutable city admission/plan/source binding differs")
    return admission


def _binding(snapshot, run_id, tier, plan, indices, items, budget) -> dict:
    return {
        "snapshot_id": str(snapshot["id"]),
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "case_id": str(snapshot["case_id"]),
        "research_run_id": str(run_id),
        "tier": tier,
        "city": CITIES[tier],
        "enum_city": enum_city(CITIES[tier]).value,
        "plan_sha256": plan.plan_sha256,
        "parse_version_id": str(snapshot["parse_version_id"]),
        "source_payload_sha256": snapshot["source_payload_sha256"],
        "applied_payload_sha256": snapshot["applied_payload_sha256"],
        "original_item_ids": [str(item["id"]) for item in items],
        "item_indices": indices,
        "item_ids": [str(items[i]["id"]) for i in indices],
        "successful_work_budget": validate_city_budget(budget),
    }


async def activate_city_admission(c: asyncpg.Connection, snapshot, run_id: UUID) -> int | None:
    """Called inside the native approved-start transaction before any enqueue."""
    admission = await validate_city_admission(c, snapshot, run_id)
    if admission is None:
        return None
    if await _held(c, snapshot["case_id"]):
        raise ResearchPolicyDenied("city research is held")
    tier = admission["tier"]
    previous = await c.fetchval(
        "SELECT research_run_id FROM case_pipeline_runtime WHERE case_id=$1",
        snapshot["case_id"],
    )
    if previous is not None and previous != run_id:
        # Never displace an unseeded run or a live/queued discovery batch.
        if not await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM research_city_admissions WHERE research_run_id=$1)",
            previous,
        ) or not await reconcile_city_run(c, snapshot, previous):
            raise ResearchPolicyDenied("previous city discovery is still open or unknown")
    for item_id in map(UUID, admission["binding"]["item_ids"]):
        if tier == 0:
            await c.execute(
                """INSERT INTO research_city_item_cursors(snapshot_id,case_id,request_item_id)
                VALUES($1,$2,$3) ON CONFLICT DO NOTHING""",
                snapshot["id"],
                snapshot["case_id"],
                item_id,
            )
        cursor = await c.fetchrow(
            """SELECT * FROM research_city_item_cursors WHERE snapshot_id=$1
            AND request_item_id=$2 FOR UPDATE""",
            snapshot["id"],
            item_id,
        )
        if (
            cursor is None
            or cursor["tier"] != tier
            or cursor["current_run_id"] is not None
            or cursor["satisfied"]
        ):
            raise ResearchPolicyDenied("item city predecessor/fence differs")
        if tier and not await c.fetchval(
            """SELECT EXISTS(SELECT 1 FROM research_city_item_outcomes WHERE snapshot_id=$1
            AND request_item_id=$2 AND tier=$3 AND status IN ('exhausted','budget_complete'))""",
            snapshot["id"],
            item_id,
            tier - 1,
        ):
            raise ResearchPolicyDenied("item predecessor is not confirmed terminal")
        await c.execute(
            """UPDATE research_city_item_cursors SET current_run_id=$3,updated_at=now()
            WHERE snapshot_id=$1 AND request_item_id=$2""",
            snapshot["id"],
            item_id,
            run_id,
        )
    return tier


async def start_city_research(
    c: asyncpg.Connection, snapshot, tier: int, *,
    city_budget: dict[str, int] | None = None,
) -> bool:
    """Batch every eligible original item at this tier through native consent/start."""
    async with c.transaction():
        await c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"owner-research-start:{snapshot['case_id']}",
        )
        return await _start_city_research(c, snapshot, tier, city_budget=city_budget)


async def _start_city_research(
    c: asyncpg.Connection, snapshot, tier: int, *,
    city_budget: dict[str, int] | None = None,
) -> bool:
    from procurement_bot.owner_autonomy import bind_research_policy
    from procurement_bot.research_workflow import start_approved_research

    request, items = await _source_items(c, snapshot)
    if await _held(c, snapshot["case_id"]):
        raise ResearchPolicyDenied("city research is held")
    for item in items:
        await c.execute(
            """INSERT INTO research_city_item_cursors(snapshot_id,case_id,request_item_id)
            VALUES($1,$2,$3) ON CONFLICT DO NOTHING""",
            snapshot["id"],
            snapshot["case_id"],
            item["id"],
        )
    cursors = {
        r["request_item_id"]: r
        for r in await c.fetch(
            "SELECT * FROM research_city_item_cursors WHERE snapshot_id=$1 FOR UPDATE",
            snapshot["id"],
        )
    }
    satisfied = await _satisfied(c, snapshot, items)
    for item_id in satisfied:
        await c.execute(
            """UPDATE research_city_item_cursors SET satisfied=true,updated_at=now()
            WHERE snapshot_id=$1 AND request_item_id=$2 AND current_run_id IS NULL""",
            snapshot["id"],
            item_id,
        )
    indices = [
        i
        for i, item in enumerate(items)
        if not item_search_issues(request.items[i])
        and item["id"] not in satisfied
        and not cursors[item["id"]]["satisfied"]
        and cursors[item["id"]]["tier"] == tier
        and cursors[item["id"]]["current_run_id"] is None
    ]
    if not indices:
        return False
    if tier == 0:
        if city_budget is None:
            raise ResearchPolicyDenied(
                "initial city admission needs the native successful-work cap"
            )
        route_budget = validate_city_budget(city_budget)
    else:
        first = await c.fetchrow(
            "SELECT binding FROM research_city_admissions WHERE snapshot_id=$1 AND tier=0",
            snapshot["id"],
        )
        if first is None:
            raise ResearchPolicyDenied("later city lacks its immutable first-tier budget")
        route_budget = validate_city_budget(first["binding"].get("successful_work_budget"))
    plan = city_plan(request, tier, indices)
    persisted = await persist_research_plan(
        c, case_id=snapshot["case_id"], plan=plan, requested_by=snapshot["owner_user_id"]
    )
    binding = _binding(snapshot, persisted.id, tier, plan, indices, items, route_budget)
    await c.execute(
        """INSERT INTO research_city_admissions(research_run_id,snapshot_id,case_id,tier,
        city,enum_city,binding,binding_sha256) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
        ON CONFLICT DO NOTHING""",
        persisted.id,
        snapshot["id"],
        snapshot["case_id"],
        tier,
        CITIES[tier],
        enum_city(CITIES[tier]).value,
        binding,
        payload_sha256(binding),
    )
    # Record excluded original lines as well; they cannot silently disappear from
    # the admission ledger or be renumbered into a later plan.
    for index, item in enumerate(items):
        if index in indices:
            continue
        status = (
            "satisfied"
            if item["id"] in satisfied
            else "blocked"
            if item_search_issues(request.items[index])
            else "not_started"
        )
        proof = {
            "status": status,
            "item_index": index,
            "satisfaction": satisfied.get(item["id"]),
            "selected": False,
        }
        await c.execute(
            """INSERT INTO research_city_item_outcomes(research_run_id,snapshot_id,case_id,
            request_item_id,tier,status,proof,proof_sha256) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
            ON CONFLICT DO NOTHING""",
            persisted.id,
            snapshot["id"],
            snapshot["case_id"],
            item["id"],
            tier,
            status,
            proof,
            payload_sha256(proof),
        )
    auth = await bind_research_policy(
        c,
        snapshot_id=snapshot["id"],
        case_id=snapshot["case_id"],
        run_id=persisted.id,
        plan_sha256=plan.plan_sha256,
    )
    payload = {
        "case_id": str(snapshot["case_id"]),
        "research_run_id": str(persisted.id),
        "plan_sha256": plan.plan_sha256,
        **auth,
    }
    approval = await c.fetchval(
        """SELECT id FROM action_approvals WHERE case_id=$1 AND action_type='start_research'
        AND payload_sha256=$2 AND status IN ('requested','approved','consumed')
        ORDER BY requested_at DESC LIMIT 1""",
        snapshot["case_id"],
        payload_sha256(payload),
    )
    if approval is None:
        approval = await request_approval(
            c,
            case_id=snapshot["case_id"],
            action_type="start_research",
            payload=payload,
            requested_by=snapshot["owner_user_id"],
        )
    await approve_request(c, approval_id=approval, approved_by=snapshot["owner_user_id"])
    await start_approved_research(c, approval_id=approval)
    return True


async def reconcile_city_run(c: asyncpg.Connection, snapshot, run_id: UUID) -> bool:
    """Append outcomes and move only items still owned by this exact run.

    Reconcile every item independently. Open, unknown, leased or blocked items
    keep their current fence, while proven terminal neighbours advance.
    """
    admission = await validate_city_admission(c, snapshot, run_id)
    if admission is None:
        return False
    _, items = await _source_items(c, snapshot)
    satisfied = await _satisfied(c, snapshot, items)
    checkpoints = await c.fetch(
        """SELECT cp.*,q.parameters->>'item_index' AS item_index,
        g.target_suppliers,g.eligible_suppliers,
        (cp.lease_until>now() AND cp.owner_worker IS NOT NULL) AS live_lease
        FROM research_branch_checkpoints cp JOIN research_queries q ON q.id=cp.query_id
        JOIN research_item_groups g ON g.id=cp.group_id
        WHERE cp.research_run_id=$1 ORDER BY cp.id FOR SHARE OF cp""",
        run_id,
    )
    queries = await c.fetch(
        "SELECT id,parameters FROM research_queries WHERE research_run_id=$1", run_id
    )
    intent_rows = await c.fetch(
        """SELECT p.request_item_id,
        COALESCE(i.recovery_stop_reason,i.stop_reason) AS stop_reason
        FROM research_search_intents i JOIN research_search_profiles p ON p.id=i.profile_id
        WHERE i.research_run_id=$1 AND p.profile_version=(
          SELECT max(v.profile_version) FROM research_search_profiles v
          WHERE v.research_run_id=p.research_run_id AND v.request_item_id=p.request_item_id)
        """,
        run_id,
    )
    pending_cards = {
        row["request_item_id"]
        for row in await c.fetch(
            """SELECT DISTINCT g.request_item_id FROM research_discovery_cards card
            JOIN research_discovery_pages p ON p.id=card.page_id
            JOIN research_branch_checkpoints cp ON cp.query_id=p.query_id
            JOIN research_item_groups g ON g.id=cp.group_id
            WHERE cp.research_run_id=$1 AND card.status IN ('pending','processing','failed')""",
            run_id,
        )
    }
    intents_by_item: dict[UUID, list[str]] = {}
    for row in intent_rows:
        intents_by_item.setdefault(row["request_item_id"], []).append(row["stop_reason"] or "")
    for index, raw_id in zip(
        admission["binding"]["item_indices"], admission["binding"]["item_ids"], strict=True
    ):
        item_id = UUID(raw_id)
        rows = [cp for cp in checkpoints if int(cp["item_index"]) == index]
        expected = {q["id"] for q in queries if q["parameters"]["item_index"] == index}
        complete = bool(expected) and {cp["query_id"] for cp in rows} == expected
        budget = validate_city_budget(admission["binding"].get("successful_work_budget"))
        provisional = discovery_outcome(
            rows, complete=complete, unsafe=False, successful_work_budget=budget,
        )
        intent_reasons = intents_by_item.get(item_id, [])
        cap_reasons = {"target_reached", "batch_limit_reached"}
        invalid_intents = any(
            reason not in _EXHAUSTED and reason not in cap_reasons
            for reason in intent_reasons
        )
        valid_budget_intents = (
            provisional == "budget_complete"
            and all(reason in _EXHAUSTED or reason in cap_reasons for reason in intent_reasons)
        )
        unsafe = (
            item_id in pending_cards
            or invalid_intents and not valid_budget_intents
        )
        status = discovery_outcome(
            rows, complete=complete, unsafe=unsafe, successful_work_budget=budget,
        )
        if item_id in satisfied:
            status = "satisfied"
        proof = {
            "status": status,
            "satisfaction": satisfied.get(item_id),
            "complete": complete,
            "unsafe": unsafe,
            "successful_work_budget": budget,
            "successful_work_budget_sha256": payload_sha256(budget),
            "checkpoints": [
                {
                    "id": str(cp["id"]),
                    "generation": cp["owner_generation"],
                    "status": cp["status"],
                    "reason": cp["stop_reason"],
                    "error": cp["last_error_code"],
                    "pages": cp["pages_examined"],
                    "eligible_suppliers": cp["eligible_suppliers"],
                    "target_suppliers": cp["target_suppliers"],
                    "current_batch_no": cp["current_batch_no"],
                    "max_batches": cp["max_batches"],
                }
                for cp in rows
            ],
        }
        await c.execute(
            """INSERT INTO research_city_item_outcomes(research_run_id,snapshot_id,case_id,
            request_item_id,tier,status,proof,proof_sha256) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
            ON CONFLICT DO NOTHING""",
            run_id,
            snapshot["id"],
            snapshot["case_id"],
            item_id,
            admission["tier"],
            status,
            proof,
            payload_sha256(proof),
        )
        if status in {"satisfied", "exhausted", "budget_complete"}:
            await c.execute(
                """UPDATE research_city_item_cursors SET current_run_id=NULL,satisfied=$4,
                tier=tier+CASE WHEN $4 THEN 0 ELSE 1 END,updated_at=now()
                WHERE snapshot_id=$1 AND request_item_id=$2 AND current_run_id=$3 AND tier=$5""",
                snapshot["id"],
                item_id,
                run_id,
                status == "satisfied",
                admission["tier"],
            )
    return True


async def bootstrap_existing_first_city_run(
    c: asyncpg.Connection, snapshot, run_id: UUID, *,
    city_budget: dict[str, int] | None = None,
) -> bool:
    """Attach 046 metadata to the exact live 044 Taraz run; never re-plan it."""
    if await c.fetchval(
        "SELECT EXISTS(SELECT 1 FROM research_city_admissions WHERE research_run_id=$1)",
        run_id,
    ):
        return False
    runtime = await c.fetchrow(
        """SELECT research_run_id,research_policy_snapshot_id,enabled,paused
        FROM case_pipeline_runtime WHERE case_id=$1 FOR UPDATE""", snapshot["case_id"],
    )
    if (runtime is None or runtime["research_run_id"] != run_id
            or runtime["research_policy_snapshot_id"] != snapshot["id"]
            or not runtime["enabled"] or runtime["paused"]):
        return False
    run = await c.fetchrow(
        "SELECT id,case_id,status,started_at,search_scope FROM research_runs WHERE id=$1 FOR SHARE",
        run_id,
    )
    if (run is None or run["case_id"] != snapshot["case_id"] or run["status"] != "running"
            or run["started_at"] is None):
        raise ResearchPolicyDenied("legacy city run is not the current live approved run")
    scope = run["search_scope"]
    if isinstance(scope, str):
        scope = json.loads(scope)
    if not isinstance(scope, dict) or not isinstance(scope.get("plan"), dict):
        raise ResearchPolicyDenied("legacy city run has no immutable plan")
    plan_sha = scope.get("plan_sha256")
    plan = ResearchPlan.model_validate({**scope["plan"], "plan_sha256": plan_sha})
    if (plan.city != CITIES[0] or recompute_plan_sha256(plan) != plan_sha
            or plan_sha != scope.get("plan_sha256")):
        raise ResearchPolicyDenied("legacy run is not an exact first-city plan")
    request, items = await _source_items(c, snapshot)
    indices = sorted({branch.item_index for branch in plan.source_plans})
    expected = city_plan(request, 0, indices)
    if plan.model_dump(mode="json", exclude={"plan_sha256"}) != expected.model_dump(
        mode="json", exclude={"plan_sha256"}
    ):
        raise ResearchPolicyDenied("legacy Taraz plan differs from canonical source projection")
    queries = await c.fetch(
        "SELECT parameters FROM research_queries WHERE research_run_id=$1", run_id,
    )
    query_indices = sorted({int(row["parameters"]["item_index"]) for row in queries})
    if not queries or query_indices != indices:
        raise ResearchPolicyDenied("legacy run query membership differs from its immutable plan")

    from procurement_bot.owner_autonomy import validate_research_policy_binding
    authorization = await c.fetchrow(
        """SELECT id,binding_sha256,plan_sha256 FROM research_policy_authorizations
        WHERE research_run_id=$1""", run_id,
    )
    if authorization is None or authorization["plan_sha256"] != plan_sha:
        raise ResearchPolicyDenied("legacy run has no exact 044 owner authorization")
    await validate_research_policy_binding(
        c, authorization_id=authorization["id"],
        authorization_sha256=authorization["binding_sha256"],
        case_id=snapshot["case_id"], run_id=run_id, plan_sha256=plan_sha,
        approved_by=snapshot["owner_user_id"],
    )
    instructions = await c.fetch(
        """SELECT d.*,i.quantity AS original_quantity,i.unit AS original_unit
        FROM item_sourcing_dispositions d JOIN request_items i
          ON i.id=d.request_item_id AND i.case_id=d.case_id
        WHERE d.case_id=$1 AND d.owner_user_id=$2 AND d.applies_to_sourcing
          AND d.source_parse_version_id=$3 AND d.source_payload_sha256=$4""",
        snapshot["case_id"], snapshot["owner_user_id"], snapshot["parse_version_id"],
        snapshot["applied_payload_sha256"],
    )
    selected_ids = {items[index]["id"] for index in indices}
    if any(row["request_item_id"] in selected_ids and full_instruction(
        {**row, "unit": row["unit"], "quantity": row["quantity"]},
        {"unit": row["original_unit"], "quantity": row["original_quantity"]},
    ) for row in instructions):
        raise ResearchPolicyDenied("legacy run includes a fully excluded owner item")
    satisfied = await _satisfied(c, snapshot, items)
    for index, item in enumerate(items):
        if (
            index not in indices
            and not item_search_issues(request.items[index])
            and item["id"] not in satisfied
        ):
            raise ResearchPolicyDenied("legacy run omitted an eligible original item")

    checkpoints = await c.fetch(
        """SELECT cp.max_batches,g.target_suppliers FROM research_branch_checkpoints cp
        JOIN research_item_groups g ON g.id=cp.group_id WHERE cp.research_run_id=$1""", run_id,
    )
    if checkpoints:
        targets = {int(row["target_suppliers"]) for row in checkpoints}
        if len(targets) != 1:
            raise ResearchPolicyDenied("legacy run has inconsistent native target budget")
        route_budget = validate_city_budget({
            "target_suppliers": targets.pop(),
            "max_batches": max(int(row["max_batches"]) for row in checkpoints),
        })
    elif city_budget is not None:
        route_budget = validate_city_budget(city_budget)
    else:
        raise ResearchPolicyDenied("legacy run has no persisted native successful-work cap")

    binding = _binding(snapshot, run_id, 0, expected, indices, items, route_budget)
    digest = payload_sha256(binding)
    await c.execute(
        """INSERT INTO research_city_admissions(research_run_id,snapshot_id,case_id,tier,
        city,enum_city,binding,binding_sha256) VALUES($1,$2,$3,0,$4,$5,$6::jsonb,$7)""",
        run_id, snapshot["id"], snapshot["case_id"], CITIES[0],
        enum_city(CITIES[0]).value, binding, digest,
    )
    for index, item in enumerate(items):
        selected = index in indices
        item_satisfied = item["id"] in satisfied and not selected
        await c.execute(
            """INSERT INTO research_city_item_cursors(snapshot_id,case_id,request_item_id,
            tier,current_run_id,satisfied) VALUES($1,$2,$3,0,$4,$5)""",
            snapshot["id"], snapshot["case_id"], item["id"],
            run_id if selected else None, item_satisfied,
        )
        if not selected:
            status = "satisfied" if item_satisfied else (
                "blocked" if item_search_issues(request.items[index]) else "not_started"
            )
            proof = {"status": status, "item_index": index,
                     "satisfaction": satisfied.get(item["id"]), "selected": False,
                     "bootstrap": "legacy-044"}
            await c.execute(
                """INSERT INTO research_city_item_outcomes(research_run_id,snapshot_id,case_id,
                request_item_id,tier,status,proof,proof_sha256)
                VALUES($1,$2,$3,$4,0,$5,$6::jsonb,$7)""",
                run_id, snapshot["id"], snapshot["case_id"], item["id"], status,
                proof, payload_sha256(proof),
            )
    await c.execute(
        """UPDATE case_pipeline_runtime SET active_city_tier=0,
        report_checkpoint_at=COALESCE(report_checkpoint_at,
          (SELECT started_at+interval '4 hours' FROM research_runs WHERE id=$2))
        WHERE case_id=$1""", snapshot["case_id"], run_id,
    )
    await c.execute(
        """INSERT INTO research_city_report_checkpoints(case_id,snapshot_id,start_run_id,
        started_at,due_at) VALUES($1,$2,$3,$4,$4::timestamptz+interval '4 hours')
        ON CONFLICT(case_id,snapshot_id) DO NOTHING""",
        snapshot["case_id"], snapshot["id"], run_id, run["started_at"],
    )
    return True


async def city_tick(
    c: asyncpg.Connection, case_id: UUID, *, results_root: Path | None = None,
    city_budget: dict[str, int] | None = None,
):
    """One serialized native tick, also used by the worker terminal hook."""
    async with c.transaction():
        await c.execute("SELECT pg_advisory_xact_lock($1)", RESEARCH_SCHEDULER_LOCK_KEY)
        await c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"owner-research-start:{case_id}",
        )
        runtime = await c.fetchrow(
            "SELECT * FROM case_pipeline_runtime WHERE case_id=$1 FOR UPDATE",
            case_id,
        )
        if runtime is None or runtime["research_policy_snapshot_id"] is None:
            return
        snapshot = await load_valid_snapshot(
            c,
            snapshot_id=runtime["research_policy_snapshot_id"],
            case_id=case_id,
        )
        if not city_route(snapshot["snapshot"]) or await _held(c, case_id):
            return
        active_run = runtime["research_run_id"]
        if active_run is not None and not await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM research_city_admissions WHERE research_run_id=$1)",
            active_run,
        ):
            await bootstrap_existing_first_city_run(
                c, snapshot, active_run, city_budget=city_budget,
            )
        if results_root is not None:
            try:
                # A report failure leaves its durable checkpoint pending and
                # rolls back only report metadata/outbox, never discovery work.
                async with c.transaction():
                    await checkpoint_report(c, snapshot, results_root=results_root)
            except Exception as exc:
                LOGGER.warning("city checkpoint deferred: %s", type(exc).__name__)
        if active_run is not None and not await reconcile_city_run(c, snapshot, active_run):
            return
        for tier in range(len(CITIES)):
            if await start_city_research(
                c, snapshot, tier, city_budget=city_budget,
            ):
                return
        # Keep a still-fenced current city run at the pointer while it has
        # unresolved items. Restore an older blocked run only after this run's
        # own items have all advanced or been satisfied.
        if active_run is not None and await c.fetchval(
            """SELECT EXISTS(SELECT 1 FROM research_city_item_cursors
            WHERE snapshot_id=$1 AND current_run_id=$2)""",
            snapshot["id"],
            active_run,
        ):
            return
        unresolved = await c.fetchval(
            """SELECT current_run_id FROM research_city_item_cursors WHERE snapshot_id=$1
            AND current_run_id IS NOT NULL ORDER BY tier,request_item_id LIMIT 1""",
            snapshot["id"],
        )
        if unresolved is not None:
            await c.execute(
                """UPDATE case_pipeline_runtime SET research_run_id=$2,active_city_tier=(
                SELECT tier FROM research_city_admissions WHERE research_run_id=$2)
                WHERE case_id=$1""",
                case_id,
                unresolved,
            )
        elif not await c.fetchval(
            """SELECT EXISTS(SELECT 1 FROM research_city_item_cursors WHERE snapshot_id=$1
            AND NOT satisfied AND tier<3)""",
            snapshot["id"],
        ):
            await c.execute(
                """UPDATE procurement_cases SET status='evaluating',updated_at=now()
                WHERE id=$1 AND status IN ('researching','contacting')""",
                case_id,
            )
            await enqueue_job(
                c,
                kind="refresh_report",
                payload={"case_id": str(case_id), "trigger_id": f"city-route:{snapshot['id']}"},
                idempotency_key=stable_key("city-route-report", snapshot["id"]),
            )


async def prepare_city_resume(c: asyncpg.Connection, run_id: UUID) -> None:
    """Prepare the existing explicit native resume; preserve prior source outcomes.

    The caller holds the native scheduler lock and transaction. Only unresolved
    original items can be retried; another live city run cannot be displaced.
    """
    admission = await c.fetchrow(
        "SELECT * FROM research_city_admissions WHERE research_run_id=$1",
        run_id,
    )
    if admission is None:
        return
    snapshot = await load_valid_snapshot(
        c, snapshot_id=admission["snapshot_id"], case_id=admission["case_id"]
    )
    await validate_city_admission(c, snapshot, run_id)
    runtime = await c.fetchrow(
        "SELECT * FROM case_pipeline_runtime WHERE case_id=$1 FOR UPDATE",
        admission["case_id"],
    )
    if not runtime["enabled"]:
        raise ResearchPolicyDenied("disabled city research cannot resume")
    if runtime["research_run_id"] != run_id and not await reconcile_city_run(
        c,
        snapshot,
        runtime["research_run_id"],
    ):
        raise ResearchPolicyDenied("another city still has open discovery")
    tasks = await c.fetch(
        """SELECT cp.* FROM research_branch_checkpoints cp
        JOIN research_item_groups g ON g.id=cp.group_id
        JOIN research_city_item_cursors ci ON ci.request_item_id=g.request_item_id
          AND ci.current_run_id=cp.research_run_id
        WHERE cp.research_run_id=$1 AND cp.status IN
          ('succeeded','failed','blocked','skipped','paused')
        AND (cp.status<>'succeeded' OR cp.stop_reason<>ALL($2::text[]) OR cp.last_error_code<>'')
        AND (cp.lease_until IS NULL OR cp.lease_until<=now()) FOR UPDATE OF cp""",
        run_id,
        sorted(_EXHAUSTED),
    )
    if not tasks:
        return
    for task in tasks:
        intent = await c.fetchrow(
            """WITH reopened AS (
            UPDATE research_search_intents i SET recovery_stop_reason='',updated_at=now()
            FROM research_search_profiles p,research_item_groups g,research_queries q
            WHERE i.profile_id=p.id AND p.research_run_id=$1 AND p.request_item_id=g.request_item_id
            AND g.id=$2 AND q.id=$3 AND i.provider=q.provider
            AND p.profile_version=(SELECT max(v.profile_version) FROM research_search_profiles v
              WHERE v.research_run_id=p.research_run_id AND v.request_item_id=p.request_item_id)
            AND COALESCE(i.recovery_stop_reason,i.stop_reason)<>ALL($4::text[])
            RETURNING i.ordinal,i.cursor_state,i.results)
            SELECT * FROM reopened ORDER BY ordinal LIMIT 1""",
            run_id,
            task["group_id"],
            task["query_id"],
            sorted(_EXHAUSTED),
        )
        if intent is not None:
            await c.execute(
                """UPDATE research_branch_checkpoints SET semantic_intent_ordinal=$2,
                search_cursor_state=$3,source_progress_token=$4 WHERE id=$1""",
                task["id"],
                intent["ordinal"],
                intent["cursor_state"],
                intent["results"].get("source_progress_token"),
            )
        await c.execute(
            """UPDATE research_discovery_cards SET status='pending',recovery_base_attempts=attempts
            WHERE status='failed' AND page_id IN
            (SELECT id FROM research_discovery_pages WHERE query_id=$1)""",
            task["query_id"],
        )
        await c.execute(
            "UPDATE research_branch_checkpoints SET status='paused' WHERE id=$1", task["id"]
        )
        await c.execute(
            "UPDATE research_item_groups SET status='paused' WHERE id=$1", task["group_id"]
        )
        await c.execute("UPDATE research_lanes SET status='paused' WHERE id=$1", task["lane_id"])
    await c.execute(
        """UPDATE research_runs SET status='running',completed_at=NULL,error_code='' WHERE id=$1""",
        run_id,
    )
    await c.execute(
        """UPDATE case_pipeline_runtime SET research_run_id=$2,active_city_tier=$3
        WHERE case_id=$1""",
        admission["case_id"],
        run_id,
        admission["tier"],
    )
    await c.execute(
        "UPDATE procurement_cases SET status='researching' WHERE id=$1", admission["case_id"]
    )


async def checkpoint_report(c: asyncpg.Connection, snapshot, *, results_root: Path):
    from procurement_bot.report_pipeline import build_case_report

    checkpoint = await c.fetchrow(
        """SELECT * FROM research_city_report_checkpoints WHERE case_id=$1 AND snapshot_id=$2
        AND due_at<=now() AND artifact_id IS NULL FOR UPDATE""",
        snapshot["case_id"],
        snapshot["id"],
    )
    if checkpoint is None:
        return
    # Explicit run freezes report identity across restarts and later-city pointer changes.
    report = await build_case_report(
        c,
        results_root=results_root,
        case_id=snapshot["case_id"],
        research_run_id=checkpoint["start_run_id"],
        refresh_key=f"city-checkpoint:{checkpoint['id']}",
        partial_research_snapshot=True,
        checkpoint_id=checkpoint["id"],
    )
    outbox_id = await c.fetchval(
        "SELECT id FROM outbox_events WHERE idempotency_key=$1",
        stable_key("telegram.city-checkpoint", report.artifact_id),
    )
    await c.execute(
        """UPDATE research_city_report_checkpoints SET artifact_id=$2,outbox_event_id=$3,
        completed_at=now() WHERE id=$1""",
        checkpoint["id"],
        report.artifact_id,
        outbox_id,
    )


async def checkpoint_delivery_allowed(c: asyncpg.Connection, event_id: UUID) -> bool:
    checkpoint = await c.fetchrow(
        "SELECT * FROM research_city_report_checkpoints WHERE outbox_event_id=$1",
        event_id,
    )
    if checkpoint is None:
        return True
    try:
        snapshot = await load_valid_snapshot(
            c, snapshot_id=checkpoint["snapshot_id"], case_id=checkpoint["case_id"]
        )
        await _source_items(c, snapshot)
        return not await _held(c, checkpoint["case_id"])
    except ResearchPolicyDenied:
        return False
