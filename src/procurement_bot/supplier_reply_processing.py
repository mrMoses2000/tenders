from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.conversations import (
    ConversationState,
    transition_conversation_state,
)
from procurement_bot.errors import ValidationBlocked
from procurement_bot.queue import stable_key
from procurement_bot.supplier_dialogue import (
    SupplierDialogueState,
    SupplierInquiry,
    SupplierLanguage,
    SupplierReply,
    advance_dialogue,
    detect_supplier_language,
    is_opt_out,
)
from procurement_bot.suppliers import (
    OfferObservationInput,
    SourceEvidenceInput,
    upsert_offer_observation,
    upsert_source_evidence,
)


class SupplierReplyProcessingConflict(RuntimeError):
    """The reply cannot be projected without contradicting durable state."""


class ReplyClassification(StrEnum):
    OPT_OUT = "opt_out"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"
    PRICE_RECEIVED = "price_received"
    AMBIGUOUS = "ambiguous"
    UNRECOGNIZED = "unrecognized"


@dataclass(frozen=True, slots=True)
class ExplicitPrice:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class ClassifiedSupplierReply:
    classification: ReplyClassification
    availability_status: str = "unknown"
    price: ExplicitPrice | None = None
    signals: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SupplierReplyProcessingResult:
    conversation_id: UUID
    message_id: UUID
    case_id: UUID
    classification: ReplyClassification
    previous_state: SupplierDialogueState
    state: SupplierDialogueState
    needs_human: bool
    reason: str
    request_item_id: UUID | None = None
    offer_id: UUID | None = None
    source_evidence_id: UUID | None = None
    offer_observation_id: UUID | None = None
    draft_outbound_text: str | None = None
    newly_processed: bool = True


_UNAVAILABLE_PATTERNS = (
    re.compile(r"\b(?:нет|нету)\s+в\s+наличии\b", re.IGNORECASE),
    re.compile(r"\bв\s+наличии\s+(?:нет|нету)\b", re.IGNORECASE),
    re.compile(r"\bне\s+в\s+наличии\b", re.IGNORECASE),
    re.compile(r"\bне\s+имеется\b", re.IGNORECASE),
    re.compile(
        r"\b(?:отсутствует|отсутствуют|закончился|закончилась|закончились)\b",
        re.IGNORECASE,
    ),
)
_AVAILABLE_PATTERNS = (
    re.compile(r"\bесть\s+в\s+наличии\b", re.IGNORECASE),
    re.compile(r"\bимеется\s+в\s+наличии\b", re.IGNORECASE),
    re.compile(r"^\s*в\s+наличии\b", re.IGNORECASE),
)
_EXACT_UNAVAILABLE = re.compile(
    r"^\s*(?:нет|нету|отсутствует|закончился|закончилась|закончились)[.!?\s]*$",
    re.IGNORECASE,
)
_CURRENCY_AMOUNT = re.compile(
    r"(?:(?P<prefix>[₸$€])\s*(?P<prefix_amount>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)"
    r"|(?P<suffix_amount>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*"
    r"(?P<suffix>тг\.?|тенге|kzt|usd|доллар(?:а|ов)?|eur|евро))",
    re.IGNORECASE,
)
_PRICE_WITHOUT_CURRENCY = re.compile(
    r"\b(?:цена|стоимость|по)\s*[:—-]?\s*\d", re.IGNORECASE
)
_CURRENCY_CODES = {
    "₸": "KZT",
    "тг": "KZT",
    "тг.": "KZT",
    "тенге": "KZT",
    "kzt": "KZT",
    "$": "USD",
    "usd": "USD",
    "доллар": "USD",
    "доллара": "USD",
    "долларов": "USD",
    "€": "EUR",
    "eur": "EUR",
    "евро": "EUR",
}
_ACTIVE_OFFER_STATUSES = ("lead", "confirm", "exact", "mismatch")
_PROCESS_EVENT = "supplier_reply_processed"


