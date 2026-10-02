from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.supplier_reply_processing as processing
from procurement_bot.errors import RetryableProviderError
from procurement_bot.queue import stable_key
from procurement_bot.semantic_supplier_reply import (
    SemanticReplyTarget,
    _make_snapshot,
    validate_semantic_reply_proposal,
)
from procurement_bot.supplier_dialogue import SupplierDialogueState
from procurement_bot.supplier_reply_processing import (
    ReplyClassification,
    SupplierReplyOrderPending,
    SupplierReplyProcessingConflict,
    _semantic_quote_details,
    classify_supplier_reply,
    enqueue_favorable_quote_notification,
    extract_quote_details,
    process_supplier_reply,
)
from procurement_bot.suppliers import UpsertResult
from procurement_bot.worker import JobWorker


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


def test_quantified_stock_after_in_stock_phrase_is_kept_in_item_units() -> None:
    classified = classify_supplier_reply("Есть в наличии 5 штук")
    details = extract_quote_details("Есть в наличии 5 штук", item_unit="шт")

    assert classified.classification == ReplyClassification.AVAILABLE
    assert details.available_qty == Decimal("5")


def test_pack_details_require_explicit_pack_size_and_keep_pack_moq() -> None:
    details = extract_quote_details(
        "Цена 900 тг за коробку. Коробка по 6 штук. Минимальная партия 2 коробки.",
        item_unit="шт",
    )

    assert details.price_unit == "коробку"
    assert details.pack_qty == Decimal("6")
    assert details.moq == Decimal("2")


def test_package_stock_is_not_guessed_as_tender_item_stock() -> None:
    details = extract_quote_details(
        "Цена 900 тг за упаковку. В упаковке 6 шт. Есть 5 упаковок.",
        item_unit="шт",
    )

    assert details.available_qty is None


def test_separate_goods_and_delivery_prices_are_ambiguous() -> None:
    classified = classify_supplier_reply("Товар 1 000 тг, доставка 1 000 тг")

    assert classified.classification == ReplyClassification.AMBIGUOUS
    assert classified.price is None


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
            "created_at": datetime(2026, 9, 23, 0, 0, 1, tzinfo=UTC),
            "case_id": self.case_id,
            "supplier_id": self.supplier_id,
            "state": state,
            "opted_out": opted_out,
            "session_name": "default",
            "external_chat_id": "77001234567@c.us",
            "preferred_language": "ru",
            "case_status": "evaluating",
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
        self.preceding_messages: list[dict[str, UUID]] = []
        self.actor_busy = False
        self.actor_id = uuid4()

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *_args: object) -> str:
        self.sql.append(sql)
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "INSERT INTO chat_actor_leases" in sql:
            return "INSERT 0 0"
        if "UPDATE chat_actor_leases" in sql:
            return "UPDATE 1"
        if "UPDATE supplier_dialogue_assignments" in sql:
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.sql.append(sql)
        if "UPDATE chat_actor_leases" in sql:
            if self.actor_busy:
                return None
            return {
                "actor_id": self.actor_id,
                "owner_worker_id": args[2],
                "generation": 2,
                "lease_expires_at": datetime(2026, 9, 23, 0, 1, tzinfo=UTC),
            }
        if "FROM workflow_events" in sql:
            return self.events.get(str(args[0]))
        if "FROM chat_actor_leases" in sql:
            if "UPDATE chat_actor_leases" in sql and self.actor_busy:
                return None
            return {
                "actor_id": self.actor_id,
                "owner_worker_id": None,
                "generation": 1,
                "lease_expires_at": datetime(2026, 9, 23, 0, 1, tzinfo=UTC),
            }
        if "FROM supplier_conversation_messages" in sql:
            if args == (self.message_id, self.conversation_id):
                return self.message
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *_args: object) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "FROM supplier_conversation_messages" in sql:
            return self.preceding_messages
        if "FROM offers" in sql:
            return self.candidates
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> int | None:
        self.sql.append(sql)
        if "SELECT 1 FROM workflow_events" in sql:
            return 1 if str(args[0]) in self.events else None
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
async def test_semantic_reply_rechecks_snapshot_and_is_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(text="Есть в наличии, цена 12 500 тг")
    await _install_persistence_fakes(monkeypatch, connection)
    target = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=connection.offer_id,
        request_item_id=connection.item_id,
        line_number=1,
        item_name="Марля",
        quantity=Decimal("20"),
        unit="рулон",
        specification_text="ГОСТ, 10 метров",
    )
    snapshot = _make_snapshot(
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
        case_id=connection.case_id,
        supplier_id=connection.supplier_id,
        message_text=connection.message["text_content"],
        targets=(target,),
    )
    proposal = validate_semantic_reply_proposal(
        snapshot,
        {
            "context_sha256": snapshot.context_sha256,
            "classification": "price_received",
            "classification_quote": "цена 12 500 тг",
            "target_offer_id": str(connection.offer_id),
            "target_item_id": str(connection.item_id),
            "facts": [
                {
                    "offer_id": str(connection.offer_id),
                    "item_id": str(connection.item_id),
                    "field": "price",
                    "value": "12500",
                    "evidence_quote": "цена 12 500 тг",
                },
                {
                    "offer_id": str(connection.offer_id),
                    "item_id": str(connection.item_id),
                    "field": "currency",
                    "value": "KZT",
                    "evidence_quote": "цена 12 500 тг",
                },
            ],
            "reason": "Supplier quoted one price.",
        },
    )

    async def current_snapshot(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(processing, "load_semantic_reply_snapshot", current_snapshot)
    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
        semantic_snapshot=snapshot,
        semantic_proposal=proposal,
    )

    assert result.offer_id == connection.offer_id
    assert result.offer_observation_id == connection.observation_id
    assert connection.observation_value.price_amount == Decimal("12500")
    assert (
        connection.evidence_value.raw_payload["semantic_proposal"]["facts"][0]["evidence_quote"]
        == "цена 12 500 тг"
    )
    replay = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
        semantic_snapshot=snapshot,
        semantic_proposal=proposal,
    )
    assert replay.newly_processed is False


