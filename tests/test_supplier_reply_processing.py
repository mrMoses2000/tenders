from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.supplier_reply_processing as processing
from procurement_bot.supplier_dialogue import SupplierDialogueState
from procurement_bot.supplier_reply_processing import (
    ReplyClassification,
    SupplierReplyProcessingConflict,
    classify_supplier_reply,
    process_supplier_reply,
)
from procurement_bot.suppliers import UpsertResult


@pytest.mark.parametrize(
    ("text", "classification", "amount", "currency", "availability"),
    [
        ("Не пишите мне больше", ReplyClassification.OPT_OUT, None, None, "unknown"),
        ("Нет в наличии", ReplyClassification.UNAVAILABLE, None, None, "out_of_stock"),
        (
            "Есть в наличии",
            ReplyClassification.AVAILABLE,
            None,
            None,
            "in_stock_unquantified",
        ),
        (
            "В наличии, осталось 5 штук",
            ReplyClassification.AVAILABLE,
            None,
            None,
            "in_stock_unquantified",
        ),
        ("Цена 12 500 тг", ReplyClassification.PRICE_RECEIVED, Decimal("12500"), "KZT", "unknown"),
        ("€99,50", ReplyClassification.PRICE_RECEIVED, Decimal("99.50"), "EUR", "unknown"),
        ("Да, соответствует", ReplyClassification.UNRECOGNIZED, None, None, "unknown"),
        ("Цена 10000", ReplyClassification.AMBIGUOUS, None, None, "unknown"),
        (
            "Есть в наличии, но уже нет в наличии",
            ReplyClassification.AMBIGUOUS,
            None,
            None,
            "unknown",
        ),
        ("1000 тг или 1200 тг", ReplyClassification.AMBIGUOUS, None, None, "unknown"),
    ],
)
def test_classifier_extracts_only_unambiguous_facts(
    text: str,
    classification: ReplyClassification,
    amount: Decimal | None,
    currency: str | None,
    availability: str,
) -> None:
    result = classify_supplier_reply(text)

    assert result.classification == classification
    assert result.availability_status == availability
    assert (result.price.amount if result.price else None) == amount
    assert (result.price.currency if result.price else None) == currency


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        text: str = "Есть в наличии, цена 12 500 тг",
        state: str = "waiting_availability",
        opted_out: bool = False,
        candidate_count: int = 1,
    ) -> None:
        self.conversation_id = uuid4()
        self.message_id = uuid4()
        self.case_id = uuid4()
        self.supplier_id = uuid4()
        self.offer_id = uuid4()
        self.item_id = uuid4()
        self.evidence_id = uuid4()
        self.observation_id = uuid4()
        self.message = {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "direction": "inbound",
            "message_kind": "text",
            "text_content": text,
            "external_message_id": "wamid-1",
            "received_at": datetime(2026, 9, 23, 0, 0, tzinfo=UTC),
            "case_id": self.case_id,
            "supplier_id": self.supplier_id,
            "state": state,
            "opted_out": opted_out,
            "session_name": "default",
            "external_chat_id": "77001234567@c.us",
            "preferred_language": "ru",
        }
        candidate = {
            "offer_id": self.offer_id,
            "request_item_id": self.item_id,
            "name": "Марля",
            "specification_text": "ГОСТ, 10 метров",
            "quantity": Decimal("20"),
            "unit": "рулон",
        }
        self.candidates = [dict(candidate) for _ in range(candidate_count)]
        if candidate_count > 1:
            self.candidates[1]["offer_id"] = uuid4()
        self.events: dict[str, dict[str, Any]] = {}
        self.sql: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *_args: object) -> str:
        self.sql.append(sql)
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.sql.append(sql)
        if "FROM workflow_events" in sql:
            return self.events.get(str(args[0]))
        if "FROM supplier_conversation_messages" in sql:
            if args == (self.message_id, self.conversation_id):
                return self.message
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *_args: object) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "FROM offers" in sql:
            return self.candidates
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> int | None:
        self.sql.append(sql)
        if "INSERT INTO workflow_events" in sql:
            event_type = str(args[2])
            key = str(args[3])
            if key in self.events:
                return None
            self.events[key] = {"event_type": event_type, "data": args[4]}
            return 1
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


async def _install_persistence_fakes(
    monkeypatch: pytest.MonkeyPatch,
    connection: _Connection,
) -> list[tuple[SupplierDialogueState, SupplierDialogueState]]:
    transitions: list[tuple[SupplierDialogueState, SupplierDialogueState]] = []

    async def evidence(_connection: object, value: object) -> UpsertResult:
        connection.evidence_value = value
        return UpsertResult(connection.evidence_id, True)

    async def observation(_connection: object, value: object) -> UpsertResult:
        connection.observation_value = value
        return UpsertResult(connection.observation_id, True)

    async def transition(
        _connection: object,
        *,
        conversation_id: UUID,
        expected_state: object,
        new_state: object,
    ) -> bool:
        assert conversation_id == connection.conversation_id
        transitions.append(
            (SupplierDialogueState(str(expected_state)), SupplierDialogueState(str(new_state)))
        )
        connection.message["state"] = str(new_state)
        return True

    monkeypatch.setattr(processing, "upsert_source_evidence", evidence)
    monkeypatch.setattr(processing, "upsert_offer_observation", observation)
    monkeypatch.setattr(processing, "transition_conversation_state", transition)
    return transitions