def classify_supplier_reply(text: str) -> ClassifiedSupplierReply:
    """Extract only high-precision facts; generic confirmations are not spec facts."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = " ".join(text.split())
    if not normalized:
        return ClassifiedSupplierReply(
            ReplyClassification.UNRECOGNIZED,
            reason="empty supplier text",
        )
    if is_opt_out(normalized):
        return ClassifiedSupplierReply(
            ReplyClassification.OPT_OUT,
            signals=("opt_out",),
            reason="explicit opt-out phrase",
        )

    unavailable = bool(_EXACT_UNAVAILABLE.fullmatch(normalized)) or any(
        pattern.search(normalized) for pattern in _UNAVAILABLE_PATTERNS
    )
    available = any(pattern.search(normalized) for pattern in _AVAILABLE_PATTERNS)
    prices = _extract_explicit_prices(normalized)
    price_shaped = bool(prices) or bool(_PRICE_WITHOUT_CURRENCY.search(normalized))
    signals = tuple(
        signal
        for signal, present in (
            ("available", available),
            ("unavailable", unavailable),
            ("price", price_shaped),
        )
        if present
    )

    if available and unavailable:
        return ClassifiedSupplierReply(
            ReplyClassification.AMBIGUOUS,
            signals=signals,
            reason="conflicting availability statements",
        )
    availability_status = (
        "out_of_stock"
        if unavailable
        else "in_stock_unquantified"
        if available
        else "unknown"
    )
    if len(prices) > 1:
        return ClassifiedSupplierReply(
            ReplyClassification.AMBIGUOUS,
            availability_status=availability_status,
            signals=signals,
            reason="multiple distinct price amounts",
        )
    if price_shaped and not prices:
        return ClassifiedSupplierReply(
            ReplyClassification.AMBIGUOUS,
            availability_status=availability_status,
            signals=signals,
            reason="price has no unambiguous currency and amount",
        )
    if unavailable:
        return ClassifiedSupplierReply(
            ReplyClassification.UNAVAILABLE,
            availability_status="out_of_stock",
            price=next(iter(prices), None),
            signals=signals,
            reason="explicitly unavailable",
        )
    if available:
        return ClassifiedSupplierReply(
            ReplyClassification.AVAILABLE,
            availability_status="in_stock_unquantified",
            price=next(iter(prices), None),
            signals=signals,
            reason="explicitly available",
        )
    if prices:
        return ClassifiedSupplierReply(
            ReplyClassification.PRICE_RECEIVED,
            price=prices[0],
            signals=signals,
            reason="one explicit amount and currency",
        )
    return ClassifiedSupplierReply(
        ReplyClassification.UNRECOGNIZED,
        reason="no deterministic supplier fact",
    )


async def process_supplier_reply(
    connection: asyncpg.Connection,
    *,
    conversation_id: UUID,
    message_id: UUID,
) -> SupplierReplyProcessingResult:
    """Project one immutable inbound reply without sending an outbound message."""

    if not isinstance(conversation_id, UUID) or not isinstance(message_id, UUID):
        raise TypeError("conversation_id and message_id must be UUID values")
    event_key = stable_key("supplier-reply-processed", message_id)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", event_key
        )
        existing = await connection.fetchrow(
            "SELECT event_type,data FROM workflow_events WHERE idempotency_key=$1",
            event_key,
        )
        if existing is not None:
            return _result_from_event(
                existing,
                expected_conversation_id=conversation_id,
                expected_message_id=message_id,
            )

        row = await connection.fetchrow(
            """
            SELECT
                m.id AS message_id,m.conversation_id,m.direction,m.message_kind,
                m.text_content,m.external_message_id,m.received_at,
                c.case_id,c.supplier_id,c.state,c.opted_out,c.session_name,
                c.preferred_language,
                c.external_chat_id
            FROM supplier_conversation_messages AS m
            JOIN supplier_conversations AS c ON c.id=m.conversation_id
            WHERE m.id=$1 AND c.id=$2
            FOR UPDATE OF c
            """,
            message_id,
            conversation_id,
        )
        if row is None:
            raise ValueError("inbound supplier message does not exist in conversation")
        if row["direction"] != "inbound":
            raise SupplierReplyProcessingConflict("message is not inbound")

        previous_state = SupplierDialogueState(row["state"])
        classified = classify_supplier_reply(row["text_content"])
        language = detect_supplier_language(
            row["text_content"],
            fallback=SupplierLanguage(row["preferred_language"]),
        )
        candidates = await connection.fetch(
            """
            SELECT
                o.id AS offer_id,ri.id AS request_item_id,ri.name,
                ri.specification_text,ri.quantity,ri.unit
            FROM offers AS o
            JOIN supplier_conversation_targets AS target
              ON target.offer_id=o.id AND target.request_item_id=o.request_item_id
            JOIN request_items AS ri ON ri.id=o.request_item_id
            WHERE target.conversation_id=$1
              AND target.case_id=$2
              AND target.supplier_id=$3
              AND o.status=ANY($4::text[])
            ORDER BY ri.line_number,o.created_at,o.id
            LIMIT 2
            """,
            conversation_id,
            row["case_id"],
            row["supplier_id"],
            list(_ACTIVE_OFFER_STATUSES),
        )
        candidate = candidates[0] if len(candidates) == 1 else None
        outcome = _decide(
            state=previous_state,
            classified=classified,
            candidate=candidate,
            candidate_count=len(candidates),
            already_opted_out=bool(row["opted_out"]),
            language=language,
        )

        evidence_id: UUID | None = None
        observation_id: UUID | None = None
        if classified.classification not in {
            ReplyClassification.UNRECOGNIZED,
            ReplyClassification.AMBIGUOUS,
        }:
            evidence_payload = {
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "external_message_id": row["external_message_id"],
                "text": row["text_content"],
                "classification": classified.classification.value,
                "signals": list(classified.signals),
            }
            evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="supplier_message",
                    source_locator=(
                        f"whatsapp:{row['session_name']}:{row['external_chat_id']}:"
                        f"{row['external_message_id']}"
                    ),
                    external_id=row["external_message_id"],
                    title="Supplier WhatsApp reply",
                    captured_at=row["received_at"],
                    content_sha256=hashlib.sha256(
                        row["text_content"].encode("utf-8")
                    ).hexdigest(),
                    raw_payload=evidence_payload,
                    idempotency_key=stable_key("supplier-message-evidence", message_id),
                ),
            )
            evidence_id = evidence.id
            if candidate is not None and classified.classification != ReplyClassification.OPT_OUT:
                price = classified.price
                observation = await upsert_offer_observation(
                    connection,
                    OfferObservationInput(
                        offer_id=candidate["offer_id"],
                        source_evidence_id=evidence_id,
                        observed_at=row["received_at"],
                        observed_by=f"supplier:{row['supplier_id']}",
                        source_type="supplier_message",
                        external_message_id=row["external_message_id"],
                        price_amount=price.amount if price is not None else None,
                        currency=price.currency if price is not None else None,
                        availability_status=classified.availability_status,
                        evidence_level=4,
                        raw_payload=evidence_payload,
                        idempotency_key=stable_key(
                            "supplier-message-observation", message_id, candidate["offer_id"]
                        ),
                    ),
                )
                observation_id = observation.id

        if outcome.state != previous_state:
            transitioned = await transition_conversation_state(
                connection,
                conversation_id=conversation_id,
                expected_state=ConversationState(previous_state.value),
                new_state=ConversationState(outcome.state.value),
            )
            if not transitioned:
                raise SupplierReplyProcessingConflict("conversation transition was not applied")

        result = SupplierReplyProcessingResult(
            conversation_id=conversation_id,
            message_id=message_id,
            case_id=row["case_id"],
            classification=classified.classification,
            previous_state=previous_state,
            state=outcome.state,
            needs_human=outcome.needs_human,
            reason=outcome.reason,
            request_item_id=candidate["request_item_id"] if candidate is not None else None,
            offer_id=candidate["offer_id"] if candidate is not None else None,
            source_evidence_id=evidence_id,
            offer_observation_id=observation_id,
            draft_outbound_text=outcome.outbound_text,
        )
        inserted = await connection.fetchval(
            """
            INSERT INTO workflow_events(
                case_id,request_item_id,event_type,actor_type,actor_id,
                idempotency_key,data
            ) VALUES ($1,$2,$3,'worker','supplier-reply-processor',$4,$5::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            result.case_id,
            result.request_item_id,
            _PROCESS_EVENT,
            event_key,
            _event_data(result),
        )
        if inserted is None:
            raise SupplierReplyProcessingConflict("reply event appeared during locked processing")
        return result


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: SupplierDialogueState
    needs_human: bool
    reason: str
    outbound_text: str | None = None


