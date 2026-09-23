from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.approvals import payload_sha256
from procurement_bot.queue import stable_key
from procurement_bot.whatsapp_approval_workflow import (
    WhatsAppSendDenied,
    WhatsAppSendStateConflict,
    start_approved_whatsapp_send,
)


class FakeConnection:
    def __init__(self) -> None:
        self.approval_id = uuid4()
        self.case_id = uuid4()
        self.conversation_id = uuid4()
        self.approved_by = uuid4()
        self.payload: dict[str, Any] = {
            "case_id": str(self.case_id),
            "channel": "whatsapp",
            "recipient": "+7 700 123-45-67",
            "text": "Здравствуйте! Есть ли товар в наличии?",
            "conversation_id": str(self.conversation_id),
            "logical_action_id": "availability-v1",
            "is_followup": False,
        }
        self.approval: dict[str, Any] = {
            "id": self.approval_id,
            "case_id": self.case_id,
            "action_type": "send_whatsapp",
            "payload": self.payload,
            "payload_sha256": payload_sha256(self.payload),
            "status": "approved",
            "approved_by": self.approved_by,
            "expires_at": None,
            "consumed_at": None,
            "is_live": True,
            "consumed_when_live": False,
        }
        self.conversation: dict[str, Any] | None = {
            "case_id": self.case_id,
            "state": "approved",
            "opted_out": False,
            "outbound_count": 0,
            "followup_count": 0,
            "last_outbound_at": None,
            "contact_type": "whatsapp",
            "normalized_value": "+77001234567",
            "active": True,
            "sent_today": 0,
        }
        self.outbox: dict[str, Any] | None = None
        self.approval_updates = 0
        self.outbox_inserts = 0

    def transaction(self):
        @asynccontextmanager
        async def manager():
            yield

        return manager()

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        if "FROM action_approvals" in query:
            assert args == (self.approval_id,)
            return self.approval
        if "FROM supplier_conversations" in query:
            assert args[0] == self.conversation_id
            assert isinstance(args[1], datetime)
            return self.conversation
        if "FROM outbox_events WHERE idempotency_key" in query:
            key = args[0]
            return self.outbox if self.outbox and self.outbox["idempotency_key"] == key else None
        if "WHERE approval_id=$1 OR idempotency_key=$2" in query:
            if self.outbox is None:
                return None
            if (
                self.outbox["approval_id"] == args[0]
                or self.outbox["idempotency_key"] == args[1]
            ):
                return self.outbox
            return None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query: str, *args: object) -> str:
        if "pg_advisory_xact_lock" in query:
            assert args == (
                stable_key(
                    "whatsapp-send",
                    self.conversation_id,
                    self.payload["logical_action_id"],
                ),
            )
            return "SELECT 1"
        if "UPDATE action_approvals" in query:
            assert args == (self.approval_id,)
            if self.approval["status"] != "approved":
                return "UPDATE 0"
            self.approval["status"] = "consumed"
            self.approval["consumed_at"] = datetime(2026, 9, 23, 7, tzinfo=UTC)
            self.approval["consumed_when_live"] = True
            self.approval_updates += 1
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> UUID | None:
        assert "INSERT INTO outbox_events" in query
        event_id, event_type, payload, key, max_attempts, approval_id = args
        assert event_type == "whatsapp.send_text"
        assert max_attempts == 3
        assert approval_id == self.approval_id
        self.outbox = {
            "id": event_id,
            "event_type": event_type,
            "payload": payload,
            "idempotency_key": key,
            "approval_id": approval_id,
        }
        self.outbox_inserts += 1
        return event_id  # type: ignore[return-value]


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self):
        @asynccontextmanager
        async def manager():
            yield self.connection

        return manager()


