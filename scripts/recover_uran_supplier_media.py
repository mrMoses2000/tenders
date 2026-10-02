"""Recover earlier ignored media through the native Uran inbound boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

from procurement_bot.auto_send_policy import URAN_CASE_ID
from procurement_bot.config import get_settings
from procurement_bot.db import create_pool
from procurement_bot.providers.waha import WahaClient
from procurement_bot.waha_webhook_http import parse_waha_message_event
from procurement_bot.whatsapp_inbound import AuthenticatedWhatsAppInbound, accept_whatsapp_inbound
from scripts.manage_uran_launch import EXPECTED_BASELINE_SHA256, _read_target
from scripts.run_uran_three_item_pilot import _capture_baseline


async def recover(*, apply: bool = False) -> dict:
    settings = get_settings()
    pool = await create_pool(settings.postgres_dsn)
    waha = WahaClient(
        base_url=settings.waha_base_url,
        api_key=settings.require_waha_api_key(),
        session=settings.waha_session,
        enabled=settings.waha_enabled,
        timeout_seconds=settings.waha_timeout_seconds,
    )
    counts: Counter = Counter()
    try:
        async with pool.acquire() as connection:
            target = await _read_target(connection)
            baseline = await _capture_baseline(connection, target["group_ids"])
            if baseline["baseline_hash"] != EXPECTED_BASELINE_SHA256:
                raise RuntimeError("Uran original items/prices changed; recovery refused")
            conversations = await connection.fetch(
                """SELECT c.id,c.external_chat_id,c.created_at FROM supplier_conversations c
                   JOIN procurement_cases p ON p.id=c.case_id
                   WHERE c.case_id=$1 AND NOT c.opted_out AND p.status NOT IN ('closed','cancelled')
                     AND c.session_name=$2 ORDER BY c.created_at LIMIT 100""",
                URAN_CASE_ID,
                settings.waha_session,
            )
        for conversation in conversations:
            messages = await waha.fetch_chat_messages(
                conversation["external_chat_id"],
                limit=100,
                download_media=True,
            )
            counts["conversations_checked"] += 1
            for message in messages:
                if message.get("fromMe") is not False or not message.get("hasMedia"):
                    continue
                event = parse_waha_message_event(
                    {"event": "message", "session": settings.waha_session, "payload": message}
                )
                if not isinstance(event, AuthenticatedWhatsAppInbound) or event.media is None:
                    counts["unsupported_media_or_sender"] += 1
                    continue
                if event.received_at < conversation["created_at"]:
                    counts["predates_conversation"] += 1
                    continue
                counts[event.media.kind + "_found"] += 1
                if apply:
                    async with pool.acquire() as connection, connection.transaction():
                        result = await accept_whatsapp_inbound(
                            connection, event, worker_id="uran-media-recovery"
                        )
                        if result.conversation_id and result.conversation_id != conversation["id"]:
                            raise RuntimeError("recovered media resolved to another conversation")
                    counts[result.disposition.value] += 1
        return {"case_id": str(URAN_CASE_ID), "applied": apply, "counts": dict(counts)}
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(asyncio.run(recover(apply=parser.parse_args().apply)), ensure_ascii=False))