@pytest.mark.asyncio
async def test_semantic_projection_scopes_multi_item_price_and_stock_to_literal_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Марля: 12 500 тг, осталось 3 рулона. Бинт: 900 тг, осталось 40 штук."
    connection = _Connection(text=text)
    await _install_persistence_fakes(monkeypatch, connection)
    target = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=connection.offer_id,
        request_item_id=connection.item_id,
        line_number=1,
        item_name="Марля",
        quantity=Decimal("20"),
        unit="рулон",
        specification_text="ГОСТ, 10 метров",
    )
    other = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=uuid4(),
        request_item_id=uuid4(),
        line_number=2,
        item_name="Бинт",
        quantity=Decimal("40"),
        unit="шт",
        specification_text="стерильный бинт",
    )
    snapshot = _make_snapshot(
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
        case_id=connection.case_id,
        supplier_id=connection.supplier_id,
        message_text=text,
        targets=(target, other),
    )
    proposal = validate_semantic_reply_proposal(
        snapshot,
        {
            "context_sha256": snapshot.context_sha256,
            "classification": "price_received",
            "classification_quote": "Марля: 12 500 тг",
            "target_offer_id": str(target.offer_id),
            "target_item_id": str(target.request_item_id),
            "facts": [
                {
                    "offer_id": str(target.offer_id),
                    "item_id": str(target.request_item_id),
                    "field": "price",
                    "value": "12500",
                    "evidence_quote": "Марля: 12 500 тг",
                },
                {
                    "offer_id": str(target.offer_id),
                    "item_id": str(target.request_item_id),
                    "field": "currency",
                    "value": "KZT",
                    "evidence_quote": "Марля: 12 500 тг",
                },
                {
                    "offer_id": str(target.offer_id),
                    "item_id": str(target.request_item_id),
                    "field": "stock",
                    "value": "3",
                    "evidence_quote": "Марля: 12 500 тг, осталось 3 рулона",
                },
                {
                    "offer_id": str(target.offer_id),
                    "item_id": str(target.request_item_id),
                    "field": "stock_unit",
                    "value": "рулон",
                    "evidence_quote": "Марля: 12 500 тг, осталось 3 рулона",
                },
            ],
            "reason": "The supplier quotes the selected item.",
        },
    )

    async def current_snapshot(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(processing, "load_semantic_reply_snapshot", current_snapshot)
    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
        semantic_snapshot=snapshot,
        semantic_proposal=proposal,
    )

    assert result.offer_id == target.offer_id
    assert connection.observation_value.price_amount == Decimal("12500")
    assert connection.observation_value.available_qty == Decimal("3")


