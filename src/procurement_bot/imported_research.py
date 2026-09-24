"""Owner initiated research for an item imported without a Telegram parse version."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from procurement_bot.approvals import approve_request, request_approval
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.research import ResearchPlan, build_research_plan
from procurement_bot.research_repository import persist_research_plan, recompute_plan_sha256
from procurement_bot.research_workflow import start_approved_research


class ImportedResearchConflict(ValueError):
    """The selected imported item cannot start another research run."""


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
            SELECT id,city,status,source_kind
            FROM procurement_cases
            WHERE id=$1 AND owner_user_id=$2
            """,
            case_id, owner_user_id,
        )
        if case is None or case["source_kind"] != "notion_import":
            raise ImportedResearchConflict("Импортированная заявка не найдена")
        if case["status"] not in {"ready", "evaluating", "report_ready"}:
            raise ImportedResearchConflict("Дождитесь завершения текущего поиска")
        if not case["city"]:
            raise ImportedResearchConflict("У заявки не указан город поиска")
        items = await connection.fetch(
            """
            SELECT id,name,quantity,unit
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
                name=item["name"], quantity=item["quantity"], unit=item["unit"] or None,
            )],
        )
        single = build_research_plan(request, include_olx=True)
        indexed = single.model_copy(update={
            "source_plans": [branch.model_copy(update={"item_index": index})
                             for branch in single.source_plans],
        })
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