def _decide(
    *,
    state: SupplierDialogueState,
    classified: ClassifiedSupplierReply,
    candidate: Mapping[str, Any] | None,
    candidate_count: int,
    already_opted_out: bool,
    language: SupplierLanguage = SupplierLanguage.RUSSIAN,
) -> _Outcome:
    if classified.classification == ReplyClassification.OPT_OUT:
        if not already_opted_out or state != SupplierDialogueState.OPTED_OUT:
            raise SupplierReplyProcessingConflict(
                "opt-out message was not synchronously projected by inbound handling"
            )
        decision = advance_dialogue(
            state,
            SupplierReply.OPT_OUT,
            _placeholder_inquiry(),
            language,
        )
        return _Outcome(decision.state, decision.needs_human, decision.reason)
    if state == SupplierDialogueState.OPTED_OUT or already_opted_out:
        raise SupplierReplyProcessingConflict("non-opt-out work targets opted-out conversation")
    if state in {
        SupplierDialogueState.COMPLETE,
        SupplierDialogueState.CLOSED_NO_REPLY,
    }:
        raise SupplierReplyProcessingConflict("reply targets a terminal conversation")
    if candidate is None:
        reason = (
            "no active offer is uniquely linked to the conversation"
            if candidate_count == 0
            else "multiple active offers are linked to the conversation"
        )
        return _escalate(state, reason)

    inquiry = SupplierInquiry(
        product_name=str(candidate["name"]),
        quantity=_format_quantity(candidate["quantity"], str(candidate["unit"])),
        required_spec=str(candidate["specification_text"]) or "требования из заявки",
    )
    reply: SupplierReply | None = None
    if (
        classified.classification == ReplyClassification.UNAVAILABLE
        and state
        in {
            SupplierDialogueState.WAITING_AVAILABILITY,
            SupplierDialogueState.WAITING_SPEC,
            SupplierDialogueState.WAITING_PRICE,
            SupplierDialogueState.ESCALATED,
        }
    ):
        reply = SupplierReply.UNAVAILABLE
    elif (
        state == SupplierDialogueState.WAITING_AVAILABILITY
        and classified.classification == ReplyClassification.AVAILABLE
    ):
        reply = SupplierReply.AVAILABLE
    elif (
        state == SupplierDialogueState.WAITING_PRICE
        and classified.price is not None
        and classified.classification
        in {ReplyClassification.PRICE_RECEIVED, ReplyClassification.AVAILABLE}
    ):
        reply = SupplierReply.PRICE_RECEIVED
    if reply is None:
        return _escalate(state, classified.reason)
    try:
        decision = advance_dialogue(state, reply, inquiry, language)
    except ValidationBlocked as exc:
        return _escalate(state, f"dialogue policy rejected reply: {exc}")
    return _Outcome(
        decision.state,
        decision.needs_human,
        decision.reason or classified.reason,
        decision.outbound_text,
    )