@pytest.mark.asyncio
async def test_available_reply_persists_fact_advances_and_only_returns_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    transitions = await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.classification == ReplyClassification.AVAILABLE
    assert result.state == SupplierDialogueState.WAITING_SPEC
    assert result.needs_human is False
    assert "Подтвердите" in (result.draft_outbound_text or "")
    assert transitions == [
        (SupplierDialogueState.WAITING_AVAILABILITY, SupplierDialogueState.WAITING_SPEC)
    ]
    assert connection.evidence_value.raw_payload["text"] == connection.message["text_content"]
    assert connection.observation_value.price_amount == Decimal("12500")
    assert connection.observation_value.currency == "KZT"
    assert connection.observation_value.availability_status == "in_stock_unquantified"
    assert not any("outbox_events" in sql or "action_approvals" in sql for sql in connection.sql)


@pytest.mark.asyncio
async def test_generic_spec_confirmation_is_never_treated_as_conformity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(text="Да, соответствует", state="waiting_spec")
    transitions = await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.classification == ReplyClassification.UNRECOGNIZED
    assert result.state == SupplierDialogueState.ESCALATED
    assert result.needs_human is True
    assert result.source_evidence_id is None
    assert transitions == [
        (SupplierDialogueState.WAITING_SPEC, SupplierDialogueState.ESCALATED)
    ]


@pytest.mark.asyncio
async def test_price_while_waiting_price_completes_with_direct_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(text="По 8 750 тенге", state="waiting_price")
    transitions = await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.classification == ReplyClassification.PRICE_RECEIVED
    assert result.state == SupplierDialogueState.COMPLETE
    assert result.draft_outbound_text is None
    assert connection.observation_value.price_amount == Decimal("8750")
    assert transitions == [
        (SupplierDialogueState.WAITING_PRICE, SupplierDialogueState.COMPLETE)
    ]


@pytest.mark.asyncio
async def test_availability_phrase_does_not_hide_explicit_price_at_price_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(
        text="Есть в наличии, цена 12 500 тг",
        state="waiting_price",
    )
    transitions = await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.classification == ReplyClassification.AVAILABLE
    assert result.state == SupplierDialogueState.COMPLETE
    assert connection.observation_value.price_amount == Decimal("12500")
    assert transitions == [
        (SupplierDialogueState.WAITING_PRICE, SupplierDialogueState.COMPLETE)
    ]


@pytest.mark.asyncio
async def test_ambiguous_item_association_escalates_without_offer_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(candidate_count=2)
    await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.state == SupplierDialogueState.ESCALATED
    assert result.needs_human is True
    assert result.offer_id is None
    assert result.offer_observation_id is None
    assert result.source_evidence_id == connection.evidence_id
    assert not hasattr(connection, "observation_value")


@pytest.mark.asyncio
async def test_unavailable_reply_in_draft_escalates_instead_of_invalid_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(text="Нет в наличии", state="draft")
    transitions = await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.classification == ReplyClassification.UNAVAILABLE
    assert result.state == SupplierDialogueState.ESCALATED
    assert result.needs_human is True
    assert connection.observation_value.availability_status == "out_of_stock"
    assert transitions == [(SupplierDialogueState.DRAFT, SupplierDialogueState.ESCALATED)]


@pytest.mark.asyncio
async def test_retry_is_resolved_from_workflow_event_without_duplicate_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    transitions = await _install_persistence_fakes(monkeypatch, connection)
    first = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("retry must not repeat a projection")

    monkeypatch.setattr(processing, "upsert_source_evidence", forbidden)
    monkeypatch.setattr(processing, "upsert_offer_observation", forbidden)
    monkeypatch.setattr(processing, "transition_conversation_state", forbidden)
    retry = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert retry == processing.replace(first, newly_processed=False)
    assert len(transitions) == 1
    assert len(connection.events) == 1


@pytest.mark.asyncio
async def test_opt_out_requires_synchronous_inbound_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(text="Не пишите мне", state="waiting_availability")
    await _install_persistence_fakes(monkeypatch, connection)

    with pytest.raises(SupplierReplyProcessingConflict, match="synchronously projected"):
        await process_supplier_reply(
            connection,  # type: ignore[arg-type]
            conversation_id=connection.conversation_id,
            message_id=connection.message_id,
        )


@pytest.mark.asyncio
async def test_rejects_outbound_message() -> None:
    connection = _Connection()
    connection.message["direction"] = "outbound"

    with pytest.raises(SupplierReplyProcessingConflict, match="not inbound"):
        await process_supplier_reply(
            connection,  # type: ignore[arg-type]
            conversation_id=connection.conversation_id,
            message_id=connection.message_id,
        )
