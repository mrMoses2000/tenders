from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.auto_send_policy import (
    assert_auto_send_authorization_active,
    get_case_send_policy,
)
from procurement_bot.conversations import OutboundMessageInput, record_outbound_message
from procurement_bot.errors import ValidationBlocked
from procurement_bot.phone import normalize_phone
from procurement_bot.providers.waha import WahaSendResult
from procurement_bot.queue import ClaimedOutbox
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence
from procurement_bot.supplier_dialogue import (
    DEFAULT_SEND_POLICY,
    SendContext,
    SendPolicy,
    WhatsAppSendDeferred,
    assert_send_allowed_or_defer,
    assert_supplier_safe_text,
)
from procurement_bot.whatsapp_account import assert_account_send_allowed, mark_outcome

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
        conversation_id=_canonical_uuid(value["conversation_id"], field="conversation_id"),
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
    async with pool.acquire() as connection, connection.transaction():
        return await assert_whatsapp_delivery_allowed_on_connection(
            connection, event, now=current, policy=policy
        )


async def assert_whatsapp_delivery_allowed_on_connection(
    connection: asyncpg.Connection,
    event: ClaimedOutbox,
    *,
    now: datetime,
    policy: SendPolicy = DEFAULT_SEND_POLICY,
    include_account_gate: bool = True,
    require_consumed_approval: bool = False,
) -> WhatsAppDeliveryContext:
    """Validate a new send using the caller's transaction and row locks."""

    if now.tzinfo is None:
        raise ValueError("delivery time must be timezone-aware")
    payload = parse_whatsapp_delivery_payload(event.payload)
    # Hold the active authorization episode through the transaction so an
    # owner revocation cannot win between this check and the account reserve.
    await connection.fetchrow(
        """SELECT auto_auth.approval_id
           FROM auto_send_approval_authorizations AS auto_auth
           JOIN case_auto_send_policies AS policy ON policy.id=auto_auth.policy_id
           WHERE auto_auth.approval_id=$1 FOR SHARE OF auto_auth,policy""",
        event.approval_id,
    )
    await assert_auto_send_authorization_active(connection, approval_id=event.approval_id)
    from procurement_bot.autonomous_pipeline import ensure_dialogue_slot

    await ensure_dialogue_slot(connection, payload.conversation_id)
    send_policy = await get_case_send_policy(connection, payload.case_id, policy)
    local = now.astimezone(ZoneInfo(send_policy.timezone))
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_started_at = local_midnight.astimezone(UTC)
    row = await connection.fetchrow(
        """
        SELECT c.case_id,c.session_name,c.external_chat_id,c.state,c.opted_out,
               c.outbound_count,c.followup_count,c.last_outbound_at,c.last_inbound_at,
               contact.normalized_value,contact.active,case_row.status AS case_status,
               (SELECT count(*) FROM supplier_conversation_messages AS message
                WHERE message.conversation_id=c.id AND message.direction='outbound'
                  AND message.received_at >= $2) AS sent_today
        FROM supplier_conversations AS c
        JOIN supplier_contacts AS contact ON contact.id=c.supplier_contact_id
        JOIN source_evidence AS contact_evidence
          ON contact_evidence.id=contact.source_evidence_id
        JOIN procurement_cases AS case_row ON case_row.id=c.case_id
        WHERE c.id=$1 AND c.case_id=$3
          AND __CONTACT_SCOPE__
        FOR SHARE OF c,contact,case_row
        """.replace(
            "__CONTACT_SCOPE__",
            case_owned_supplier_contact_evidence(
                contact_alias="contact",
                evidence_alias="contact_evidence",
                case_param="$3",
            ),
        ),
        payload.conversation_id,
        day_started_at,
        payload.case_id,
    )
    if row is None:
        raise ValueError("WhatsApp conversation does not exist")
    if row["case_id"] != payload.case_id:
        raise ValueError("WhatsApp conversation belongs to another case")
    if row["case_status"] in {"closed", "cancelled", "selected"}:
        raise ValidationBlocked("case is no longer active for WhatsApp sending")
    runtime_paused = await connection.fetchval(
        "SELECT paused FROM case_pipeline_runtime WHERE case_id=$1 FOR SHARE",
        payload.case_id,
    )
    if runtime_paused is True:
        raise WhatsAppSendDeferred(
            available_at=now + timedelta(minutes=30), reason="case_pipeline_paused"
        )
    if not row["active"] or normalize_phone(row["normalized_value"]) != payload.recipient:
        raise ValueError("WhatsApp recipient does not match the active contact")
    first_event = await connection.fetchval(
        """
        SELECT id FROM outbox_events
        WHERE event_type='whatsapp.send_text'
          AND payload->>'conversation_id'=$1
          AND (
            status IN ('pending','retry','sending')
            OR (status='dead' AND provider_request_id IS NOT NULL AND external_id IS NULL)
          )
        ORDER BY created_at,id
        LIMIT 1
        """,
        str(payload.conversation_id),
    )
    if first_event != event.id:
        raise RuntimeError("an earlier WhatsApp action is still pending")
    if payload.logical_action_id.startswith("timed-reminder:") and (
        row["last_outbound_at"] is None
        or (
            row["last_inbound_at"] is not None
            and row["last_inbound_at"] >= row["last_outbound_at"]
        )
    ):
        raise ValidationBlocked("supplier replied before reminder dispatch")
    context = SendContext(
        enabled=row["state"] in _ALLOWED_STATES,
        opted_out=bool(row["opted_out"]),
        messages_sent_today=int(row["sent_today"] or 0),
        messages_sent_in_conversation=int(row["outbound_count"]),
        followups_sent=int(row["followup_count"]),
        last_sent_at=row["last_outbound_at"],
    )
    assert_send_allowed_or_defer(
        context,
        now=now,
        policy=send_policy,
        is_followup=payload.is_followup,
        autonomous_case=send_policy != policy,
    )
    if require_consumed_approval:
        from procurement_bot.approvals import validate_consumed_approval

        await validate_consumed_approval(
            connection,
            approval_id=event.approval_id,
            case_id=payload.case_id,
            action_type="send_whatsapp",
            payload=event.payload,
        )
    if include_account_gate:
        await assert_account_send_allowed(connection, row["session_name"], now=now)
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
        message = await record_outbound_message(
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
        if message is not None:
            from procurement_bot.delivery_receipts import project_message_receipts

            await project_message_receipts(connection, message.id)
        await connection.execute(
            """UPDATE supplier_conversations SET
            reply_due_at=CASE
                WHEN opted_out OR state IN ('opted_out','complete','closed_no_reply')
                     OR last_inbound_at >= $2::timestamptz THEN reply_due_at
                ELSE $2::timestamptz+interval '24 hours'
            END,
            waiting_reason=CASE
                WHEN opted_out OR state IN ('opted_out','complete','closed_no_reply')
                     OR last_inbound_at >= $2::timestamptz THEN waiting_reason
                ELSE 'awaiting_supplier'
            END,
            updated_at=now() WHERE id=$1""",
            context.payload.conversation_id,
            timestamp,
        )
        await connection.execute(
            """
            UPDATE supplier_conversation_item_turns
            SET status='pending_reply',updated_at=now()
            WHERE conversation_id=$1 AND approval_id=$2
              AND status='awaiting_approval' AND draft_text=$3
            """,
            context.payload.conversation_id,
            event.approval_id,
            context.payload.text,
        )
        if context.payload.logical_action_id.startswith("initial-availability:"):
            await connection.execute(
                """
                UPDATE supplier_conversations
                SET state='waiting_availability',updated_at=now()
                WHERE id=$1 AND state='approved' AND NOT opted_out
                """,
                context.payload.conversation_id,
            )
        has_account_reservation = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations "
            "WHERE outbox_event_id=$1)",
            event.id,
        )
        if has_account_reservation:
            await mark_outcome(
                connection,
                event.id,
                state="accepted",
                reason="accepted_delivery_projection_committed",
                accepted_at=timestamp,
                projection_ok=True,
                source="http" if event.provider_request_id is None else "reconciliation",
            )
    return True
