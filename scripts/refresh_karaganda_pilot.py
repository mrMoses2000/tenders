"""Run one bounded, fresh research pass for a line of the derived Karaganda pilot.

The default is read-only. --apply uses the existing approval and job workflow.
No supplier message is approved or sent by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from procurement_bot.approvals import approve_request, request_approval
from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.research import ResearchPlan, SourcePlan
from procurement_bot.research_repository import persist_research_plan, recompute_plan_sha256
from procurement_bot.research_workflow import start_approved_research

CASE_ID = UUID("10d857bd-81e9-4001-a7b7-cb8b789b66a0")


async def refresh(
    line_number: int, *, apply: bool, olx_only: bool = False,
    ttl_hours: int | None = None,
) -> dict[str, object]:
    if ttl_hours is None:
        ttl_hours = 2 if olx_only else 1
    pool = await create_pool(get_settings().postgres_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection, connection.transaction():
            case = await connection.fetchrow(
                """SELECT c.id,c.owner_user_id,c.status,
                          EXISTS(SELECT 1 FROM case_derivations d
                                 WHERE d.derived_case_id=c.id) AS is_derived
                   FROM procurement_cases c WHERE c.id=$1""",
                CASE_ID,
            )
            if case is None or not case["is_derived"]:
                raise ValueError("the derived pilot case was not found")
            item = await connection.fetchrow(
                "SELECT id,name FROM request_items WHERE case_id=$1 AND line_number=$2",
                CASE_ID, line_number,
            )
            if item is None:
                raise ValueError("the requested pilot line was not found")
            rows = await connection.fetch(
                """SELECT id,search_scope FROM research_runs
                   WHERE case_id=$1
                     AND jsonb_array_length(search_scope->'plan'->'source_plans')=4
                   ORDER BY created_at ASC""",
                CASE_ID,
            )
            original = next(
                (
                    row for row in rows
                    if row["search_scope"]["plan"]["source_plans"][0]["item_index"]
                    == line_number - 1
                ),
                None,
            )
            if original is None:
                raise ValueError("the original four-source plan was not found")
            scope = original["search_scope"]
            old_plan = ResearchPlan.model_validate({
                **scope["plan"], "plan_sha256": scope["plan_sha256"],
            })
            branches = [
                SourcePlan.model_validate(branch.model_copy(
                    update={"result_ttl_hours": ttl_hours},
                ).model_dump(mode="json"))
                for branch in old_plan.source_plans
                if not olx_only or branch.source.value == "olx"
            ]
            draft = old_plan.model_copy(update={"source_plans": branches})
            plan = ResearchPlan.model_validate(draft.model_copy(update={
                "plan_sha256": recompute_plan_sha256(draft),
            }).model_dump(mode="json"))
            result: dict[str, object] = {
                "line": line_number,
                "item": item["name"],
                "case_status": case["status"],
                "original_run_id": str(original["id"]),
                "plan_sha256": plan.plan_sha256,
                "sources": [branch.source.value for branch in plan.source_plans],
                "applied": apply,
            }
            if not apply:
                return result
            if case["status"] not in {"ready", "evaluating", "report_ready"}:
                raise ValueError("wait for the previous pilot line to finish")
            persisted = await persist_research_plan(
                connection, case_id=CASE_ID, plan=plan,
                requested_by=case["owner_user_id"],
            )
            result["research_run_id"] = str(persisted.id)
            if persisted.status != "planned":
                raise ValueError(f"this fresh pilot plan already has status {persisted.status}")
            approval_id = await request_approval(
                connection,
                case_id=CASE_ID,
                action_type="start_research",
                payload={
                    "case_id": str(CASE_ID),
                    "research_run_id": str(persisted.id),
                    "plan_sha256": plan.plan_sha256,
                },
                requested_by=case["owner_user_id"],
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            await approve_request(
                connection, approval_id=approval_id,
                approved_by=case["owner_user_id"],
            )
            started = await start_approved_research(
                connection, approval_id=approval_id, allow_followup=True,
            )
            result["job_id"] = str(started.job_id)
            return result
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("line_number", type=int, choices=(1, 2, 3, 4))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--olx-only", action="store_true")
    parser.add_argument("--ttl-hours", type=int)
    args = parser.parse_args()
    print(json.dumps(
        asyncio.run(refresh(
            args.line_number, apply=args.apply, olx_only=args.olx_only,
            ttl_hours=args.ttl_hours,
        )),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
