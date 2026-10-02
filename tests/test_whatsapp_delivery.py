from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from procurement_bot.errors import ValidationBlocked
from procurement_bot.providers.waha import WahaSendResult
from procurement_bot.queue import ClaimedOutbox
from procurement_bot.supplier_dialogue import WhatsAppSendDeferred
from procurement_bot.whatsapp_delivery import (
    assert_whatsapp_delivery_allowed,
    complete_whatsapp_delivery,
    parse_whatsapp_delivery_payload,
)


def _payload() -> dict[str, object]:
    return {
        "case_id": str(uuid4()),
        "channel": "whatsapp",
        "recipient": "+7 700 123-45-67",
        "text": "Здравствуйте! Есть ли товар в наличии?",
        "conversation_id": str(uuid4()),
        "logical_action_id": "availability-v1",
        "is_followup": False,
    }


def _event(payload: dict[str, object]) -> ClaimedOutbox:
    return ClaimedOutbox(
        id=uuid4(),
        event_type="whatsapp.send_text",
        payload=payload,
        attempts=1,
        max_attempts=3,
        idempotency_key="wa:test",
        locked_by="worker",
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        approval_id=uuid4(),
        provider_request_id="message-id",
    )


def test_delivery_payload_is_exact_and_canonical() -> None:
    parsed = parse_whatsapp_delivery_payload(_payload())
    assert parsed.recipient == "+77001234567"

    extra = _payload() | {"parse_mode": "HTML"}
    with pytest.raises(ValueError, match="unknown shape"):
        parse_whatsapp_delivery_payload(extra)

    unsafe = _payload() | {"text": "Наш бюджет 100 000 тенге"}
    with pytest.raises(ValidationBlocked):
        parse_whatsapp_delivery_payload(unsafe)


class _Connection:
    def __init__(self, row: dict[str, object], event_id: object) -> None:
        self.row = row
        self.event_id = event_id

    def transaction(self):
        @asynccontextmanager
        async def manager():
            yield

        return manager()

    async def fetchrow(self, *_args: object) -> dict[str, object]:
        return self.row

    async def fetchval(self, query: str, *_args: object) -> object:
        if "SELECT CASE" in query and "auto_send_approval_authorizations" in query:
            # These tests exercise legacy/manual grants, not auto-policy revocation.
            return True
        if "SELECT EXISTS" in query and "case_auto_send_policies" in query:
            return False
        return self.event_id


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self):
        @asynccontextmanager
        async def manager():
            yield self.connection

        return manager()


