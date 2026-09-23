from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import asyncpg


class ConversationPersistenceConflict(RuntimeError):
    """A transport identity or idempotency key was reused for different evidence."""


class ConversationState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    WAITING_AVAILABILITY = "waiting_availability"
    WAITING_SPEC = "waiting_spec"
    WAITING_PRICE = "waiting_price"
    COMPLETE = "complete"
    ESCALATED = "escalated"
    CLOSED_NO_REPLY = "closed_no_reply"
    OPTED_OUT = "opted_out"


MESSAGE_KINDS = frozenset(
    {"text", "image", "document", "audio", "voice", "video", "location", "contact", "unknown"}
)
WEBHOOK_TERMINAL_STATUSES = frozenset({"processed", "ignored"})
_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,99}")
_ALLOWED_TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.DRAFT: frozenset(
        {ConversationState.APPROVED, ConversationState.ESCALATED}
    ),
    ConversationState.APPROVED: frozenset(
        {ConversationState.WAITING_AVAILABILITY, ConversationState.ESCALATED}
    ),
    ConversationState.WAITING_AVAILABILITY: frozenset(
        {
            ConversationState.WAITING_SPEC,
            ConversationState.COMPLETE,
            ConversationState.ESCALATED,
            ConversationState.CLOSED_NO_REPLY,
        }
    ),
    ConversationState.WAITING_SPEC: frozenset(
        {
            ConversationState.WAITING_PRICE,
            ConversationState.COMPLETE,
            ConversationState.ESCALATED,
            ConversationState.CLOSED_NO_REPLY,
        }
    ),
    ConversationState.WAITING_PRICE: frozenset(
        {
            ConversationState.COMPLETE,
            ConversationState.ESCALATED,
            ConversationState.CLOSED_NO_REPLY,
        }
    ),
    ConversationState.ESCALATED: frozenset(
        {ConversationState.WAITING_AVAILABILITY, ConversationState.COMPLETE}
    ),
    ConversationState.COMPLETE: frozenset(),
    ConversationState.CLOSED_NO_REPLY: frozenset(),
    ConversationState.OPTED_OUT: frozenset(),
}


@dataclass(frozen=True, slots=True)
class LedgerWriteResult:
    id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class ConversationInput:
    case_id: UUID
    supplier_id: UUID
    supplier_contact_id: UUID
    session_name: str
    external_chat_id: str
    idempotency_key: str
    preferred_language: str = "ru"

    def __post_init__(self) -> None:
        _require_transport_identity(self.session_name, self.external_chat_id)
        _require_nonempty("idempotency_key", self.idempotency_key)
        if self.preferred_language not in {"ru", "kk"}:
            raise ValueError("preferred_language must be ru or kk")


@dataclass(frozen=True, slots=True)
class WebhookEventInput:
    session_name: str
    event_name: str
    raw_payload: Mapping[str, Any]
    idempotency_key: str
    received_at: datetime
    external_event_id: str = ""
    max_attempts: int = 5

    def __post_init__(self) -> None:
        _require_nonempty("session_name", self.session_name)
        _require_nonempty("event_name", self.event_name)
        _require_nonempty("idempotency_key", self.idempotency_key)
        _require_aware("received_at", self.received_at)
        if not 1 <= self.max_attempts <= 50:
            raise ValueError("max_attempts must be between 1 and 50")
        canonical_json_object(self.raw_payload)


@dataclass(frozen=True, slots=True)
class InboundMessageInput:
    conversation_id: UUID
    webhook_event_id: UUID
    session_name: str
    external_chat_id: str
    external_message_id: str
    received_at: datetime
    raw_payload: Mapping[str, Any]
    message_kind: str = "text"
    text_content: str = ""
    is_opt_out: bool = False

    def __post_init__(self) -> None:
        _validate_message_fields(self)


@dataclass(frozen=True, slots=True)
class OutboundMessageInput:
    conversation_id: UUID
    outbox_event_id: UUID
    session_name: str
    external_chat_id: str
    external_message_id: str
    received_at: datetime
    raw_payload: Mapping[str, Any]
    message_kind: str = "text"
    text_content: str = ""
    is_followup: bool = False

    def __post_init__(self) -> None:
        _validate_message_fields(self)


