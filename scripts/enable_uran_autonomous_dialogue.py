"""Apply the Uran owner's explicit autonomous-dialogue consent to live exact drafts."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from procurement_bot.auto_send_policy import (
    URAN_CASE_ID,
    URAN_OWNER_USER_ID,
    authorize_and_enqueue_auto_whatsapp,
    enable_uran_auto_send_policy,
)
from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.whatsapp_approval_workflow import initial_draft_is_current
from scripts.manage_uran_launch import EXPECTED_BASELINE_SHA256, _read_target
from scripts.run_uran_three_item_pilot import _capture_baseline


async def activate() -> dict[str, object]:
    settings = get_settings()
    if not settings.waha_enabled or not settings.research_auto_prepare_outreach:
        raise RuntimeError("Uran dialogue requires enabled WAHA and native outreach preparation")
    pool = await create_pool(settings.postgres_dsn)
    try:
        async with pool.acquire() as connection, connection.transaction():
            owner = await connection.fetchval(
                "SELECT owner_user_id FROM procurement_cases "
                "WHERE id=$1 AND status NOT IN ('closed','cancelled') FOR UPDATE",
                URAN_CASE_ID,
            )
            if owner != URAN_OWNER_USER_ID:
                raise RuntimeError("Uran case owner or open lifecycle changed")
            target = await _read_target(connection)
            baseline = await _capture_baseline(connection, target["group_ids"])
            if baseline["baseline_hash"] != EXPECTED_BASELINE_SHA256:
                raise RuntimeError("Uran frozen 55-item financial baseline changed")
            policy_id = await enable_uran_auto_send_policy(connection)
            drafts = await connection.fetch(
                "SELECT id,payload,requested_by FROM action_approvals "
                "WHERE case_id=$1 AND action_type='send_whatsapp' "
                "AND status='requested' AND (expires_at IS NULL OR expires_at>now()) "
                "ORDER BY requested_at,id FOR UPDATE",
                URAN_CASE_ID,
            )
            queued = 0
            stale = 0
            for draft in drafts:
                payload = draft["payload"]
                if not await initial_draft_is_current(
                    connection,
                    case_id=URAN_CASE_ID,
                    conversation_id=UUID(payload["conversation_id"]),
                    logical_action_id=payload["logical_action_id"],
                    text=payload["text"],
                ):
                    stale += 1
                    continue
                job_id = await authorize_and_enqueue_auto_whatsapp(
                    connection,
                    approval_id=draft["id"],
                    case_id=URAN_CASE_ID,
                    payload=payload,
                    requested_by=draft["requested_by"],
                )
                queued += int(job_id is not None)
            return {
                "case_id": str(URAN_CASE_ID),
                "policy_id": str(policy_id),
                "live_drafts": len(drafts),
                "queued": queued,
                "stale_skipped": stale,
                "baseline_sha256": baseline["baseline_hash"],
            }
    finally:
        await pool.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(activate()), ensure_ascii=False))