@pytest.mark.asyncio
async def test_delivery_rechecks_opt_out_before_provider_call() -> None:
    payload = _payload()
    event = _event(payload)
    row = {
        "case_id": event.payload["case_id"],
        "session_name": "default",
        "external_chat_id": "77001234567@c.us",
        "state": "opted_out",
        "opted_out": True,
        "outbound_count": 0,
        "followup_count": 0,
        "last_outbound_at": None,
        "normalized_value": "+77001234567",
        "active": True,
        "sent_today": 0,
    }
    row["case_id"] = parse_whatsapp_delivery_payload(payload).case_id

    with pytest.raises(ValidationBlocked, match="opted out"):
        await assert_whatsapp_delivery_allowed(
            _Pool(_Connection(row, event.id)),  # type: ignore[arg-type]
            event,
            now=datetime(2026, 9, 22, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_delivery_enforces_conversation_order() -> None:
    payload = _payload()
    event = _event(payload)
    parsed = parse_whatsapp_delivery_payload(payload)
    row = {
        "case_id": parsed.case_id,
        "session_name": "default",
        "external_chat_id": "77001234567@c.us",
        "state": "waiting_availability",
        "opted_out": False,
        "outbound_count": 0,
        "followup_count": 0,
        "last_outbound_at": None,
        "normalized_value": "+77001234567",
        "active": True,
        "sent_today": 0,
    }

    with pytest.raises(RuntimeError, match="earlier"):
        await assert_whatsapp_delivery_allowed(
            _Pool(_Connection(row, uuid4())),  # type: ignore[arg-type]
            event,
            now=datetime(2026, 9, 22, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_delivery_accepts_matching_active_conversation() -> None:
    payload = _payload()
    event = _event(payload)
    parsed = parse_whatsapp_delivery_payload(payload)
    row = {
        "case_id": parsed.case_id,
        "session_name": "procurement",
        "external_chat_id": "77001234567@c.us",
        "state": "waiting_availability",
        "opted_out": False,
        "outbound_count": 0,
        "followup_count": 0,
        "last_outbound_at": None,
        "normalized_value": "+77001234567",
        "active": True,
        "sent_today": 0,
    }

    result = await assert_whatsapp_delivery_allowed(
        _Pool(_Connection(row, event.id)),  # type: ignore[arg-type]
        event,
        now=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )

    assert result.payload == parsed
    assert result.session_name == "procurement"
    assert result.external_chat_id == "77001234567@c.us"


@pytest.mark.asyncio
async def test_completion_rolls_sent_state_and_ledger_into_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    event = _event(payload)
    parsed = parse_whatsapp_delivery_payload(payload)
    context = type(
        "Context",
        (),
        {
            "payload": parsed,
            "session_name": "procurement",
            "external_chat_id": "77001234567@c.us",
        },
    )()
    recorded: list[object] = []

    class CompletionConnection(_Connection):
        def __init__(self, row: dict[str, object], event_id: object) -> None:
            super().__init__(row, event_id)
            self.executions: list[tuple[str, tuple[object, ...]]] = []

        async def execute(self, *_args: object) -> str:
            self.executions.append((_args[0], _args[1:]))
            return "UPDATE 1"

    async def record(_connection: object, value: object) -> None:
        recorded.append(value)

    monkeypatch.setattr("procurement_bot.whatsapp_delivery.record_outbound_message", record)
    connection = CompletionConnection({}, event.id)
    result = WahaSendResult(external_message_id="wa-42", raw={"id": "wa-42"})

    completed = await complete_whatsapp_delivery(
        _Pool(connection),  # type: ignore[arg-type]
        event,
        context,  # type: ignore[arg-type]
        result,
        received_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )

    assert completed is True
    assert recorded[0].external_message_id == "wa-42"
    assert recorded[0].outbox_event_id == event.id
    turn_update = next(
        (sql, args)
        for sql, args in connection.executions
        if "UPDATE supplier_conversation_item_turns" in sql
    )
    assert turn_update[1] == (
        parsed.conversation_id,
        event.approval_id,
        parsed.text,
    )


@pytest.mark.asyncio
async def test_delivery_defers_auto_case_quota_and_skips_provider_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zoneinfo import ZoneInfo

    payload = _payload()
    event = _event(payload)
    parsed = parse_whatsapp_delivery_payload(payload)
    now = datetime(2026, 9, 23, 7, tzinfo=UTC)
    row = {
        "case_id": parsed.case_id,
        "session_name": "procurement",
        "external_chat_id": "77001234567@c.us",
        "state": "waiting_availability",
        "opted_out": False,
        "outbound_count": 2,
        "followup_count": 0,
        "last_outbound_at": now - timedelta(minutes=3),
        "normalized_value": "+77001234567",
        "active": True,
        "sent_today": 30,
    }

    async def active_policy(_connection: object, _case_id: UUID, default: object):
        return replace(default, daily_limit=30, conversation_limit=60)

    async def active_authorization(_connection: object, *, approval_id: UUID) -> None:
        assert approval_id == event.approval_id

    monkeypatch.setattr("procurement_bot.whatsapp_delivery.get_case_send_policy", active_policy)
    monkeypatch.setattr(
        "procurement_bot.whatsapp_delivery.assert_auto_send_authorization_active",
        active_authorization,
    )

    with pytest.raises(WhatsAppSendDeferred) as deferred:
        await assert_whatsapp_delivery_allowed(
            _Pool(_Connection(row, event.id)),  # type: ignore[arg-type]
            event,
            now=now,
        )

    assert deferred.value.reason == "daily WhatsApp limit reached"
    assert deferred.value.available_at == datetime(
        2026, 9, 24, 9, tzinfo=ZoneInfo("Asia/Almaty")
    ).astimezone(UTC)
