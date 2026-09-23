from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.approvals import payload_sha256
from procurement_bot.errors import ValidationBlocked
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_outbox, stable_key
from procurement_bot.supplier_dialogue import (
    DEFAULT_SEND_POLICY,
    SendContext,
    assert_send_allowed,
    assert_supplier_safe_text,
)

_APPROVAL_ACTION = "send_whatsapp"
_OUTBOX_EVENT = "whatsapp.send_text"
_PAYLOAD_KEYS = {
    "case_id",
    "channel",
    "recipient",
    "text",
    "conversation_id",
    "logical_action_id",
    "is_followup",
}
_SENDABLE_CONVERSATION_STATES = {
    "approved",
    "waiting_availability",
    "waiting_spec",
    "waiting_price",
    "escalated",
}


class WhatsAppSendDenied(RuntimeError):
    """Persisted approval or conversation state does not authorise the send."""


class WhatsAppSendStateConflict(WhatsAppSendDenied):
    """An idempotency identity is already attached to different durable state."""


class _SendPayload:
    __slots__ = (
        "canonical",
        "case_id",
        "conversation_id",
        "is_followup",
        "logical_action_id",
        "recipient",
        "text",
    )

    def __init__(
        self,
        *,
        case_id: UUID,
        recipient: str,
        text: str,
        conversation_id: UUID,
        logical_action_id: str,
        is_followup: bool,
    ) -> None:
        self.case_id = case_id
        self.recipient = recipient
        self.text = text
        self.conversation_id = conversation_id
        self.logical_action_id = logical_action_id
        self.is_followup = is_followup
        self.canonical: dict[str, Any] = {
            "case_id": str(case_id),
            "channel": "whatsapp",
            "recipient": recipient,
            "text": text,
            "conversation_id": str(conversation_id),
            "logical_action_id": logical_action_id,
            "is_followup": is_followup,
        }


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WhatsAppSendDenied(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise WhatsAppSendDenied(f"{field} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise WhatsAppSendDenied(f"{field} keys must be strings")
    return dict(value)


def _canonical_uuid(value: object, *, field: str) -> UUID:
    if not isinstance(value, str):
        raise WhatsAppSendDenied(f"{field} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise WhatsAppSendDenied(f"{field} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise WhatsAppSendDenied(f"{field} must be a canonical UUID string")
    return parsed


def _parse_payload(value: object) -> _SendPayload:
    payload = _mapping(value, field="approval payload")
    if set(payload) != _PAYLOAD_KEYS:
        raise WhatsAppSendDenied(
            "send_whatsapp approval payload must contain exactly case_id, channel, "
            "recipient, text, conversation_id, logical_action_id, and is_followup"
        )
    if payload["channel"] != "whatsapp":
        raise WhatsAppSendDenied("send_whatsapp approval channel is invalid")

    recipient = payload["recipient"]
    text = payload["text"]
    logical_action_id = payload["logical_action_id"]
    is_followup = payload["is_followup"]
    if not isinstance(recipient, str):
        raise WhatsAppSendDenied("recipient must be a phone-number string")
    try:
        normalize_phone(recipient)
    except ValueError as exc:
        raise WhatsAppSendDenied("recipient is not a valid phone number") from exc
    if not isinstance(text, str) or len(text) > 4096:
        raise WhatsAppSendDenied("WhatsApp text is invalid")
    try:
        assert_supplier_safe_text(text)
    except ValidationBlocked as exc:
        raise WhatsAppSendDenied(str(exc)) from exc
    if (
        not isinstance(logical_action_id, str)
        or not logical_action_id.strip()
        or logical_action_id != logical_action_id.strip()
        or len(logical_action_id) > 200
    ):
        raise WhatsAppSendDenied("logical_action_id is invalid")
    if not isinstance(is_followup, bool):
        raise WhatsAppSendDenied("is_followup must be a boolean")

    return _SendPayload(
        case_id=_canonical_uuid(payload["case_id"], field="case_id"),
        recipient=recipient,
        text=text,
        conversation_id=_canonical_uuid(
            payload["conversation_id"], field="conversation_id"
        ),
        logical_action_id=logical_action_id,
        is_followup=is_followup,
    )


@asynccontextmanager
async def _connection(
    database: asyncpg.Pool | asyncpg.Connection,
) -> AsyncIterator[asyncpg.Connection]:
    acquire = getattr(database, "acquire", None)
    if acquire is None:
        yield database  # type: ignore[misc]
        return
    async with acquire() as connection:
        yield connection


def _now_utc() -> datetime:
    return datetime.now(UTC)


async def _existing_exact_outbox(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID,
    payload: _SendPayload,
    idempotency_key: str,
) -> UUID:
    event = await connection.fetchrow(
        """
        SELECT id,event_type,payload,idempotency_key,approval_id
        FROM outbox_events
        WHERE approval_id=$1 OR idempotency_key=$2
        FOR SHARE
        """,
        approval_id,
        idempotency_key,
    )
    if event is None:
        raise WhatsAppSendStateConflict(
            "consumed send_whatsapp approval has no matching outbox event"
        )
    try:
        event_payload = _mapping(event["payload"], field="outbox payload")
    except WhatsAppSendDenied as exc:
        raise WhatsAppSendStateConflict(str(exc)) from exc
    if (
        event["event_type"] != _OUTBOX_EVENT
        or event["approval_id"] != approval_id
        or event["idempotency_key"] != idempotency_key
        or event_payload != payload.canonical
    ):
        raise WhatsAppSendStateConflict(
            "consumed send_whatsapp approval is linked to a mismatched outbox event"
        )
    return event["id"]


async def start_approved_whatsapp_send(
    database: asyncpg.Pool | asyncpg.Connection,
    approval_id: UUID,
) -> UUID:
    """Consume an exact WhatsApp approval and atomically enqueue its outbox event.

    This function is a control-plane boundary: it never invokes WAHA or any
    other provider. Provider delivery performs the mutable policy checks again.
    """

    if not isinstance(approval_id, UUID):
        raise TypeError("approval_id must be a UUID")

    now = _now_utc()
    async with _connection(database) as connection, connection.transaction():
        approval = await connection.fetchrow(
            """
            SELECT id,case_id,action_type,payload,payload_sha256,status,
                approved_by,expires_at,consumed_at,
                (expires_at IS NULL OR expires_at>now()) AS is_live,
                (expires_at IS NULL OR consumed_at<expires_at) AS consumed_when_live
            FROM action_approvals
            WHERE id=$1
            FOR UPDATE
            """,
            approval_id,
        )
        if approval is None:
            raise WhatsAppSendDenied("send_whatsapp approval does not exist")
        if approval["action_type"] != _APPROVAL_ACTION:
            raise WhatsAppSendDenied("approval is for a different action")

        payload = _parse_payload(approval["payload"])
        if approval["case_id"] != payload.case_id:
            raise WhatsAppSendDenied("approval case does not match its payload")
        if approval["payload_sha256"] != payload_sha256(payload.canonical):
            raise WhatsAppSendDenied("approval payload hash is invalid")
        if approval["approved_by"] is None:
            raise WhatsAppSendDenied("send_whatsapp approval has no approving user")

        idempotency_key = stable_key(
            "whatsapp-send", payload.conversation_id, payload.logical_action_id
        )
        if approval["status"] == "consumed":
            if approval["consumed_at"] is None or not approval["consumed_when_live"]:
                raise WhatsAppSendDenied("approval consumption is invalid")
            return await _existing_exact_outbox(
                connection,
                approval_id=approval_id,
                payload=payload,
                idempotency_key=idempotency_key,
            )
        if approval["status"] != "approved" or not approval["is_live"]:
            raise WhatsAppSendDenied("approval is not approved and unexpired")

        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            idempotency_key,
        )
        collision = await connection.fetchrow(
            """
            SELECT id,event_type,payload,idempotency_key,approval_id
            FROM outbox_events WHERE idempotency_key=$1 FOR SHARE
            """,
            idempotency_key,
        )
        if collision is not None:
            raise WhatsAppSendStateConflict(
                "WhatsApp logical action is already attached to an outbox event"
            )

        local = now.astimezone(ZoneInfo(DEFAULT_SEND_POLICY.timezone))
        day_started_at = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            UTC
        )
        conversation = await connection.fetchrow(
            """
            SELECT c.case_id,c.state,c.opted_out,c.outbound_count,c.followup_count,
                   c.last_outbound_at,contact.contact_type,contact.normalized_value,
                   contact.active,
                   (SELECT count(*) FROM supplier_conversation_messages AS message
                    WHERE message.conversation_id=c.id AND message.direction='outbound'
                      AND message.received_at >= $2) AS sent_today
            FROM supplier_conversations AS c
            JOIN supplier_contacts AS contact ON contact.id=c.supplier_contact_id
            WHERE c.id=$1
            FOR UPDATE OF c,contact
            """,
            payload.conversation_id,
            day_started_at,
        )
        if conversation is None:
            raise WhatsAppSendDenied("approved WhatsApp conversation does not exist")
        if conversation["case_id"] != payload.case_id:
            raise WhatsAppSendDenied("WhatsApp conversation belongs to a different case")
        if conversation["state"] not in _SENDABLE_CONVERSATION_STATES:
            raise WhatsAppSendDenied("WhatsApp conversation is not in a sendable state")
        if conversation["opted_out"]:
            raise WhatsAppSendDenied("supplier opted out")
        if not conversation["active"] or conversation["contact_type"] not in {
            "phone",
            "whatsapp",
        }:
            raise WhatsAppSendDenied("WhatsApp supplier contact is not active")
        persisted_contact = conversation["normalized_value"]
        if not isinstance(persisted_contact, str):
            raise WhatsAppSendDenied("persisted WhatsApp contact is invalid")
        try:
            persisted_recipient = normalize_phone(persisted_contact)
        except ValueError as exc:
            raise WhatsAppSendDenied("persisted WhatsApp contact is invalid") from exc
        if persisted_recipient != normalize_phone(payload.recipient):
            raise WhatsAppSendDenied("approved recipient does not match the conversation contact")

        context = SendContext(
            enabled=True,
            opted_out=False,
            messages_sent_today=int(conversation["sent_today"] or 0),
            messages_sent_in_conversation=int(conversation["outbound_count"]),
            followups_sent=int(conversation["followup_count"]),
            last_sent_at=conversation["last_outbound_at"],
        )
        try:
            assert_send_allowed(
                context,
                now=now,
                policy=DEFAULT_SEND_POLICY,
                is_followup=payload.is_followup,
            )
        except (ValueError, ValidationBlocked) as exc:
            raise WhatsAppSendDenied(str(exc)) from exc

        consumed = await connection.execute(
            """
            UPDATE action_approvals
            SET status='consumed',consumed_at=now()
            WHERE id=$1 AND status='approved'
              AND approved_by IS NOT NULL
              AND (expires_at IS NULL OR expires_at>now())
            """,
            approval_id,
        )
        if consumed != "UPDATE 1":
            raise WhatsAppSendDenied("approval changed before it could be consumed")
        event_id = await enqueue_outbox(
            connection,
            event_type=_OUTBOX_EVENT,
            payload=payload.canonical,
            idempotency_key=idempotency_key,
            max_attempts=3,
            approval_id=approval_id,
        )
        if event_id is None:
            return await _existing_exact_outbox(
                connection,
                approval_id=approval_id,
                payload=payload,
                idempotency_key=idempotency_key,
            )
        return event_id
