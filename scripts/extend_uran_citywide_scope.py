"""Record the owner's citywide instruction without starting research or sending messages."""

from __future__ import annotations

import argparse
import asyncio
import json

from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.research_scope import append_citywide_scope_revision
from scripts.run_uran_three_item_pilot import (
    CASE_ID,
    PILOT_LINES,
    RUN_ID,
    SCHEDULER_LOCK_KEY,
    _capture_baseline,
    _read_target,
)


async def extend(*, apply: bool, authorization_reference: str | None) -> dict:
    if apply and not authorization_reference:
        raise ValueError("--apply requires the direct human instruction reference")
    pool = await create_pool(get_settings().postgres_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEDULER_LOCK_KEY)
            target = await _read_target(connection)
            before = await _capture_baseline(connection, target["selected_group_ids"])
            current = await connection.fetchrow(
                "SELECT * FROM research_scope_revisions WHERE research_run_id=$1 "
                "ORDER BY revision DESC LIMIT 1", RUN_ID,
            )
            if not apply:
                return {"mode": "read_only", "case_id": str(CASE_ID),
                        "current_revision": dict(current) if current else None,
                        "baseline_hash": before["baseline_hash"]}
            owner = await connection.fetchval(
                "SELECT owner_user_id FROM procurement_cases WHERE id=$1", CASE_ID
            )
            revision = await append_citywide_scope_revision(
                connection, RUN_ID, owner, authorization_reference,
                "Северное Кольцо",
            )
            if current is None or current["id"] != revision.id:
                # Existing observations and exclusions survive. Only the three
                # pilot cursors restart against the newly versioned profile.
                await connection.execute(
                    """UPDATE research_branch_checkpoints cp
                    SET semantic_intent_ordinal=0,
                        search_cursor_state='{"current":null,"seen":[]}'::jsonb,
                        source_progress_token=NULL,owner_generation=owner_generation+1,
                        max_batches=GREATEST(max_batches,80),updated_at=now()
                    FROM research_item_groups grp JOIN request_items item
                      ON item.id=grp.request_item_id
                    WHERE cp.group_id=grp.id AND grp.research_run_id=$1
                      AND item.line_number=ANY($2::int[])""",
                    RUN_ID, list(PILOT_LINES),
                )
                coordinator = ParallelResearchRunExecutor(pool, None)
                for group_id in target["selected_group_ids"].values():
                    await coordinator._reconcile_group(connection, group_id)
            after = await _capture_baseline(connection, target["selected_group_ids"])
            if (before["baseline_hash"] != after["baseline_hash"]
                    or before["nonpilot_state_hash"] != after["nonpilot_state_hash"]):
                raise RuntimeError("citywide revision changed frozen data or nonpilot state")
            return {"mode": "applied", "revision": revision.to_context(),
                    "baseline_hash": after["baseline_hash"],
                    "nonpilot_state_unchanged": True, "all_groups_remain_paused": True}
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization-reference")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(extend(
        apply=args.apply, authorization_reference=args.authorization_reference
    )), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