def canonical_json_object(payload: Mapping[str, Any]) -> str:
    """Return stable JSON for hashing, rejecting lossy or non-object values."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be an object")
    _validate_json_keys(payload)
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain only finite JSON values") from exc


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_object(payload).encode("utf-8")).hexdigest()


async def create_conversation(
    connection: asyncpg.Connection,
    value: ConversationInput,
) -> LedgerWriteResult:
    """Create one logical conversation and reject natural-key drift."""

    lock_key = f"supplier-conversation:{value.idempotency_key}"
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lock_key
        )
        contact = await connection.fetchrow(
            "SELECT supplier_id,active FROM supplier_contacts WHERE id=$1",
            value.supplier_contact_id,
        )
        if contact is None or not contact["active"]:
            raise ValueError("supplier contact does not exist or is inactive")
        if contact["supplier_id"] != value.supplier_id:
            raise ValueError("supplier contact belongs to a different supplier")

        conversation_id = uuid4()
        row = await connection.fetchrow(
            """
            INSERT INTO supplier_conversations(
                id,case_id,supplier_id,supplier_contact_id,session_name,
                external_chat_id,idempotency_key,preferred_language
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            conversation_id,
            value.case_id,
            value.supplier_id,
            value.supplier_contact_id,
            value.session_name,
            value.external_chat_id,
            value.idempotency_key,
            value.preferred_language,
        )
        if row is not None:
            return LedgerWriteResult(id=row["id"], created=True)
        existing = await connection.fetchrow(
            """
            SELECT * FROM supplier_conversations
            WHERE idempotency_key=$1 OR (
                case_id=$2 AND supplier_id=$3 AND supplier_contact_id=$4
                AND session_name=$5 AND external_chat_id=$6
            )
            """,
            value.idempotency_key,
            value.case_id,
            value.supplier_id,
            value.supplier_contact_id,
            value.session_name,
            value.external_chat_id,
        )
        if existing is None:
            raise RuntimeError("conversation conflict vanished")
        _verify_immutable(
            existing,
            {
                "case_id": value.case_id,
                "supplier_id": value.supplier_id,
                "supplier_contact_id": value.supplier_contact_id,
                "session_name": value.session_name,
                "external_chat_id": value.external_chat_id,
                "idempotency_key": value.idempotency_key,
                "preferred_language": value.preferred_language,
            },
            "conversation",
        )
        return LedgerWriteResult(id=existing["id"], created=False)


