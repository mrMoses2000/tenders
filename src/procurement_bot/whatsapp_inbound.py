from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.conversations import (
    InboundMessageInput,
    WebhookEventInput,
    accept_webhook_event,
    mark_webhook_ignored,
    mark_webhook_processing,
    record_inbound_message,
)
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.supplier_dialogue import is_opt_out


class WhatsAppInboundConflict(RuntimeError):
    """The durable webhook state cannot be safely applied by this service."""


class WhatsAppInboundDisposition(StrEnum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    QUARANTINED_UNKNOWN_SENDER = "quarantined_unknown_sender"
    QUARANTINED_NO_ACTIVE_CONVERSATION = "quarantined_no_active_conversation"
    QUARANTINED_AMBIGUOUS_SENDER = "quarantined_ambiguous_sender"
    QUARANTINED_AMBIGUOUS_CONVERSATION = "quarantined_ambiguous_conversation"


_QUARANTINE_REASON_TO_DISPOSITION = {
    "unknown_sender": WhatsAppInboundDisposition.QUARANTINED_UNKNOWN_SENDER,
    "no_active_conversation": (
        WhatsAppInboundDisposition.QUARANTINED_NO_ACTIVE_CONVERSATION
    ),
    "ambiguous_sender": WhatsAppInboundDisposition.QUARANTINED_AMBIGUOUS_SENDER,
    "ambiguous_conversation": (
        WhatsAppInboundDisposition.QUARANTINED_AMBIGUOUS_CONVERSATION
    ),
}
_TERMINAL_CONVERSATION_STATES = ("complete", "closed_no_reply", "opted_out")


@dataclass(frozen=True, slots=True)
class AuthenticatedWhatsAppInbound:
    """A provider-independent message after transport authentication and parsing."""

    provider_event_id: str
    session_name: str
    sender_phone: str
    provider_message_id: str
    received_at: datetime
    text: str

    def __post_init__(self) -> None:
        _require_text("provider_event_id", self.provider_event_id, max_length=500)
        _require_text("session_name", self.session_name, max_length=200)
        _require_text("provider_message_id", self.provider_message_id, max_length=500)
        _require_text("text", self.text, max_length=100_000)
        normalize_phone(self.sender_phone)
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")

    @property
    def normalized_sender_phone(self) -> str:
        return normalize_phone(self.sender_phone)

    def evidence_payload(self) -> dict[str, str]:
        """Return the minimal authenticated transport fact stored in the inbox."""

        return {
            "provider_event_id": self.provider_event_id,
            "session_name": self.session_name,
            "sender_phone": self.normalized_sender_phone,
            "provider_message_id": self.provider_message_id,
            "received_at": self.received_at.isoformat(),
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class WhatsAppInboundResult:
    webhook_event_id: UUID
    disposition: WhatsAppInboundDisposition
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    job_id: UUID | None = None


async def accept_whatsapp_inbound(
    connection: asyncpg.Connection,
    event: AuthenticatedWhatsAppInbound,
    *,
    worker_id: str = "whatsapp-inbound",
) -> WhatsAppInboundResult:
    """Durably apply one authenticated inbound message without external side effects.

    The caller owns provider authentication and normalization of the webhook shape.
    This service stores only transport evidence, resolves an existing supplier
    conversation, applies opt-out synchronously, and schedules opaque internal work.
    """

    _require_text("worker_id", worker_id, max_length=200)
    raw_payload = event.evidence_payload()
    webhook_key = stable_key(
        "whatsapp-webhook", event.session_name, event.provider_event_id
    )

    async with connection.transaction():
        accepted = await accept_webhook_event(
            connection,
            WebhookEventInput(
                session_name=event.session_name,
                event_name="message",
                external_event_id=event.provider_event_id,
                raw_payload=raw_payload,
                idempotency_key=webhook_key,
                received_at=event.received_at,
            ),
        )
        webhook = await connection.fetchrow(
            """
            SELECT status,error_code
            FROM waha_webhook_events WHERE id=$1 FOR UPDATE
            """,
            accepted.id,
        )
        if webhook is None:
            raise WhatsAppInboundConflict("accepted webhook event vanished")
        if not accepted.created:
            terminal = await _terminal_retry_result(connection, accepted.id, webhook)
            if terminal is not None:
                return terminal

        if webhook["status"] not in {"accepted", "failed"}:
            raise WhatsAppInboundConflict(
                f"webhook event is not claimable: {webhook['status']}"
            )
        claimed = await mark_webhook_processing(
            connection,
            event_id=accepted.id,
            worker_id=worker_id,
        )
        if not claimed:
            raise WhatsAppInboundConflict("webhook event could not be claimed")

        contacts = await connection.fetch(
            """
            SELECT id,supplier_id
            FROM supplier_contacts
            WHERE normalized_value=$1 AND active
              AND contact_type IN ('phone','whatsapp')
            ORDER BY id
            LIMIT 2
            """,
            event.normalized_sender_phone,
        )
        if not contacts:
            return await _quarantine(connection, accepted.id, "unknown_sender")
        if len(contacts) != 1:
            return await _quarantine(connection, accepted.id, "ambiguous_sender")

        conversations = await connection.fetch(
            """
            SELECT id,external_chat_id,state,opted_out
            FROM supplier_conversations
            WHERE supplier_contact_id=$1 AND session_name=$2 AND channel='whatsapp'
              AND NOT opted_out
              AND state <> ALL($3::text[])
            ORDER BY updated_at DESC,id
            LIMIT 2
            FOR UPDATE
            """,
            contacts[0]["id"],
            event.session_name,
            list(_TERMINAL_CONVERSATION_STATES),
        )
        if not conversations:
            return await _quarantine(
                connection, accepted.id, "no_active_conversation"
            )
        if len(conversations) != 1:
            return await _quarantine(
                connection, accepted.id, "ambiguous_conversation"
            )

        conversation = conversations[0]
        opt_out = is_opt_out(event.text)
        message = await record_inbound_message(
            connection,
            InboundMessageInput(
                conversation_id=conversation["id"],
                webhook_event_id=accepted.id,
                session_name=event.session_name,
                external_chat_id=conversation["external_chat_id"],
                external_message_id=event.provider_message_id,
                received_at=event.received_at,
                raw_payload=raw_payload,
                text_content=event.text,
                is_opt_out=opt_out,
            ),
        )
        job_id = await enqueue_job(
            connection,
            kind="process_supplier_reply",
            payload={
                "conversation_id": str(conversation["id"]),
                "message_id": str(message.id),
            },
            idempotency_key=stable_key("process-supplier-reply", message.id),
        )
        return WhatsAppInboundResult(
            webhook_event_id=accepted.id,
            disposition=WhatsAppInboundDisposition.PROCESSED,
            conversation_id=conversation["id"],
            message_id=message.id,
            job_id=job_id,
        )


async def _terminal_retry_result(
    connection: asyncpg.Connection,
    webhook_event_id: UUID,
    webhook: Mapping[str, Any],
) -> WhatsAppInboundResult | None:
    if webhook["status"] == "ignored":
        disposition = _QUARANTINE_REASON_TO_DISPOSITION.get(webhook["error_code"])
        if disposition is None:
            raise WhatsAppInboundConflict("webhook was ignored by another policy")
        return WhatsAppInboundResult(
            webhook_event_id=webhook_event_id,
            disposition=disposition,
        )
    if webhook["status"] != "processed":
        return None
    message = await connection.fetchrow(
        """
        SELECT id,conversation_id
        FROM supplier_conversation_messages
        WHERE webhook_event_id=$1 AND direction='inbound'
        """,
        webhook_event_id,
    )
    if message is None:
        raise WhatsAppInboundConflict(
            "processed webhook has no linked inbound message"
        )
    return WhatsAppInboundResult(
        webhook_event_id=webhook_event_id,
        disposition=WhatsAppInboundDisposition.DUPLICATE,
        conversation_id=message["conversation_id"],
        message_id=message["id"],
    )


async def _quarantine(
    connection: asyncpg.Connection,
    webhook_event_id: UUID,
    reason: str,
) -> WhatsAppInboundResult:
    disposition = _QUARANTINE_REASON_TO_DISPOSITION[reason]
    ignored = await mark_webhook_ignored(
        connection,
        event_id=webhook_event_id,
        reason_code=reason,
    )
    if not ignored:
        raise WhatsAppInboundConflict("webhook event could not be quarantined")
    return WhatsAppInboundResult(
        webhook_event_id=webhook_event_id,
        disposition=disposition,
    )


def _require_text(name: str, value: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
