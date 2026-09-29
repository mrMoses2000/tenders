"""Owner initiated research for an item imported without a Telegram parse version."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from procurement_bot.approvals import approve_request, request_approval
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.research import (
    PrivatePriceLeakError,
    ResearchPlan,
    ResearchSource,
    assert_no_private_price,
    build_research_plan,
)
from procurement_bot.research_repository import persist_research_plan, recompute_plan_sha256
from procurement_bot.research_workflow import start_approved_research


class ImportedResearchConflict(ValueError):
    """The selected imported item cannot start another research run."""


def _search_name(name: str, specification_text: str) -> str:
    first_clause = re.split(
        r"\bпо согласованию\b|[.;\n]", specification_text, maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,:-")
    if len(first_clause) < 4:
        return name
    phrase = first_clause[:240].rsplit(" ", 1)[0] if len(first_clause) > 240 else first_clause
    try:
        assert_no_private_price(phrase)
    except PrivatePriceLeakError:
        return name
    return phrase or name


def _specification_clues(name: str, specification_text: str) -> list[str]:
    """Keep product requirements available to 2GIS category inference."""

    clauses = [name, *re.split(r"[.;\n]+", specification_text)]
    clues: list[str] = []
    seen: set[str] = set()
    for clause in clauses:
        value = clause.strip(" ,:-")[:500].strip()
        if len(value) < 4 or value.casefold() in seen:
            continue
        try:
            assert_no_private_price(value)
        except PrivatePriceLeakError:
            continue
        clues.append(value)
        seen.add(value.casefold())
        if len(clues) >= 20:
            break
    return clues or [name]


async def start_imported_item_research(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    item_id: UUID,
    owner_user_id: UUID,
) -> UUID:
    """Plan and queue one item through the same approval boundary as Telegram intake."""

    async with connection.transaction():
        case = await connection.fetchrow(
            """
            SELECT id,city,search_area_text,status,source_kind,
                   EXISTS(SELECT 1 FROM case_derivations d
                          WHERE d.derived_case_id=procurement_cases.id) AS is_derived
            FROM procurement_cases
            WHERE id=$1 AND owner_user_id=$2
            """,
            case_id, owner_user_id,
        )
        if case is None or not (
            case["source_kind"] == "notion_import" or case["is_derived"]
        ):
            raise ImportedResearchConflict("Заявка для поиска не найдена")
        if case["status"] not in {"ready", "evaluating", "report_ready"}:
            raise ImportedResearchConflict("Дождитесь завершения текущего поиска")
        if not case["city"]:
            raise ImportedResearchConflict("У заявки не указан город поиска")
        items = await connection.fetch(
            """
            SELECT id,name,specification_text,quantity,unit
            FROM request_items WHERE case_id=$1 ORDER BY line_number,id
            """,
            case_id,
        )
        index = next((number for number, item in enumerate(items) if item["id"] == item_id), None)
        if index is None:
            raise ImportedResearchConflict("Позиция не найдена в заявке")
        item = items[index]
        request = ParsedProcurementRequest(
            city=case["city"],
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(
                name=_search_name(item["name"], item["specification_text"]),
                quantity=item["quantity"], unit=item["unit"] or None,
            )],
        )
        # The initial derived pilot used four candidates to prove each source.
        # Subsequent runs must pursue the requested supplier campaign coverage;
        # candidates are still only leads until contact evidence is verified.
        single = build_research_plan(
            request, include_olx=True,
            minimum_unique_candidates=30 if case["is_derived"] else 10,
        )
        source_priority = {
            ResearchSource.OWN_DATABASE: 0,
            ResearchSource.OLX: 1,
            ResearchSource.TWO_GIS: 2,
            ResearchSource.WEB: 3,
        }
        area = case["search_area_text"].strip()
        if area:
            assert_no_private_price(area)
        branches = []
        for branch in sorted(
            single.source_plans, key=lambda branch: source_priority[branch.source],
        ):
            changes: dict[str, object] = {"item_index": index}
            if branch.source == ResearchSource.TWO_GIS:
                changes["query_terms"] = _specification_clues(
                    item["name"], item["specification_text"],
                )
            if area:
                changes["rendered_query"] = f"{branch.rendered_query} {area}"
                changes["locality"] = branch.locality.model_copy(
                    update={"raw_area_text": area},
                )
            branches.append(branch.model_copy(update=changes))
        indexed = single.model_copy(update={"source_plans": branches})
        plan = ResearchPlan.model_validate(indexed.model_copy(update={
            "plan_sha256": recompute_plan_sha256(indexed),
        }).model_dump(mode="json"))
        persisted = await persist_research_plan(
            connection, case_id=case_id, plan=plan, requested_by=owner_user_id,
        )
        if persisted.status != "planned":
            raise ImportedResearchConflict("Этот поиск уже был запущен")
        approval_id = await request_approval(
            connection,
            case_id=case_id,
            action_type="start_research",
            payload={
                "case_id": str(case_id),
                "research_run_id": str(persisted.id),
                "plan_sha256": plan.plan_sha256,
            },
            requested_by=owner_user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        await approve_request(connection, approval_id=approval_id, approved_by=owner_user_id)
        await start_approved_research(
            connection, approval_id=approval_id, allow_followup=True,
        )
        return persisted.id