async def accept_webhook_event(
    connection: asyncpg.Connection,
    value: WebhookEventInput,
) -> LedgerWriteResult:
    """Persist a webhook before processing; exact retries return the same row."""

    normalized = json.loads(canonical_json_object(value.raw_payload))
    digest = payload_sha256(normalized)
    event_id = uuid4()
    row = await connection.fetchrow(
        """
        INSERT INTO waha_webhook_events(
            id,session_name,event_name,external_event_id,payload_sha256,
            raw_payload,idempotency_key,received_at,max_attempts
        ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        event_id,
        value.session_name,
        value.event_name,
        value.external_event_id,
        digest,
        normalized,
        value.idempotency_key,
        value.received_at,
        value.max_attempts,
    )
    if row is not None:
        return LedgerWriteResult(id=row["id"], created=True)
    existing = await connection.fetchrow(
        """
        SELECT * FROM waha_webhook_events
        WHERE idempotency_key=$1 OR (
            external_event_id<>'' AND session_name=$2 AND event_name=$3
            AND external_event_id=$4
        )
        """,
        value.idempotency_key,
        value.session_name,
        value.event_name,
        value.external_event_id,
    )
    if existing is None:
        raise RuntimeError("webhook conflict vanished")
    _verify_immutable(
        existing,
        {
            "session_name": value.session_name,
            "event_name": value.event_name,
            "external_event_id": value.external_event_id,
            "payload_sha256": digest,
            "raw_payload": normalized,
            "idempotency_key": value.idempotency_key,
            "received_at": value.received_at,
            "max_attempts": value.max_attempts,
        },
        "webhook event",
    )
    return LedgerWriteResult(id=existing["id"], created=False)


async def mark_webhook_processing(
    connection: asyncpg.Connection,
    *,
    event_id: UUID,
    worker_id: str,
) -> bool:
    _require_nonempty("worker_id", worker_id)
    result = await connection.execute(
        """
        UPDATE waha_webhook_events
        SET status='processing',attempts=attempts+1,locked_at=now(),
            locked_by=$2,error_code=''
        WHERE id=$1 AND status IN ('accepted','failed') AND attempts<max_attempts
        """,
        event_id,
        worker_id,
    )
    return result == "UPDATE 1"


async def mark_webhook_failed(
    connection: asyncpg.Connection,
    *,
    event_id: UUID,
    worker_id: str,
    error_code: str,
) -> bool:
    _require_nonempty("worker_id", worker_id)
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be a short machine-readable value")
    result = await connection.execute(
        """
        UPDATE waha_webhook_events
        SET status='failed',locked_at=NULL,locked_by=NULL,error_code=$3
        WHERE id=$1 AND status='processing' AND locked_by=$2
        """,
        event_id,
        worker_id,
        error_code,
    )
    return result == "UPDATE 1"


async def mark_webhook_ignored(
    connection: asyncpg.Connection,
    *,
    event_id: UUID,
    reason_code: str,
) -> bool:
    if not _ERROR_CODE.fullmatch(reason_code):
        raise ValueError("reason_code must be a short machine-readable value")
    result = await connection.execute(
        """
        UPDATE waha_webhook_events
        SET status='ignored',processed_at=now(),locked_at=NULL,locked_by=NULL,
            error_code=$2
        WHERE id=$1 AND status IN ('accepted','processing')
        """,
        event_id,
        reason_code,
    )
    return result == "UPDATE 1"


async def transition_conversation_state(
    connection: asyncpg.Connection,
    *,
    conversation_id: UUID,
    expected_state: ConversationState,
    new_state: ConversationState,
) -> bool:
    """Apply an explicit state-machine edge; opt-out is inbound-message-only."""

    if new_state == ConversationState.OPTED_OUT:
        raise ValueError("opt-out may only be recorded with its inbound message")
    if new_state not in _ALLOWED_TRANSITIONS[expected_state]:
        raise ValueError(f"invalid conversation transition: {expected_state} -> {new_state}")
    async with connection.transaction():
        row = await connection.fetchrow(
            "SELECT state,opted_out FROM supplier_conversations WHERE id=$1 FOR UPDATE",
            conversation_id,
        )
        if row is None:
            raise ValueError("conversation does not exist")
        current = ConversationState(row["state"])
        if row["opted_out"]:
            raise ConversationPersistenceConflict("opted-out conversation is terminal")
        if current == new_state:
            return False
        if current != expected_state:
            raise ConversationPersistenceConflict(
                f"conversation state drift: expected {expected_state}, found {current}"
            )
        result = await connection.execute(
            """
            UPDATE supplier_conversations SET state=$2,updated_at=now()
            WHERE id=$1 AND state=$3 AND NOT opted_out
            """,
            conversation_id,
            new_state.value,
            expected_state.value,
        )
        if result != "UPDATE 1":
            raise ConversationPersistenceConflict("conversation changed during transition")
        return True


async def record_inbound_message(
    connection: asyncpg.Connection,
    value: InboundMessageInput,
    *,
    opt_out_reason: str = "supplier_requested",
) -> LedgerWriteResult:
    """Atomically append inbound evidence, update counters and persist opt-out."""

    if value.is_opt_out and not _ERROR_CODE.fullmatch(opt_out_reason):
        raise ValueError("opt_out_reason must be a short machine-readable value")
    normalized = json.loads(canonical_json_object(value.raw_payload))
    digest = payload_sha256(normalized)
    lock_key = _message_lock_key(value)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lock_key
        )
        conversation = await _locked_conversation(connection, value.conversation_id)
        _verify_transport_matches(conversation, value)
        webhook = await connection.fetchrow(
            "SELECT * FROM waha_webhook_events WHERE id=$1 FOR UPDATE",
            value.webhook_event_id,
        )
        if webhook is None:
            raise ValueError("webhook event does not exist")
        if webhook["session_name"] != value.session_name:
            raise ConversationPersistenceConflict("webhook session does not match message")
        if webhook["status"] == "ignored":
            raise ConversationPersistenceConflict("ignored webhook cannot produce a message")

        result = await _insert_message(
            connection,
            conversation_id=value.conversation_id,
            webhook_event_id=value.webhook_event_id,
            outbox_event_id=None,
            direction="inbound",
            session_name=value.session_name,
            external_chat_id=value.external_chat_id,
            external_message_id=value.external_message_id,
            message_kind=value.message_kind,
            text_content=value.text_content,
            raw_payload=normalized,
            payload_digest=digest,
            is_opt_out=value.is_opt_out,
            is_followup=False,
            received_at=value.received_at,
        )
        if result.created:
            updated = await connection.execute(
                """
                UPDATE supplier_conversations
                SET inbound_count=inbound_count+1,
                    last_inbound_at=CASE WHEN last_inbound_at IS NULL THEN $2
                        ELSE GREATEST(last_inbound_at,$2) END,
                    state=CASE WHEN $3 THEN 'opted_out' ELSE state END,
                    opted_out=opted_out OR $3,
                    opted_out_at=CASE WHEN $3 THEN COALESCE(opted_out_at,$2)
                        ELSE opted_out_at END,
                    opt_out_reason=CASE WHEN $3 THEN $4 ELSE opt_out_reason END,
                    updated_at=now()
                WHERE id=$1
                """,
                value.conversation_id,
                value.received_at,
                value.is_opt_out,
                opt_out_reason,
            )
            if updated != "UPDATE 1":
                raise RuntimeError("conversation vanished while recording inbound message")
        processed = await connection.execute(
            """
            UPDATE waha_webhook_events
            SET status='processed',processed_at=COALESCE(processed_at,now()),
                locked_at=NULL,locked_by=NULL,error_code=''
            WHERE id=$1 AND status<>'ignored'
            """,
            value.webhook_event_id,
        )
        if processed != "UPDATE 1":
            raise ConversationPersistenceConflict("webhook could not be marked processed")
        return result


async def record_outbound_message(
    connection: asyncpg.Connection,
    value: OutboundMessageInput,
) -> LedgerWriteResult:
    """Record an already-sent outbox result; this function never sends anything."""

    normalized = json.loads(canonical_json_object(value.raw_payload))
    digest = payload_sha256(normalized)
    lock_key = _message_lock_key(value)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lock_key
        )
        conversation = await _locked_conversation(connection, value.conversation_id)
        _verify_transport_matches(conversation, value)
        outbox = await connection.fetchrow(
            """
            SELECT event_type,status,external_id,payload
            FROM outbox_events WHERE id=$1 FOR UPDATE
            """,
            value.outbox_event_id,
        )
        if outbox is None:
            raise ValueError("outbox event does not exist")
        if outbox["event_type"] != "whatsapp.send_text" or outbox["status"] != "sent":
            raise ConversationPersistenceConflict("outbox event is not a sent WhatsApp message")
        if outbox["external_id"] != value.external_message_id:
            raise ConversationPersistenceConflict("outbox external id does not match message")
        outbox_payload = outbox["payload"]
        if outbox_payload.get("conversation_id") != str(value.conversation_id):
            raise ConversationPersistenceConflict("outbox conversation id does not match")

        result = await _insert_message(
            connection,
            conversation_id=value.conversation_id,
            webhook_event_id=None,
            outbox_event_id=value.outbox_event_id,
            direction="outbound",
            session_name=value.session_name,
            external_chat_id=value.external_chat_id,
            external_message_id=value.external_message_id,
            message_kind=value.message_kind,
            text_content=value.text_content,
            raw_payload=normalized,
            payload_digest=digest,
            is_opt_out=False,
            is_followup=value.is_followup,
            received_at=value.received_at,
        )
        if result.created:
            updated = await connection.execute(
                """
                UPDATE supplier_conversations
                SET outbound_count=outbound_count+1,
                    followup_count=followup_count+CASE WHEN $3 THEN 1 ELSE 0 END,
                    last_outbound_at=CASE WHEN last_outbound_at IS NULL THEN $2
                        ELSE GREATEST(last_outbound_at,$2) END,
                    updated_at=now()
                WHERE id=$1
                """,
                value.conversation_id,
                value.received_at,
                value.is_followup,
            )
            if updated != "UPDATE 1":
                raise RuntimeError("conversation vanished while recording outbound message")
        return result


async def _insert_message(
    connection: asyncpg.Connection,
    *,
    conversation_id: UUID,
    webhook_event_id: UUID | None,
    outbox_event_id: UUID | None,
    direction: str,
    session_name: str,
    external_chat_id: str,
    external_message_id: str,
    message_kind: str,
    text_content: str,
    raw_payload: dict[str, Any],
    payload_digest: str,
    is_opt_out: bool,
    is_followup: bool,
    received_at: datetime,
) -> LedgerWriteResult:
    message_id = uuid4()
    row = await connection.fetchrow(
        """
        INSERT INTO supplier_conversation_messages(
            id,conversation_id,webhook_event_id,outbox_event_id,direction,
            session_name,external_chat_id,external_message_id,message_kind,
            text_content,raw_payload,payload_sha256,is_opt_out,is_followup,received_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14,$15)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        message_id,
        conversation_id,
        webhook_event_id,
        outbox_event_id,
        direction,
        session_name,
        external_chat_id,
        external_message_id,
        message_kind,
        text_content,
        raw_payload,
        payload_digest,
        is_opt_out,
        is_followup,
        received_at,
    )
    if row is not None:
        return LedgerWriteResult(id=row["id"], created=True)
    existing = await connection.fetchrow(
        """
        SELECT * FROM supplier_conversation_messages
        WHERE webhook_event_id=$1 OR outbox_event_id=$2 OR (
            session_name=$3 AND external_chat_id=$4 AND external_message_id=$5
        )
        """,
        webhook_event_id,
        outbox_event_id,
        session_name,
        external_chat_id,
        external_message_id,
    )
    if existing is None:
        raise RuntimeError("message conflict vanished")
    _verify_immutable(
        existing,
        {
            "conversation_id": conversation_id,
            "webhook_event_id": webhook_event_id,
            "outbox_event_id": outbox_event_id,
            "direction": direction,
            "session_name": session_name,
            "external_chat_id": external_chat_id,
            "external_message_id": external_message_id,
            "message_kind": message_kind,
            "text_content": text_content,
            "raw_payload": raw_payload,
            "payload_sha256": payload_digest,
            "is_opt_out": is_opt_out,
            "is_followup": is_followup,
            "received_at": received_at,
        },
        "supplier message",
    )
    return LedgerWriteResult(id=existing["id"], created=False)