def _escalate(state: SupplierDialogueState, reason: str) -> _Outcome:
    if state == SupplierDialogueState.ESCALATED:
        return _Outcome(state, True, reason)
    if state not in {
        SupplierDialogueState.DRAFT,
        SupplierDialogueState.APPROVED,
        SupplierDialogueState.WAITING_AVAILABILITY,
        SupplierDialogueState.WAITING_SPEC,
        SupplierDialogueState.WAITING_PRICE,
    }:
        raise SupplierReplyProcessingConflict(f"cannot escalate conversation from {state}")
    return _Outcome(SupplierDialogueState.ESCALATED, True, reason)


def _extract_explicit_prices(text: str) -> tuple[ExplicitPrice, ...]:
    found: set[tuple[Decimal, str]] = set()
    for match in _CURRENCY_AMOUNT.finditer(text):
        currency_token = match.group("prefix") or match.group("suffix")
        amount_token = match.group("prefix_amount") or match.group("suffix_amount")
        if currency_token is None or amount_token is None:
            continue
        currency = _CURRENCY_CODES[currency_token.casefold()]
        compact = re.sub(r"[\s\u00a0]", "", amount_token).replace(",", ".")
        try:
            amount = Decimal(compact)
        except InvalidOperation:
            continue
        if amount >= 0:
            found.add((amount, currency))
    return tuple(
        ExplicitPrice(amount=amount, currency=currency)
        for amount, currency in sorted(found, key=lambda value: (value[1], value[0]))
    )


