from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.conversations import (
    ConversationPersistenceConflict,
    ConversationState,
    InboundMessageInput,
    WebhookEventInput,
    accept_webhook_event,
    canonical_json_object,
    payload_sha256,
    record_inbound_message,
    transition_conversation_state,
)


class _AsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _WebhookConnection:
    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "INSERT INTO waha_webhook_events" in sql:
            if self.row is not None:
                return None
            (
                event_id,
                session_name,
                event_name,
                external_event_id,
                digest,
                raw_payload,
                idempotency_key,
                received_at,
                max_attempts,
            ) = args
            self.row = {
                "id": event_id,
                "session_name": session_name,
                "event_name": event_name,
                "external_event_id": external_event_id,
                "payload_sha256": digest,
                "raw_payload": raw_payload,
                "idempotency_key": idempotency_key,
                "received_at": received_at,
                "max_attempts": max_attempts,
            }
            return {"id": event_id}
        assert "SELECT * FROM waha_webhook_events" in sql
        return self.row


class _InboundConnection:
    def __init__(self, value: InboundMessageInput) -> None:
        self.value = value
        self.message: dict[str, Any] | None = None
        self.inbound_count = 0
        self.conversation_state = "waiting_availability"
        self.opted_out = False
        self.webhook_status = "processing"

    def transaction(self) -> _AsyncContext:
        return _AsyncContext()

    async def execute(self, sql: str, *args: object) -> str:
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "UPDATE supplier_conversations" in sql:
            self.inbound_count += 1
            if args[2]:
                self.conversation_state = "opted_out"
                self.opted_out = True
            return "UPDATE 1"
        if "UPDATE waha_webhook_events" in sql:
            self.webhook_status = "processed"
            return "UPDATE 1"
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "FROM supplier_conversations" in sql:
            return {
                "id": self.value.conversation_id,
                "session_name": self.value.session_name,
                "external_chat_id": self.value.external_chat_id,
                "state": self.conversation_state,
                "opted_out": self.opted_out,
            }
        if "FROM waha_webhook_events" in sql:
            return {
                "id": self.value.webhook_event_id,
                "session_name": self.value.session_name,
                "status": self.webhook_status,
            }
        if "INSERT INTO supplier_conversation_messages" in sql:
            if self.message is not None:
                return None
            self.message = _message_row(args)
            return {"id": self.message["id"]}
        if "SELECT * FROM supplier_conversation_messages" in sql:
            return self.message
        raise AssertionError(sql)


def _message_row(args: tuple[object, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "conversation_id",
        "webhook_event_id",
        "outbox_event_id",
        "direction",
        "session_name",
        "external_chat_id",
        "external_message_id",
        "message_kind",
        "text_content",
        "raw_payload",
        "payload_sha256",
        "is_opt_out",
        "is_followup",
        "received_at",
    )
    return dict(zip(keys, args, strict=True))


def test_canonical_payload_is_stable_and_rejects_lossy_json() -> None:
    left = {"message": {"id": "abc", "body": "стоп"}, "event": "message"}
    right = {"event": "message", "message": {"body": "стоп", "id": "abc"}}

    assert canonical_json_object(left) == canonical_json_object(right)
    assert payload_sha256(left) == payload_sha256(right)
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_json_object({"nested": {1: "lossy"}})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="finite JSON"):
        canonical_json_object({"value": float("nan")})


@pytest.mark.asyncio
async def test_webhook_retry_is_idempotent_but_payload_drift_is_rejected() -> None:
    connection = _WebhookConnection()
    timestamp = datetime(2026, 9, 22, 12, tzinfo=UTC)
    value = WebhookEventInput(
        session_name="default",
        event_name="message",
        external_event_id="evt-42",
        raw_payload={"id": "evt-42", "body": "Есть"},
        idempotency_key="waha:evt-42",
        received_at=timestamp,
    )

    first = await accept_webhook_event(connection, value)  # type: ignore[arg-type]
    retry = await accept_webhook_event(connection, value)  # type: ignore[arg-type]
    assert first.id == retry.id
    assert first.created and not retry.created

    changed = WebhookEventInput(
        session_name="default",
        event_name="message",
        external_event_id="evt-42",
        raw_payload={"id": "evt-42", "body": "Изменено"},
        idempotency_key="waha:evt-42",
        received_at=timestamp,
    )
    with pytest.raises(ConversationPersistenceConflict, match="payload_sha256"):
        await accept_webhook_event(connection, changed)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_inbound_opt_out_is_recorded_once_with_message_and_projection() -> None:
    value = InboundMessageInput(
        conversation_id=uuid4(),
        webhook_event_id=uuid4(),
        session_name="default",
        external_chat_id="77001234567@c.us",
        external_message_id="msg-42",
        text_content="Не пишите мне",
        raw_payload={"id": "msg-42", "body": "Не пишите мне"},
        received_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        is_opt_out=True,
    )
    connection = _InboundConnection(value)

    first = await record_inbound_message(connection, value)  # type: ignore[arg-type]
    retry = await record_inbound_message(connection, value)  # type: ignore[arg-type]

    assert first.id == retry.id
    assert first.created and not retry.created
    assert connection.inbound_count == 1
    assert connection.conversation_state == "opted_out"
    assert connection.opted_out
    assert connection.webhook_status == "processed"


@pytest.mark.asyncio
async def test_exact_message_id_cannot_hide_changed_text() -> None:
    original = InboundMessageInput(
        conversation_id=uuid4(),
        webhook_event_id=uuid4(),
        session_name="default",
        external_chat_id="77001234567@c.us",
        external_message_id="msg-7",
        text_content="Есть в наличии",
        raw_payload={"id": "msg-7", "body": "Есть в наличии"},
        received_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )
    connection = _InboundConnection(original)
    await record_inbound_message(connection, original)  # type: ignore[arg-type]
    changed = InboundMessageInput(
        conversation_id=original.conversation_id,
        webhook_event_id=original.webhook_event_id,
        session_name=original.session_name,
        external_chat_id=original.external_chat_id,
        external_message_id=original.external_message_id,
        text_content="Нет в наличии",
        raw_payload={"id": "msg-7", "body": "Есть в наличии"},
        received_at=original.received_at,
    )

    with pytest.raises(ConversationPersistenceConflict, match="text_content"):
        await record_inbound_message(connection, changed)  # type: ignore[arg-type]
    assert connection.inbound_count == 1


@pytest.mark.asyncio
async def test_invalid_state_transition_is_rejected_before_database_access() -> None:
    with pytest.raises(ValueError, match="invalid conversation transition"):
        await transition_conversation_state(
            object(),  # type: ignore[arg-type]
            conversation_id=UUID(int=1),
            expected_state=ConversationState.DRAFT,
            new_state=ConversationState.COMPLETE,
        )
