from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.whatsapp_inbound as inbound_module
from procurement_bot.conversations import LedgerWriteResult
from procurement_bot.whatsapp_inbound import (
    AuthenticatedWhatsAppInbound,
    WhatsAppInboundConflict,
    WhatsAppInboundDisposition,
    accept_whatsapp_inbound,
)


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        webhook_status: str = "accepted",
        webhook_error: str = "",
        contacts: list[dict[str, Any]] | None = None,
        conversations: list[dict[str, Any]] | None = None,
        existing_message: dict[str, Any] | None = None,
    ) -> None:
        self.webhook_status = webhook_status
        self.webhook_error = webhook_error
        self.contacts = contacts if contacts is not None else []
        self.conversations = conversations if conversations is not None else []
        self.existing_message = existing_message
        self.transaction_count = 0

    def transaction(self) -> _Transaction:
        self.transaction_count += 1
        return _Transaction()

    async def fetchrow(self, sql: str, *_args: object) -> dict[str, Any] | None:
        if "FROM waha_webhook_events" in sql:
            return {"status": self.webhook_status, "error_code": self.webhook_error}
        if "FROM supplier_conversation_messages" in sql:
            return self.existing_message
        raise AssertionError(sql)

    async def fetch(self, sql: str, *_args: object) -> list[dict[str, Any]]:
        if "FROM supplier_contacts" in sql:
            return self.contacts
        if "FROM supplier_conversations" in sql:
            return self.conversations
        raise AssertionError(sql)


def _event(text: str = "Есть в наличии") -> AuthenticatedWhatsAppInbound:
    return AuthenticatedWhatsAppInbound(
        provider_event_id="event-42",
        session_name="procurement",
        sender_phone="8 (700) 123-45-67",
        provider_message_id="message-42",
        received_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        text=text,
    )


def _install_webhook_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_id: UUID,
    created: bool = True,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def accept(_connection: object, value: object) -> LedgerWriteResult:
        captured["webhook"] = value
        return LedgerWriteResult(id=event_id, created=created)

    async def claim(_connection: object, *, event_id: UUID, worker_id: str) -> bool:
        captured["claim"] = (event_id, worker_id)
        return True

    monkeypatch.setattr(inbound_module, "accept_webhook_event", accept)
    monkeypatch.setattr(inbound_module, "mark_webhook_processing", claim)
    return captured


@pytest.mark.asyncio
async def test_known_sender_is_recorded_and_only_opaque_ids_are_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    contact_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()
    job_id = uuid4()
    connection = _Connection(
        contacts=[{"id": contact_id, "supplier_id": uuid4()}],
        conversations=[
            {
                "id": conversation_id,
                "external_chat_id": "77001234567@c.us",
                "state": "waiting_availability",
                "opted_out": False,
            }
        ],
    )
    captured = _install_webhook_stubs(monkeypatch, event_id=event_id)

    async def record(_connection: object, value: object) -> LedgerWriteResult:
        captured["message"] = value
        return LedgerWriteResult(id=message_id, created=True)

    async def enqueue(_connection: object, **kwargs: object) -> UUID:
        captured["job"] = kwargs
        return job_id

    monkeypatch.setattr(inbound_module, "record_inbound_message", record)
    monkeypatch.setattr(inbound_module, "enqueue_job", enqueue)

    result = await accept_whatsapp_inbound(connection, _event())  # type: ignore[arg-type]

    assert result == inbound_module.WhatsAppInboundResult(
        webhook_event_id=event_id,
        disposition=WhatsAppInboundDisposition.PROCESSED,
        conversation_id=conversation_id,
        message_id=message_id,
        job_id=job_id,
    )
    assert captured["webhook"].raw_payload["sender_phone"] == "+77001234567"
    assert captured["message"].text_content == "Есть в наличии"
    assert captured["message"].external_chat_id == "77001234567@c.us"
    assert captured["job"]["kind"] == "process_supplier_reply"
    assert captured["job"]["payload"] == {
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
    }
    assert "Есть" not in repr(captured["job"])
    assert "+7700" not in repr(captured["job"])
    assert connection.transaction_count == 1


@pytest.mark.asyncio
async def test_opt_out_is_applied_before_internal_job_is_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()
    connection = _Connection(
        contacts=[{"id": uuid4(), "supplier_id": uuid4()}],
        conversations=[
            {
                "id": conversation_id,
                "external_chat_id": "77001234567@c.us",
                "state": "waiting_spec",
                "opted_out": False,
            }
        ],
    )
    _install_webhook_stubs(monkeypatch, event_id=event_id)
    order: list[str] = []

    async def record(_connection: object, value: object) -> LedgerWriteResult:
        assert value.is_opt_out is True
        order.append("opt_out_persisted")
        return LedgerWriteResult(id=message_id, created=True)

    async def enqueue(_connection: object, **_kwargs: object) -> UUID:
        order.append("job_enqueued")
        return uuid4()

    monkeypatch.setattr(inbound_module, "record_inbound_message", record)
    monkeypatch.setattr(inbound_module, "enqueue_job", enqueue)

    await accept_whatsapp_inbound(connection, _event("Не пишите мне"))  # type: ignore[arg-type]

    assert order == ["opt_out_persisted", "job_enqueued"]