def _format_quantity(quantity: Any, unit: str) -> str:
    if quantity is None:
        return unit.strip() or "количество из заявки"
    return f"{quantity} {unit}".strip()


def _placeholder_inquiry() -> SupplierInquiry:
    return SupplierInquiry("товар", "количество из заявки", "требования из заявки")


def _event_data(result: SupplierReplyProcessingResult) -> dict[str, Any]:
    return {
        "conversation_id": str(result.conversation_id),
        "message_id": str(result.message_id),
        "case_id": str(result.case_id),
        "classification": result.classification.value,
        "previous_state": result.previous_state.value,
        "state": result.state.value,
        "needs_human": result.needs_human,
        "reason": result.reason,
        "request_item_id": str(result.request_item_id) if result.request_item_id else None,
        "offer_id": str(result.offer_id) if result.offer_id else None,
        "source_evidence_id": (
            str(result.source_evidence_id) if result.source_evidence_id else None
        ),
        "offer_observation_id": (
            str(result.offer_observation_id) if result.offer_observation_id else None
        ),
        "draft_outbound_text": result.draft_outbound_text,
    }


def _result_from_event(
    event: Mapping[str, Any],
    *,
    expected_conversation_id: UUID,
    expected_message_id: UUID,
) -> SupplierReplyProcessingResult:
    if event["event_type"] != _PROCESS_EVENT or not isinstance(event["data"], Mapping):
        raise SupplierReplyProcessingConflict("idempotency key belongs to another event")
    data = event["data"]
    try:
        result = SupplierReplyProcessingResult(
            conversation_id=UUID(str(data["conversation_id"])),
            message_id=UUID(str(data["message_id"])),
            case_id=UUID(str(data["case_id"])),
            classification=ReplyClassification(data["classification"]),
            previous_state=SupplierDialogueState(data["previous_state"]),
            state=SupplierDialogueState(data["state"]),
            needs_human=data["needs_human"],
            reason=data["reason"],
            request_item_id=_optional_uuid(data["request_item_id"]),
            offer_id=_optional_uuid(data["offer_id"]),
            source_evidence_id=_optional_uuid(data["source_evidence_id"]),
            offer_observation_id=_optional_uuid(data["offer_observation_id"]),
            draft_outbound_text=data["draft_outbound_text"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SupplierReplyProcessingConflict("stored reply event is malformed") from exc
    if (
        result.conversation_id != expected_conversation_id
        or result.message_id != expected_message_id
    ):
        raise SupplierReplyProcessingConflict("stored reply event targets different input")
    return replace(result, newly_processed=False)


def _optional_uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))
