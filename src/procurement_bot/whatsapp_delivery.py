from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.conversations import OutboundMessageInput, record_outbound_message
from procurement_bot.phone import normalize_phone
from procurement_bot.providers.waha import WahaSendResult
from procurement_bot.queue import ClaimedOutbox
from procurement_bot.supplier_dialogue import (
    DEFAULT_SEND_POLICY,
    SendContext,
    SendPolicy,
    assert_send_allowed,
    assert_supplier_safe_text,
)

_ALLOWED_STATES = {
    "approved",
    "waiting_availability",
    "waiting_spec",
    "waiting_price",
    "escalated",
}
_PAYLOAD_KEYS = {
    "case_id",
    "channel",
    "recipient",
    "text",
    "conversation_id",
    "logical_action_id",
    "is_followup",
}


@dataclass(frozen=True, slots=True)
class WhatsAppDeliveryPayload:
    case_id: UUID
    recipient: str
    text: str
    conversation_id: UUID
    logical_action_id: str
    is_followup: bool


@dataclass(frozen=True, slots=True)
class WhatsAppDeliveryContext:
    payload: WhatsAppDeliveryPayload
    session_name: str
    external_chat_id: str


def _canonical_uuid(value: object, *, field: str) -> UUID:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID string")
    return parsed


def parse_whatsapp_delivery_payload(
    value: Mapping[str, Any],
) -> WhatsAppDeliveryPayload:
    if not isinstance(value, Mapping) or set(value) != _PAYLOAD_KEYS:
        raise ValueError("WhatsApp payload has an unknown shape")
    if value["channel"] != "whatsapp":
        raise ValueError("WhatsApp payload channel is invalid")
    text = value["text"]
    action = value["logical_action_id"]
    followup = value["is_followup"]
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("WhatsApp text is invalid")
    if not isinstance(action, str) or not action.strip() or len(action) > 200:
        raise ValueError("WhatsApp logical action is invalid")
    if not isinstance(followup, bool):
        raise ValueError("WhatsApp is_followup must be a boolean")
    assert_supplier_safe_text(text)
    recipient = normalize_phone(str(value["recipient"]))
    return WhatsAppDeliveryPayload(
        case_id=_canonical_uuid(value["case_id"], field="case_id"),
        recipient=recipient,
        text=text.strip(),
        conversation_id=_canonical_uuid(
            value["conversation_id"], field="conversation_id"
        ),
        logical_action_id=action.strip(),
        is_followup=followup,
    )


async def assert_whatsapp_delivery_allowed(
    pool: asyncpg.Pool,
    event: ClaimedOutbox,
    *,
    now: datetime | None = None,
    policy: SendPolicy = DEFAULT_SEND_POLICY,
) -> WhatsAppDeliveryContext:
    """Recheck mutable dialogue policy immediately before the provider call."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("delivery time must be timezone-aware")
    payload = parse_whatsapp_delivery_payload(event.payload)
    local = current.astimezone(ZoneInfo(policy.timezone))
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_started_at = local_midnight.astimezone(UTC)
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """
            SELECT c.case_id,c.session_name,c.external_chat_id,c.state,c.opted_out,
                   c.outbound_count,c.followup_count,c.last_outbound_at,
                   contact.normalized_value,contact.active,
                   (SELECT count(*) FROM supplier_conversation_messages AS message
                    WHERE message.conversation_id=c.id AND message.direction='outbound'
                      AND message.received_at >= $2) AS sent_today
            FROM supplier_conversations AS c
            JOIN supplier_contacts AS contact ON contact.id=c.supplier_contact_id
            WHERE c.id=$1
            FOR SHARE OF c,contact
            """,
            payload.conversation_id,
            day_started_at,
        )
        if row is None:
            raise ValueError("WhatsApp conversation does not exist")
        if row["case_id"] != payload.case_id:
            raise ValueError("WhatsApp conversation belongs to another case")
        if not row["active"] or normalize_phone(row["normalized_value"]) != payload.recipient:
            raise ValueError("WhatsApp recipient does not match the active contact")
        first_event = await connection.fetchval(
            """
            SELECT id FROM outbox_events
            WHERE event_type='whatsapp.send_text'
              AND payload->>'conversation_id'=$1
              AND status IN ('pending','retry','sending')
            ORDER BY created_at,id
            LIMIT 1
            """,
            str(payload.conversation_id),
        )
        if first_event != event.id:
            raise RuntimeError("an earlier WhatsApp action is still pending")
        context = SendContext(
            enabled=row["state"] in _ALLOWED_STATES,
            opted_out=bool(row["opted_out"]),
            messages_sent_today=int(row["sent_today"] or 0),
            messages_sent_in_conversation=int(row["outbound_count"]),
            followups_sent=int(row["followup_count"]),
            last_sent_at=row["last_outbound_at"],
        )
        assert_send_allowed(
            context,
            now=current,
            policy=policy,
            is_followup=payload.is_followup,
        )
    return WhatsAppDeliveryContext(
        payload=payload,
        session_name=row["session_name"],
        external_chat_id=row["external_chat_id"],
    )


async def complete_whatsapp_delivery(
    pool: asyncpg.Pool,
    event: ClaimedOutbox,
    context: WhatsAppDeliveryContext,
    result: WahaSendResult,
    *,
    received_at: datetime | None = None,
) -> bool:
    """Commit sent outbox state and immutable conversation evidence together."""

    timestamp = received_at or datetime.now(UTC)
    async with pool.acquire() as connection, connection.transaction():
        completed = await connection.execute(
            """
            UPDATE outbox_events
            SET status='sent',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
                external_id=$3,sent_at=$4
            WHERE id=$1 AND status='sending' AND locked_by=$2
              AND lease_expires_at>now()
            """,
            event.id,
            event.locked_by,
            result.external_message_id,
            timestamp,
        )
        if completed != "UPDATE 1":
            return False
        await record_outbound_message(
            connection,
            OutboundMessageInput(
                conversation_id=context.payload.conversation_id,
                outbox_event_id=event.id,
                session_name=context.session_name,
                external_chat_id=context.external_chat_id,
                external_message_id=result.external_message_id,
                received_at=timestamp,
                raw_payload=result.raw,
                text_content=context.payload.text,
                is_followup=context.payload.is_followup,
            ),
        )
        if context.payload.logical_action_id.startswith("initial-availability:"):
            transitioned = await connection.execute(
                """
                UPDATE supplier_conversations
                SET state='waiting_availability',updated_at=now()
                WHERE id=$1 AND state='approved'
                """,
                context.payload.conversation_id,
            )
            if transitioned != "UPDATE 1":
                raise RuntimeError("initial outreach conversation state changed before delivery")
    return True