def test_semantic_quantities_without_matching_item_units_are_not_projected() -> None:
    from procurement_bot.semantic_supplier_reply import (
        SemanticReplyFact,
        ValidatedSemanticReplyProposal,
    )

    offer_id = uuid4()
    item_id = uuid4()
    proposal = ValidatedSemanticReplyProposal(
        context_sha256="0" * 64,
        classification="price_received",
        classification_quote="Марля 12 500 тг",
        target_offer_id=offer_id,
        target_item_id=item_id,
        facts=(
            SemanticReplyFact(
                offer_id=offer_id,
                request_item_id=item_id,
                field="stock",
                value="40",
                evidence_quote="Осталось 40",
            ),
        ),
        reason="Partial stock quantity without a unit.",
    )
    details = _semantic_quote_details(proposal, item_unit="рулон")

    assert details.available_qty is None


@pytest.mark.asyncio
async def test_semantic_reply_rejects_stale_snapshot_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(text="Цена 12 500 тг")
    snapshot = _make_snapshot(
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
        case_id=connection.case_id,
        supplier_id=connection.supplier_id,
        message_text=connection.message["text_content"],
        targets=(
            SemanticReplyTarget(
                target_id=uuid4(),
                offer_id=connection.offer_id,
                request_item_id=connection.item_id,
                line_number=1,
                item_name="Марля",
                quantity=Decimal("20"),
                unit="рулон",
                specification_text="ГОСТ, 10 метров",
            ),
        ),
    )
    proposal = validate_semantic_reply_proposal(
        snapshot,
        {
            "context_sha256": snapshot.context_sha256,
            "classification": "price_received",
            "classification_quote": "Цена 12 500 тг",
            "target_offer_id": str(connection.offer_id),
            "target_item_id": str(connection.item_id),
            "facts": [],
            "reason": "Supplier quoted one price.",
        },
    )

    async def changed_snapshot(*_args, **_kwargs):
        return replace(snapshot, context_sha256="f" * 64)

    monkeypatch.setattr(processing, "load_semantic_reply_snapshot", changed_snapshot)
    with pytest.raises(SupplierReplyProcessingConflict, match="stale"):
        await process_supplier_reply(
            connection,  # type: ignore[arg-type]
            conversation_id=connection.conversation_id,
            message_id=connection.message_id,
            semantic_snapshot=snapshot,
            semantic_proposal=proposal,
        )
    assert connection.events == {}


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
    assert transitions == [(SupplierDialogueState.WAITING_SPEC, SupplierDialogueState.ESCALATED)]


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
    assert transitions == [(SupplierDialogueState.WAITING_PRICE, SupplierDialogueState.COMPLETE)]


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_state", ["closed_no_reply", "complete"])
async def test_late_reply_after_terminal_dialogue_is_saved_without_reopening(
    monkeypatch: pytest.MonkeyPatch,
    conversation_state: str,
) -> None:
    connection = _Connection(text="Цена 12 500 тг", state=conversation_state)
    transitions = await _install_persistence_fakes(monkeypatch, connection)

    result = await process_supplier_reply(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        message_id=connection.message_id,
    )

    assert result.state == SupplierDialogueState(conversation_state)
    assert result.offer_id == connection.offer_id
    assert connection.observation_value.price_amount == Decimal("12500")
    assert transitions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case_status", ["closed", "cancelled"])
async def test_reply_projection_rejects_closed_or_cancelled_case(case_status: str) -> None:
    connection = _Connection(state="closed_no_reply")
    connection.message["case_status"] = case_status

    with pytest.raises(SupplierReplyProcessingConflict, match="closed or cancelled case"):
        await process_supplier_reply(
            connection,  # type: ignore[arg-type]
            conversation_id=connection.conversation_id,
            message_id=connection.message_id,
        )


