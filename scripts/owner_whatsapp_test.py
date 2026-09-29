"""Prepare and dispatch one owner-authorized WhatsApp test through the normal outbox.

Usage on the bot host:
  python scripts/owner_whatsapp_test.py prepare --phone +7...
  python scripts/owner_whatsapp_test.py dispatch --approval-id UUID
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from procurement_bot.approvals import approve_request, request_approval
from procurement_bot.config import get_settings
from procurement_bot.conversation_targets import ConversationTargetInput, attach_conversation_target
from procurement_bot.conversations import (
    ConversationInput,
    ConversationState,
    create_conversation,
    transition_conversation_state,
)
from procurement_bot.db import create_pool
from procurement_bot.phone import normalize_phone
from procurement_bot.providers.waha import WahaClient
from procurement_bot.queue import stable_key
from procurement_bot.supplier_dialogue import WhatsAppSendCommand
from procurement_bot.suppliers import (
    OfferInput,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierInput,
    upsert_offer,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
)
from procurement_bot.whatsapp_approval_workflow import start_approved_whatsapp_send

CASE_ID = UUID("10d857bd-81e9-4001-a7b7-cb8b789b66a0")
ITEM_ID = UUID("5a68030a-1436-4244-a0e7-823458d52a73")
MESSAGE = (
    "Здравствуйте! Это тестовое сообщение закупочного бота по позиции «Пакля» "
    "для пилотной заявки НИШ Караганда. Вы добавлены как тестовый поставщик. "
    "Ответьте, пожалуйста: «Тест получил»."
)


async def prepare(phone: str) -> None:
    settings = get_settings()
    recipient = normalize_phone(phone)
    waha = WahaClient(
        base_url=settings.waha_base_url,
        api_key=settings.waha_api_key.get_secret_value(),
        session=settings.waha_session,
        enabled=settings.waha_enabled,
        timeout_seconds=settings.waha_timeout_seconds,
    )
    check = await waha.check_phone_exists(recipient)
    if not check.number_exists or not check.chat_id:
        raise RuntimeError("test number has no WhatsApp account")

    pool = await create_pool(settings.postgres_dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as connection, connection.transaction():
            owner = await connection.fetchval(
                "SELECT owner_user_id FROM procurement_cases WHERE id=$1", CASE_ID
            )
            if owner is None:
                raise RuntimeError("pilot case does not exist")
            existing = await connection.fetchrow(
                """SELECT c.supplier_id,a.normalized_value AS identity
                   FROM supplier_contacts c
                   LEFT JOIN supplier_aliases a ON a.supplier_id=c.supplier_id
                     AND a.alias_type='source_record' AND a.active
                   WHERE c.normalized_value=$1 AND c.active""",
                recipient,
            )
            test_identity = f"owner-whatsapp-test:{recipient}".casefold()
            if existing is not None and existing["identity"] != test_identity:
                raise RuntimeError("test number already belongs to a supplier")

            raw = {"purpose": "owner_authorized_whatsapp_test", "case_id": str(CASE_ID)}
            digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()
            evidence_key = stable_key("owner-whatsapp-test-evidence", CASE_ID, recipient)
            prior_evidence = await connection.fetchval(
                "SELECT id FROM source_evidence WHERE idempotency_key=$1", evidence_key
            )
            evidence_id = (
                prior_evidence
                or (
                    await upsert_source_evidence(
                        connection,
                        SourceEvidenceInput(
                            source_type="manual",
                            source_locator="owner-authorized-whatsapp-test",
                            captured_at=datetime.now(UTC),
                            content_sha256=digest,
                            raw_payload=raw,
                            idempotency_key=evidence_key,
                            title="Owner WhatsApp test contact",
                        ),
                    )
                ).id
            )
            supplier = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Тестовый поставщик — владелец бота",
                    identity_type="source_record",
                    identity_value=f"owner-whatsapp-test:{recipient}",
                    source_evidence_id=evidence_id,
                    supplier_type="individual",
                ),
            )
            contact = await upsert_supplier_contact(
                connection,
                supplier.id,
                SupplierContactInput(
                    contact_type="whatsapp",
                    display_value=recipient,
                    source_evidence_id=evidence_id,
                    label="Тестовый номер владельца; WAHA confirmed",
                    is_whatsapp_confirmed=True,
                ),
            )
            offer = await upsert_offer(
                connection,
                OfferInput(
                    request_item_id=ITEM_ID,
                    supplier_id=supplier.id,
                    supplier_product_name="Пакля — тестовый лид",
                    idempotency_key=stable_key("owner-whatsapp-test-offer", CASE_ID, recipient),
                ),
            )
            conversation = await create_conversation(
                connection,
                ConversationInput(
                    case_id=CASE_ID,
                    supplier_id=supplier.id,
                    supplier_contact_id=contact.id,
                    session_name=settings.waha_session,
                    external_chat_id=check.chat_id,
                    idempotency_key=stable_key(
                        "owner-whatsapp-test-conversation", CASE_ID, recipient
                    ),
                ),
            )
            await attach_conversation_target(
                connection,
                ConversationTargetInput(
                    conversation_id=conversation.id,
                    offer_id=offer.id,
                    request_item_id=ITEM_ID,
                    provenance_kind="operator",
                    provenance_ref="owner authorization in Codex task, 2026-09-24",
                    source_evidence_id=evidence_id,
                    idempotency_key=stable_key("owner-whatsapp-test-target", CASE_ID, recipient),
                ),
            )
            if conversation.created:
                await transition_conversation_state(
                    connection,
                    conversation_id=conversation.id,
                    expected_state=ConversationState.DRAFT,
                    new_state=ConversationState.APPROVED,
                )
            command = WhatsAppSendCommand(
                case_id=CASE_ID,
                recipient=recipient,
                text=MESSAGE,
                conversation_id=conversation.id,
                logical_action_id="owner-authorized-test-2026-09-24",
            )
            approval_id = await request_approval(
                connection,
                case_id=CASE_ID,
                action_type="send_whatsapp",
                payload=command.approval_payload(),
                requested_by=owner,
                expires_at=datetime.now(UTC) + timedelta(hours=36),
            )
            await approve_request(connection, approval_id=approval_id, approved_by=owner)
            print(
                json.dumps(
                    {"approval_id": str(approval_id), "conversation_id": str(conversation.id)}
                )
            )
    finally:
        await pool.close()


async def dispatch(approval_id: UUID) -> None:
    pool = await create_pool(get_settings().postgres_dsn, min_size=1, max_size=1)
    try:
        event_id = await start_approved_whatsapp_send(pool, approval_id)
        print(json.dumps({"outbox_event_id": str(event_id)}))
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("--phone", required=True)
    commands.add_parser("dispatch").add_argument("--approval-id", type=UUID, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        asyncio.run(prepare(args.phone))
    else:
        asyncio.run(dispatch(args.approval_id))


if __name__ == "__main__":
    main()