async def _locked_conversation(
    connection: asyncpg.Connection,
    conversation_id: UUID,
) -> Mapping[str, Any]:
    row = await connection.fetchrow(
        "SELECT * FROM supplier_conversations WHERE id=$1 FOR UPDATE", conversation_id
    )
    if row is None:
        raise ValueError("conversation does not exist")
    return row


def _verify_transport_matches(
    conversation: Mapping[str, Any],
    value: InboundMessageInput | OutboundMessageInput,
) -> None:
    if (
        conversation["session_name"] != value.session_name
        or conversation["external_chat_id"] != value.external_chat_id
    ):
        raise ConversationPersistenceConflict("message transport does not match conversation")


def _validate_message_fields(value: InboundMessageInput | OutboundMessageInput) -> None:
    _require_transport_identity(value.session_name, value.external_chat_id)
    _require_nonempty("external_message_id", value.external_message_id)
    _require_aware("received_at", value.received_at)
    if value.message_kind not in MESSAGE_KINDS:
        raise ValueError(f"unsupported message_kind: {value.message_kind}")
    canonical_json_object(value.raw_payload)


def _message_lock_key(value: InboundMessageInput | OutboundMessageInput) -> str:
    raw = f"{value.session_name}\x1f{value.external_chat_id}\x1f{value.external_message_id}"
    return f"supplier-message:{hashlib.sha256(raw.encode()).hexdigest()}"


def _verify_immutable(
    row: Mapping[str, Any],
    expected: Mapping[str, Any],
    label: str,
) -> None:
    for key, expected_value in expected.items():
        actual = row[key]
        if key == "raw_payload":
            actual = dict(actual)
        if actual != expected_value:
            raise ConversationPersistenceConflict(f"{label} drift in {key}")


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _require_transport_identity(session_name: str, external_chat_id: str) -> None:
    _require_nonempty("session_name", session_name)
    _require_nonempty("external_chat_id", external_chat_id)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_json_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError("payload keys must be strings")
            _validate_json_keys(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _validate_json_keys(nested)
