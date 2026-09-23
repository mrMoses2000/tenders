from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.approvals import (
    ApprovalRequired,
    canonical_payload_json,
    payload_sha256,
    require_and_consume_approval,
)
from procurement_bot.queue import stable_key
from procurement_bot.supplier_dialogue import (
    SendContext,
    WhatsAppSendCommand,
    enqueue_approved_whatsapp_send,
)


def test_canonical_payload_hash_is_order_independent_and_rejects_lossy_json() -> None:
    left = {"recipient": "+77001234567", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "recipient": "+77001234567"}

    assert canonical_payload_json(left) == canonical_payload_json(right)
    assert payload_sha256(left) == payload_sha256(right)
    assert "+77001234567" not in payload_sha256(left)

    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_payload_json({1: "not allowed"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="finite JSON"):
        canonical_payload_json({"price": float("nan")})
    with pytest.raises(ValueError, match="finite JSON"):
        canonical_payload_json({"price": float("inf")})


class ApprovalConnection:
    def __init__(self, *, case_id: UUID, action_type: str, payload: dict[str, Any]) -> None:
        self.case_id = case_id
        self.action_type = action_type
        self.digest = payload_sha256(payload)
        self.approval_id = uuid4()
        self.status = "approved"

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        assert "UPDATE action_approvals" in query
        case_id, action_type, digest = args
        if (
            self.status != "approved"
            or case_id != self.case_id
            or action_type != self.action_type
            or digest != self.digest
        ):
            return None
        self.status = "consumed"
        return {
            "id": self.approval_id,
            "case_id": self.case_id,
            "action_type": self.action_type,
            "payload_sha256": self.digest,
            "consumed_at": datetime(2026, 9, 22, 10, tzinfo=UTC),
        }


@pytest.mark.asyncio
async def test_exact_approval_is_single_use_and_does_not_authorize_mutated_payload() -> None:
    case_id = uuid4()
    exact = {"recipient": "77001234567@c.us", "text": "Есть в наличии?"}
    connection = ApprovalConnection(
        case_id=case_id,
        action_type="send_whatsapp",
        payload=exact,
    )

    with pytest.raises(ApprovalRequired):
        await require_and_consume_approval(
            connection,  # type: ignore[arg-type]
            case_id=case_id,
            action_type="send_whatsapp",
            payload={**exact, "text": "Сообщите вашу лучшую цену"},
        )
    assert connection.status == "approved"

    consumed = await require_and_consume_approval(
        connection,  # type: ignore[arg-type]
        case_id=case_id,
        action_type="send_whatsapp",
        payload=exact,
    )
    assert consumed.id == connection.approval_id
    assert connection.status == "consumed"

    with pytest.raises(ApprovalRequired):
        await require_and_consume_approval(
            connection,  # type: ignore[arg-type]
            case_id=case_id,
            action_type="send_whatsapp",
            payload=exact,
        )


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


class EnqueueConnection(ApprovalConnection):
    def __init__(self, *, command: WhatsAppSendCommand) -> None:
        super().__init__(
            case_id=command.case_id,
            action_type="send_whatsapp",
            payload=command.approval_payload(),
        )
        self.idempotency_key = stable_key(
            "whatsapp-send", command.conversation_id, command.logical_action_id
        )
        self.event_id: UUID | None = None
        self.approval_consumptions = 0

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def execute(self, query: str, *args: object) -> str:
        assert "pg_advisory_xact_lock" in query
        assert args == (self.idempotency_key,)
        return "SELECT 1"

    async def fetchval(self, query: str, *args: object) -> UUID | None:
        if "SELECT id FROM outbox_events" in query:
            assert args == (self.idempotency_key,)
            return self.event_id
        if "SELECT status FROM outbox_events" in query:
            assert args == (self.event_id,)
            return "pending"  # type: ignore[return-value]
        assert "INSERT INTO outbox_events" in query
        _new_id, event_type, payload, key, max_attempts, approval_id = args
        assert event_type == "whatsapp.send_text"
        assert payload["channel"] == "whatsapp"  # type: ignore[index]
        assert key == self.idempotency_key
        assert max_attempts == 3
        assert approval_id == self.approval_id
        self.event_id = _new_id  # type: ignore[assignment]
        return self.event_id

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        result = await super().fetchrow(query, *args)
        if result is not None:
            self.approval_consumptions += 1
        return result


class EnqueuePool:
    def __init__(self, connection: EnqueueConnection) -> None:
        self.connection = connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.connection)


@pytest.mark.asyncio
async def test_whatsapp_enqueue_retry_returns_existing_event_without_second_approval() -> None:
    command = WhatsAppSendCommand(
        case_id=uuid4(),
        recipient="+7 700 123-45-67",
        text="Здравствуйте! Есть ли товар в наличии?",
        conversation_id=uuid4(),
        logical_action_id="availability-v1",
    )
    connection = EnqueueConnection(command=command)
    pool = EnqueuePool(connection)
    context = SendContext(enabled=True)
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)

    first = await enqueue_approved_whatsapp_send(
        pool,  # type: ignore[arg-type]
        command=command,
        context=context,
        now=now,
    )
    second = await enqueue_approved_whatsapp_send(
        pool,  # type: ignore[arg-type]
        command=command,
        context=context,
        now=now,
    )

    assert first == second == connection.event_id
    assert connection.approval_consumptions == 1
