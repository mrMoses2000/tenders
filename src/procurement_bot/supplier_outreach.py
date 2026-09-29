from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg

from procurement_bot.approvals import request_approval
from procurement_bot.conversation_targets import (
    ConversationTargetInput,
    attach_conversation_target,
)
from procurement_bot.conversations import (
    ConversationInput,
    ConversationState,
    create_conversation,
    transition_conversation_state,
)
from procurement_bot.offer_comparison import normalize_goods_unit
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_outbox, stable_key
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence
from procurement_bot.supplier_dialogue import (
    SupplierInquiry,
    WhatsAppSendCommand,
    build_availability_inquiry,
)


@dataclass(frozen=True, slots=True)
class OutreachPreparationResult:
    case_id: UUID
    proposals_created: int
    suppliers_without_whatsapp: int
    skipped_existing: int


async def prepare_supplier_outreach(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    session_name: str,
    max_proposals: int = 100,
) -> OutreachPreparationResult:
    """Create approval-gated initial messages; never call the provider."""

    if not session_name.strip():
        raise ValueError("session_name must be non-empty")
    if not 1 <= max_proposals <= 100:
        raise ValueError("max_proposals must be between 1 and 100")
    async with connection.transaction():
        case = await connection.fetchrow(
            """
            SELECT procurement_case.owner_user_id,owner.telegram_id,procurement_case.status
            FROM procurement_cases AS procurement_case
            JOIN users AS owner ON owner.id=procurement_case.owner_user_id
            WHERE procurement_case.id=$1 AND owner.active
            FOR SHARE OF procurement_case,owner
            """,
            case_id,
        )
        if case is None or case["telegram_id"] is None:
            raise ValueError("outreach case or active owner does not exist")
        if case["status"] not in {"evaluating", "contacting", "report_ready"}:
            raise ValueError("case is not ready for supplier outreach")

        candidates = await connection.fetch(
            """
            SELECT DISTINCT ON (offer.supplier_id)
                   offer.id AS offer_id,offer.supplier_id,offer.status AS offer_status,
                   item.id AS request_item_id,item.line_number,item.name,
                   item.quantity,item.unit,item.specification_text,
                   contact.id AS contact_id,contact.normalized_value,
                   evidence.id AS contact_evidence_id
            FROM offers AS offer
            JOIN request_items AS item ON item.id=offer.request_item_id
            JOIN supplier_contacts AS contact
              ON contact.supplier_id=offer.supplier_id
             AND contact.active AND contact.contact_type='whatsapp'
            JOIN source_evidence AS evidence ON evidence.id=contact.source_evidence_id
            WHERE item.case_id=$1
              AND __CONTACT_SCOPE__
              AND offer.status=ANY($2::text[])
              AND NOT EXISTS (
                  SELECT 1 FROM supplier_conversation_targets target
                  WHERE target.offer_id=offer.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM supplier_conversations existing
                  WHERE existing.supplier_id=offer.supplier_id
                    AND (existing.opted_out OR existing.state NOT IN
                         ('complete','closed_no_reply','opted_out'))
              )
            ORDER BY offer.supplier_id,
                     CASE offer.status WHEN 'exact' THEN 0 WHEN 'confirm' THEN 1 ELSE 2 END,
                     item.line_number,offer.created_at,contact.first_seen_at
            LIMIT $3
            """.replace(
                "__CONTACT_SCOPE__", case_owned_supplier_contact_evidence(
                    contact_alias="contact", evidence_alias="evidence"
                ),
            ),
            case_id,
            ["exact", "confirm", "lead"],
            max_proposals,
        )
        suppliers_without_whatsapp = int(
            await connection.fetchval(
                """
                SELECT count(DISTINCT offer.supplier_id)
                FROM offers AS offer
                JOIN request_items AS item ON item.id=offer.request_item_id
                WHERE item.case_id=$1 AND offer.status=ANY($2::text[])
                  AND NOT EXISTS (
                      SELECT 1 FROM supplier_contacts contact
                      JOIN source_evidence evidence
                        ON evidence.id=contact.source_evidence_id
                      WHERE contact.supplier_id=offer.supplier_id
                        AND contact.active AND contact.contact_type='whatsapp'
                        AND __CONTACT_SCOPE__
                  )
                """.replace(
                    "__CONTACT_SCOPE__", case_owned_supplier_contact_evidence(
                        contact_alias="contact", evidence_alias="evidence"
                    ),
                ),
                case_id,
                ["exact", "confirm", "lead"],
            )
            or 0
        )
        created = 0
        skipped = 0
        for candidate in candidates:
            recipient = normalize_phone(candidate["normalized_value"])
            external_chat_id = recipient.removeprefix("+") + "@c.us"
            conversation_key = stable_key(
                "supplier-conversation",
                case_id,
                candidate["supplier_id"],
                candidate["contact_id"],
                candidate["offer_id"],
            )
            existing_transport = await connection.fetchrow(
                """
                SELECT idempotency_key FROM supplier_conversations
                WHERE case_id=$1 AND supplier_id=$2 AND supplier_contact_id=$3
                  AND session_name=$4 AND external_chat_id=$5
                  AND state NOT IN ('complete','closed_no_reply','opted_out')
                FOR UPDATE
                """,
                case_id,
                candidate["supplier_id"],
                candidate["contact_id"],
                session_name,
                external_chat_id,
            )
            if (
                existing_transport is not None
                and existing_transport["idempotency_key"] != conversation_key
            ):
                # Preserve a pre-existing manual or test dialogue and its exact
                # approval payload; this campaign must not adopt it implicitly.
                skipped += 1
                continue
            conversation = await create_conversation(
                connection,
                ConversationInput(
                    case_id=case_id,
                    supplier_id=candidate["supplier_id"],
                    supplier_contact_id=candidate["contact_id"],
                    session_name=session_name,
                    external_chat_id=external_chat_id,
                    idempotency_key=conversation_key,
                    preferred_language="ru",
                ),
            )
            state = await connection.fetchrow(
                "SELECT state,outbound_count FROM supplier_conversations WHERE id=$1 FOR UPDATE",
                conversation.id,
            )
            if state is None:
                raise RuntimeError("created supplier conversation vanished")
            if state["outbound_count"] > 0 or state["state"] not in {"draft", "approved"}:
                skipped += 1
                continue
            if state["state"] == "draft":
                transitioned = await transition_conversation_state(
                    connection,
                    conversation_id=conversation.id,
                    expected_state=ConversationState.DRAFT,
                    new_state=ConversationState.APPROVED,
                )
                if not transitioned:
                    raise RuntimeError("supplier conversation could not enter approval stage")
            await attach_conversation_target(
                connection,
                ConversationTargetInput(
                    conversation_id=conversation.id,
                    offer_id=candidate["offer_id"],
                    request_item_id=candidate["request_item_id"],
                    provenance_kind="research",
                    provenance_ref=f"offer:{candidate['offer_id']}",
                    source_evidence_id=candidate["contact_evidence_id"],
                    idempotency_key=stable_key(
                        "conversation-target",
                        conversation.id,
                        candidate["offer_id"],
                    ),
                ),
            )
            inquiry = SupplierInquiry(
                product_name=str(candidate["name"]),
                quantity=_quantity(candidate["quantity"], candidate["unit"]),
                required_spec=(
                    str(candidate["specification_text"]).strip()
                    or "требования из заявки"
                ),
            )
            command = WhatsAppSendCommand(
                case_id=case_id,
                recipient=recipient,
                text=build_availability_inquiry(inquiry, "ru"),
                conversation_id=conversation.id,
                logical_action_id=f"initial-availability:{candidate['offer_id']}",
            )
            approval_id = await request_approval(
                connection,
                case_id=case_id,
                action_type="send_whatsapp",
                payload=command.approval_payload(),
                requested_by=case["owner_user_id"],
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            await enqueue_outbox(
                connection,
                event_type="telegram.send_message",
                payload={
                    "chat_id": int(case["telegram_id"]),
                    "text": (
                        f"Черновик поставщику по позиции №{candidate['line_number']}:\n\n"
                        f"{command.text}"
                    ),
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Отправить в WhatsApp",
                                    "callback_data": f"wa:a:{approval_id}",
                                },
                                {
                                    "text": "Отмена",
                                    "callback_data": f"wa:r:{approval_id}",
                                },
                            ]
                        ]
                    },
                },
                idempotency_key=stable_key("telegram.outreach-approval", approval_id),
            )
            created += 1

        await connection.execute(
            """
            INSERT INTO workflow_events(
                case_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES ($1,'supplier_outreach_prepared','worker','outreach',$2,$3::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            case_id,
            stable_key("supplier-outreach-prepared", case_id),
            {
                "proposals_created": created,
                "suppliers_without_whatsapp": suppliers_without_whatsapp,
                "skipped_existing": skipped,
            },
        )
        return OutreachPreparationResult(
            case_id=case_id,
            proposals_created=created,
            suppliers_without_whatsapp=suppliers_without_whatsapp,
            skipped_existing=skipped,
        )


def _quantity(value: Decimal | None, unit: str | None) -> str:
    quantity = "количество нужно уточнить" if value is None else format(value, "f")
    if "." in quantity:
        quantity = quantity.rstrip("0").rstrip(".")
    display_unit = {
        "piece": "шт.", "kg": "кг", "g": "г", "m": "м", "m2": "м²",
        "roll": "рул.", "liter": "л",
    }.get(normalize_goods_unit(unit), str(unit or "").strip())
    suffix = f" {display_unit}" if display_unit else ""
    return quantity + suffix