@pytest.mark.asyncio
async def test_unknown_sender_is_quarantined_without_message_or_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    connection = _Connection(contacts=[])
    _install_webhook_stubs(monkeypatch, event_id=event_id)
    captured: dict[str, Any] = {}

    async def ignore(_connection: object, *, event_id: UUID, reason_code: str) -> bool:
        captured.update(event_id=event_id, reason=reason_code)
        return True

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("quarantined events must not be linked or scheduled")

    monkeypatch.setattr(inbound_module, "mark_webhook_ignored", ignore)
    monkeypatch.setattr(inbound_module, "record_inbound_message", forbidden)
    monkeypatch.setattr(inbound_module, "enqueue_job", forbidden)

    result = await accept_whatsapp_inbound(connection, _event())  # type: ignore[arg-type]

    assert result.disposition == WhatsAppInboundDisposition.QUARANTINED_UNKNOWN_SENDER
    assert result.conversation_id is None
    assert result.message_id is None
    assert captured == {"event_id": event_id, "reason": "unknown_sender"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversations", "expected", "reason"),
    [
        (
            [],
            WhatsAppInboundDisposition.QUARANTINED_NO_ACTIVE_CONVERSATION,
            "no_active_conversation",
        ),
        (
            [
                {"id": uuid4(), "external_chat_id": "a", "state": "draft"},
                {"id": uuid4(), "external_chat_id": "b", "state": "approved"},
            ],
            WhatsAppInboundDisposition.QUARANTINED_AMBIGUOUS_CONVERSATION,
            "ambiguous_conversation",
        ),
    ],
)
async def test_conversation_resolution_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    conversations: list[dict[str, Any]],
    expected: WhatsAppInboundDisposition,
    reason: str,
) -> None:
    event_id = uuid4()
    connection = _Connection(
        contacts=[{"id": uuid4(), "supplier_id": uuid4()}],
        conversations=conversations,
    )
    _install_webhook_stubs(monkeypatch, event_id=event_id)
    captured: dict[str, str] = {}

    async def ignore(_connection: object, *, event_id: UUID, reason_code: str) -> bool:
        captured["reason"] = reason_code
        return True

    monkeypatch.setattr(inbound_module, "mark_webhook_ignored", ignore)

    result = await accept_whatsapp_inbound(connection, _event())  # type: ignore[arg-type]

    assert result.disposition == expected
    assert captured["reason"] == reason


@pytest.mark.asyncio
async def test_processed_retry_returns_existing_link_without_new_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()
    connection = _Connection(
        webhook_status="processed",
        existing_message={"id": message_id, "conversation_id": conversation_id},
    )
    _install_webhook_stubs(monkeypatch, event_id=event_id, created=False)

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("terminal retry must not claim, append, or enqueue")

    monkeypatch.setattr(inbound_module, "mark_webhook_processing", forbidden)
    monkeypatch.setattr(inbound_module, "record_inbound_message", forbidden)
    monkeypatch.setattr(inbound_module, "enqueue_job", forbidden)

    result = await accept_whatsapp_inbound(connection, _event())  # type: ignore[arg-type]

    assert result.disposition == WhatsAppInboundDisposition.DUPLICATE
    assert result.conversation_id == conversation_id
    assert result.message_id == message_id
    assert result.job_id is None


@pytest.mark.asyncio
async def test_in_progress_retry_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    event_id = uuid4()
    connection = _Connection(webhook_status="processing")
    _install_webhook_stubs(monkeypatch, event_id=event_id, created=False)

    with pytest.raises(WhatsAppInboundConflict, match="not claimable"):
        await accept_whatsapp_inbound(connection, _event())  # type: ignore[arg-type]


def test_contract_rejects_bad_phone_empty_text_and_naive_timestamp() -> None:
    base: Mapping[str, Any] = {
        "provider_event_id": "event",
        "session_name": "session",
        "sender_phone": "+77001234567",
        "provider_message_id": "message",
        "received_at": datetime(2026, 9, 22, 12, tzinfo=UTC),
        "text": "Есть",
    }
    for override, match in (
        ({"sender_phone": "not-a-phone"}, "international phone"),
        ({"text": "   "}, "text must be non-empty"),
        ({"received_at": datetime(2026, 9, 22, 12)}, "timezone-aware"),
    ):
        with pytest.raises(ValueError, match=match):
            AuthenticatedWhatsAppInbound(**{**base, **override})