@pytest.fixture(autouse=True)
def stable_daytime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "procurement_bot.whatsapp_approval_workflow._now_utc",
        lambda: datetime(2026, 9, 23, 7, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_approved_send_is_consumed_and_enqueued_atomically() -> None:
    connection = FakeConnection()

    event_id = await start_approved_whatsapp_send(connection, connection.approval_id)  # type: ignore[arg-type]

    assert event_id == connection.outbox["id"]  # type: ignore[index]
    assert connection.outbox["payload"] == connection.payload  # type: ignore[index]
    assert connection.outbox["approval_id"] == connection.approval_id  # type: ignore[index]
    assert connection.approval["status"] == "consumed"
    assert connection.approval_updates == 1
    assert connection.outbox_inserts == 1


@pytest.mark.asyncio
async def test_consumed_retry_through_pool_returns_only_exact_existing_outbox() -> None:
    connection = FakeConnection()
    pool = FakePool(connection)
    first = await start_approved_whatsapp_send(pool, connection.approval_id)  # type: ignore[arg-type]

    second = await start_approved_whatsapp_send(pool, connection.approval_id)  # type: ignore[arg-type]

    assert second == first
    assert connection.approval_updates == 1
    assert connection.outbox_inserts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda db: db.payload.pop("is_followup"), "must contain exactly"),
        (lambda db: db.payload.update(is_followup=0), "must be a boolean"),
        (
            lambda db: db.payload.update(conversation_id=str(db.conversation_id).upper()),
            "canonical UUID",
        ),
        (lambda db: db.approval.update(payload_sha256="0" * 64), "hash is invalid"),
        (lambda db: db.approval.update(action_type="start_research"), "different action"),
        (lambda db: db.approval.update(approved_by=None), "no approving user"),
    ],
)
async def test_invalid_or_mutated_approval_fails_closed(mutate: Any, message: str) -> None:
    connection = FakeConnection()
    mutate(connection)
    if message != "hash is invalid":
        connection.approval["payload_sha256"] = payload_sha256(connection.payload)

    with pytest.raises(WhatsAppSendDenied, match=message):
        await start_approved_whatsapp_send(connection, connection.approval_id)  # type: ignore[arg-type]

    assert connection.outbox is None
    assert connection.approval["status"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda db: db.conversation.update(state="complete"), "not in a sendable state"),
        (lambda db: db.conversation.update(opted_out=True), "opted out"),
        (
            lambda db: db.conversation.update(normalized_value="+77009999999"),
            "does not match",
        ),
        (lambda db: db.conversation.update(active=False), "not active"),
        (lambda db: db.conversation.update(outbound_count=4), "message limit"),
    ],
)
async def test_live_conversation_and_contact_state_are_enforced(
    mutate: Any, message: str
) -> None:
    connection = FakeConnection()
    assert connection.conversation is not None
    mutate(connection)

    with pytest.raises(WhatsAppSendDenied, match=message):
        await start_approved_whatsapp_send(connection, connection.approval_id)  # type: ignore[arg-type]

    assert connection.outbox is None
    assert connection.approval["status"] == "approved"


@pytest.mark.asyncio
async def test_consumed_retry_with_mismatched_outbox_fails_closed() -> None:
    connection = FakeConnection()
    await start_approved_whatsapp_send(connection, connection.approval_id)  # type: ignore[arg-type]
    assert connection.outbox is not None
    connection.outbox["payload"] = connection.payload | {"text": "Другое сообщение"}

    with pytest.raises(WhatsAppSendStateConflict, match="mismatched"):
        await start_approved_whatsapp_send(connection, connection.approval_id)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_consumed_approval_without_outbox_fails_closed() -> None:
    connection = FakeConnection()
    connection.approval.update(
        status="consumed",
        consumed_at=datetime(2026, 9, 23, 7, tzinfo=UTC),
        consumed_when_live=True,
    )

    with pytest.raises(WhatsAppSendStateConflict, match="no matching outbox"):
        await start_approved_whatsapp_send(connection, connection.approval_id)  # type: ignore[arg-type]