@pytest.mark.asyncio
async def test_favorable_notification_requires_qualified_supplier_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer_id = uuid4()
    item_id = uuid4()
    case_id = uuid4()
    snapshot_id = uuid4()
    facts = {
        "price_amount": {"value": "900", "source_type": "supplier_message"},
        "currency": {"value": "KZT", "source_type": "supplier_message"},
        "price_unit": {"value": "шт", "source_type": "supplier_message"},
        "available_qty": {"value": "20", "source_type": "supplier_message"},
        "availability_status": {"value": "in_stock", "source_type": "supplier_message"},
    }
    selected = {
        "offer_id": offer_id,
        "request_item_id": item_id,
        "supplier_id": uuid4(),
        "supplier_name": "Снабжение",
        "item_name": "Марля",
        "quantity": Decimal("10"),
        "unit": "шт",
        "tender_unit_price_private": Decimal("1000"),
        "tender_currency": "KZT",
        "telegram_id": 1234,
        "owner_active": True,
        "snapshot_id": snapshot_id,
        "quote_item_id": item_id,
        "quote_snapshot": {"facts": facts},
    }
    sibling = {
        **{
            key: selected[key]
            for key in (
                "offer_id",
                "request_item_id",
                "supplier_id",
                "quantity",
                "unit",
                "tender_unit_price_private",
                "tender_currency",
            )
        },
        "offer_status": "confirm",
        "quote_item_id": item_id,
        "quote_snapshot": {"facts": facts},
    }

    class Connection:
        async def fetchrow(self, _sql: str, *_args: object) -> dict[str, Any]:
            return selected

        async def fetchval(self, _sql: str, *_args: object) -> UUID:
            return case_id

        async def fetch(self, _sql: str, *_args: object) -> list[dict[str, Any]]:
            return [sibling]

    captured: dict[str, Any] = {}

    async def reviews(_connection: object, _case_id: UUID) -> tuple[list[Any], list[Any]]:
        return [], []

    def states(_offers: object, _requirements: object, _reviews: object) -> dict[str, str]:
        return {str(offer_id): "verified"}

    async def enqueue(_connection: object, **kwargs: object) -> UUID:
        captured.update(kwargs)
        return uuid4()

    monkeypatch.setattr(processing, "load_case_technical_reviews", reviews)
    monkeypatch.setattr(processing, "technical_states", states)
    monkeypatch.setattr(processing, "enqueue_outbox", enqueue)

    queued = await enqueue_favorable_quote_notification(
        Connection(),  # type: ignore[arg-type]
        offer_id=offer_id,
    )

    assert queued is True
    assert captured["event_type"] == "telegram.send_message"
    assert captured["payload"]["chat_id"] == 1234
    assert "ниже тендерного ориентира на 1000 KZT" in captured["payload"]["text"]
    assert captured["idempotency_key"] == stable_key("favorable-supplier-quote", snapshot_id)

    facts["price_amount"]["source_type"] = "public_listing"
    assert not await enqueue_favorable_quote_notification(
        Connection(),  # type: ignore[arg-type]
        offer_id=offer_id,
    )


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
    assert transitions == [(SupplierDialogueState.WAITING_PRICE, SupplierDialogueState.COMPLETE)]


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
async def test_later_reply_waits_for_unprojected_earlier_message() -> None:
    connection = _Connection()
    connection.preceding_messages = [{"id": uuid4()}]

    with pytest.raises(
        SupplierReplyProcessingConflict,
        match="earlier supplier reply must be projected first",
    ):
        await process_supplier_reply(
            connection,  # type: ignore[arg-type]
            conversation_id=connection.conversation_id,
            message_id=connection.message_id,
        )


@pytest.mark.asyncio
async def test_reply_projection_defers_while_outbound_chat_actor_is_owned() -> None:
    connection = _Connection()
    connection.actor_busy = True

    with pytest.raises(
        SupplierReplyOrderPending,
        match="physical WhatsApp chat is busy",
    ):
        await process_supplier_reply(
            connection,  # type: ignore[arg-type]
            conversation_id=connection.conversation_id,
            message_id=connection.message_id,
        )


def test_reply_order_deferrals_are_retryable_jobs() -> None:
    assert issubclass(SupplierReplyOrderPending, SupplierReplyProcessingConflict)
    assert issubclass(SupplierReplyOrderPending, RetryableProviderError)
    assert JobWorker._is_retryable(SupplierReplyOrderPending("try later"))


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
