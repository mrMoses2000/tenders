from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.chat_actors import (
    ChatActorBusy,
    acquire_chat_actor_in_transaction,
    release_chat_actor_in_transaction,
)
from procurement_bot.conversations import (
    ConversationState,
    transition_conversation_state,
)
from procurement_bot.errors import RetryableProviderError, ValidationBlocked
from procurement_bot.offer_comparison import (
    GoodsComparison,
    normalize_goods_unit,
    rank_goods_offers,
)
from procurement_bot.offer_technical import load_case_technical_reviews, technical_states
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_outbox, stable_key
from procurement_bot.semantic_supplier_reply import (
    SemanticReplySnapshot,
    SemanticReplyValidationError,
    ValidatedSemanticReplyProposal,
    assert_snapshot_current,
    load_semantic_reply_snapshot,
)
from procurement_bot.supplier_dialogue import (
    SupplierDialogueState,
    SupplierInquiry,
    SupplierLanguage,
    SupplierReply,
    advance_dialogue,
    detect_supplier_language,
    is_opt_out,
)
from procurement_bot.supplier_media_evidence import media_clarification, supplier_evidence_text
from procurement_bot.suppliers import (
    OfferObservationInput,
    SourceEvidenceInput,
    upsert_offer_observation,
    upsert_source_evidence,
)


class SupplierReplyProcessingConflict(RuntimeError):
    """The reply cannot be projected without contradicting durable state."""


class SupplierReplyOrderPending(
    SupplierReplyProcessingConflict,
    RetryableProviderError,
):
    """A prior or concurrent physical-chat event must finish before this reply."""


class ReplyClassification(StrEnum):
    OPT_OUT = "opt_out"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"
    PRICE_RECEIVED = "price_received"
    SPEC_CONFIRMED = "spec_confirmed"
    SPEC_MISMATCH = "spec_mismatch"
    NEEDS_TIME = "needs_time"
    REFERRAL = "referral"
    AMBIGUOUS = "ambiguous"
    UNRECOGNIZED = "unrecognized"


@dataclass(frozen=True, slots=True)
class ExplicitPrice:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class QuoteDetails:
    price_unit: str | None = None
    pack_qty: Decimal | None = None
    moq: Decimal | None = None
    order_multiple: Decimal | None = None
    available_qty: Decimal | None = None


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


async def persist_supplier_followup_turn(
    connection: asyncpg.Connection,
    *,
    conversation_id: UUID,
    offer_id: UUID,
    message_id: UUID,
    draft_text: str,
    approval_id: UUID | None,
) -> bool:
    """Persist the exact next-item draft and optionally bind its exact approval."""

    async with connection.transaction():
        target_id = await connection.fetchval(
            """
            SELECT id FROM supplier_conversation_targets
            WHERE conversation_id=$1 AND offer_id=$2
            """,
            conversation_id,
            offer_id,
        )
        if target_id is None:
            return False
        await connection.execute(
            """
            UPDATE supplier_conversation_item_turns
            SET status='answered',inbound_message_id=COALESCE(inbound_message_id,$3),
                next_check_at=NULL,updated_at=now()
            WHERE conversation_id=$1 AND target_id=$2
              AND status IN ('pending_reply','needs_time','ambiguous')
            """,
            conversation_id,
            target_id,
            message_id,
        )
        turn_id = await connection.fetchval(
            """
            SELECT id FROM supplier_conversation_item_turns
            WHERE conversation_id=$1 AND target_id=$2 AND status='queued'
              AND draft_text=$3
            ORDER BY turn_no DESC LIMIT 1
            FOR UPDATE
            """,
            conversation_id,
            target_id,
            draft_text,
        )
        if turn_id is None:
            turn_no = await connection.fetchval(
                """
                SELECT COALESCE(max(turn_no),0)+1
                FROM supplier_conversation_item_turns WHERE conversation_id=$1
                """,
                conversation_id,
            )
            turn_id = await connection.fetchval(
                """
                INSERT INTO supplier_conversation_item_turns(
                    conversation_id,target_id,turn_no,status,draft_text
                ) VALUES ($1,$2,$3,'queued',$4)
                RETURNING id
                """,
                conversation_id,
                target_id,
                int(turn_no),
                draft_text,
            )
        if turn_id is None:
            raise RuntimeError("supplier follow-up turn could not be persisted")
        if approval_id is not None:
            updated = await connection.execute(
                """
                UPDATE supplier_conversation_item_turns
                SET status='awaiting_approval',approval_id=$2,updated_at=now()
                WHERE id=$1 AND status='queued'
                """,
                turn_id,
                approval_id,
            )
            if updated != "UPDATE 1":
                raise SupplierReplyProcessingConflict(
                    "supplier follow-up turn changed before approval binding"
                )
        return True


_UNAVAILABLE_PATTERNS = (
    re.compile(r"\bжоқ\b|\bбар\s+емес\b", re.IGNORECASE),
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
    re.compile(r"\bбар\b(?!\s+емес\b)", re.IGNORECASE),
    re.compile(r"\bесть\s+в\s+наличии\b", re.IGNORECASE),
    re.compile(r"\bимеется\s+в\s+наличии\b", re.IGNORECASE),
    re.compile(r"^\s*в\s+наличии\b", re.IGNORECASE),
)
_EXACT_UNAVAILABLE = re.compile(
    r"^\s*(?:нет|нету|отсутствует|закончился|закончилась|закончились|жоқ)[.!?\s]*$",
    re.IGNORECASE,
)
_CURRENCY_AMOUNT = re.compile(
    r"(?:(?P<prefix>[₸$€])\s*(?P<prefix_amount>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)"
    r"|(?P<suffix_amount>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*"
    r"(?P<suffix>тг\.?|тенге|теңге|kzt|usd|доллар(?:а|ов)?|eur|евро))",
    re.IGNORECASE,
)
_PRICE_WITHOUT_CURRENCY = re.compile(r"\b(?:цена|стоимость|по|бағасы)\s*[:—-]?\s*\d", re.IGNORECASE)
_SPEC_CONFIRMED = re.compile(
    r"\b(?:характеристики|параметры|требования|спецификация)\b.{0,80}"
    r"\b(?:соответствуют|совпадают|подходят|подтверждаем)\b"
    r"|\b(?:соответствует|подходит)\b.{0,80}"
    r"\b(?:характеристикам|параметрам|требованиям|спецификации)\b",
    re.IGNORECASE,
)
_SPEC_MISMATCH = re.compile(
    r"\bне\s+(?:соответствует|подходит|совпадает)\b.{0,80}"
    r"\b(?:требованиям|параметрам|характеристикам|спецификации)\b"
    r"|\b(?:характеристики|параметры|размер|мощность)\b.{0,80}"
    r"\b(?:другие|другая|другой|не совпадают)\b",
    re.IGNORECASE,
)
_NEEDS_TIME = re.compile(
    r"\b(?:уточню|проверю|подождите|перезвоню|отвечу позже|нужно уточнить)\b",
    re.IGNORECASE,
)
_REFERRAL = re.compile(
    r"\b(?:пишите|обратитесь|позвоните|свяжитесь|контакт|другой номер)\b",
    re.IGNORECASE,
)
_PHONE_CANDIDATE = re.compile(r"(?<!\d)(?:\+?7|8)[\d\s()\-]{9,20}\d(?!\d)")
_QUANTITY_NUMBER = r"\d+(?:[.,]\d{1,3})?"
_GOODS_UNIT = (
    r"шт(?:ук[аи]?)?|штук|штука|штуки|штуку|дана(?:сы)?|кг|килограмм(?:а|ов)?|"
    r"г|грамм(?:а|ов)?|м²|м2|кв\.?\s*м|метр(?:а|ов)?|м|"
    r"рулон(?:а|ов)?|л|литр(?:а|ов)?"
)
_AVAILABLE_QUANTITY = re.compile(
    rf"\b(?:есть(?:\s+в\s+наличии)?|осталось|в\s+наличии|можем\s+поставить)\s*"
    rf"(?P<amount>{_QUANTITY_NUMBER})\s*(?P<unit>{_GOODS_UNIT})\b",
    re.IGNORECASE,
)
_AVAILABLE_PACK_QUANTITY = re.compile(
    rf"\b(?:есть(?:\s+в\s+наличии)?|осталось|в\s+наличии|можем\s+поставить)\s*"
    rf"(?P<amount>{_QUANTITY_NUMBER})\s*"
    r"(?P<unit>упаков(?:ка|ки|ок)|пачк(?:а|и|ек)|коробк(?:а|и|ок))\b",
    re.IGNORECASE,
)
_PRICE_UNIT = re.compile(
    rf"(?:\bза\s+|/)\s*(?:1\s+)?(?P<unit>{_GOODS_UNIT}|"
    r"упаковку|упаковка|пачку|пачка|коробку|коробка)\b",
    re.IGNORECASE,
)
_PACK_QUANTITY = re.compile(
    rf"\b(?:в\s+упаковке|в\s+пачке|в\s+коробке|упаковка\s+по|пачка\s+по|коробка\s+по)\s*"
    rf"(?P<amount>{_QUANTITY_NUMBER})\s*(?P<unit>{_GOODS_UNIT})\b",
    re.IGNORECASE,
)
_MOQ = re.compile(
    rf"\b(?:минимальн(?:ая\s+партия|ый\s+заказ)|минимум\s+от)\s*"
    r"[:—-]?\s*"
    rf"(?P<amount>{_QUANTITY_NUMBER})\s*(?P<unit>{_GOODS_UNIT}|"
    r"упаковк(?:а|и|ок)|пачк(?:а|и|ек)|коробк(?:а|и|ок))\b",
    re.IGNORECASE,
)
_ORDER_MULTIPLE = re.compile(
    rf"\bкратно\s*(?P<amount>{_QUANTITY_NUMBER})\s*"
    rf"(?P<unit>{_GOODS_UNIT}|упаковкам|упаковок|пачкам|коробкам)?",
    re.IGNORECASE,
)
_CURRENCY_CODES = {
    "₸": "KZT",
    "тг": "KZT",
    "тг.": "KZT",
    "тенге": "KZT",
    "теңге": "KZT",
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
_PACKAGE_UNIT_GROUPS = (
    {"упаковка", "упаковку", "упаковки", "упаковок", "упаковкам"},
    {"пачка", "пачку", "пачки", "пачек", "пачкам"},
    {"коробка", "коробку", "коробки", "коробок", "коробкам"},
)
_ACTIVE_OFFER_STATUSES = ("lead", "confirm", "exact", "mismatch")
_PROCESS_EVENT = "supplier_reply_processed"


async def enqueue_favorable_quote_notification(
    connection: asyncpg.Connection,
    *,
    offer_id: UUID,
) -> bool:
    """Notify the case owner once for a qualified, current best supplier quote.

    Public listing prices and unverified/unquantified offers are never eligible.
    The snapshot version is the idempotency boundary, so a material quote
    revision can notify again while a retry of the same revision cannot.
    """

    selected = await connection.fetchrow(
        """
        SELECT o.id AS offer_id,o.status AS offer_status,o.supplier_id,
               s.display_name AS supplier_name,ri.id AS request_item_id,
               ri.name AS item_name,ri.quantity,ri.unit,
               financial.tender_unit_price_private,
               COALESCE(financial.currency,'KZT') AS tender_currency,
               owner.telegram_id,owner.active AS owner_active,
               current_quote.snapshot_id,current_quote.request_item_id AS quote_item_id,
               snapshot.snapshot AS quote_snapshot
        FROM offers AS o
        JOIN request_items AS ri ON ri.id=o.request_item_id
        JOIN procurement_cases AS procurement_case ON procurement_case.id=ri.case_id
        JOIN users AS owner ON owner.id=procurement_case.owner_user_id
        JOIN suppliers AS s ON s.id=o.supplier_id
        LEFT JOIN request_item_financials AS financial
          ON financial.request_item_id=ri.id
        LEFT JOIN quote_snapshot_current AS current_quote ON current_quote.offer_id=o.id
        LEFT JOIN quote_snapshot_versions AS snapshot
          ON snapshot.id=current_quote.snapshot_id
         AND snapshot.offer_id=o.id
         AND snapshot.request_item_id=ri.id
        WHERE o.id=$1 AND procurement_case.status NOT IN ('closed','cancelled')
        """,
        offer_id,
    )
    if (
        selected is None
        or selected["snapshot_id"] is None
        or selected["quote_item_id"] != selected["request_item_id"]
        or not selected["owner_active"]
        or selected["telegram_id"] is None
    ):
        return False

    quote_facts = _quote_facts(selected["quote_snapshot"])
    if quote_facts.get("price_amount", {}).get("source_type") != "supplier_message":
        return False
    item_id = selected["request_item_id"]
    case_id = await connection.fetchval("SELECT case_id FROM request_items WHERE id=$1", item_id)
    if case_id is None:
        return False
    requirements, reviews = await load_case_technical_reviews(connection, case_id)
    sibling_rows = await connection.fetch(
        """
        SELECT o.id AS offer_id,o.status AS offer_status,o.supplier_id,
               ri.id AS request_item_id,ri.quantity,ri.unit,
               financial.tender_unit_price_private,
               COALESCE(financial.currency,'KZT') AS tender_currency,
               current_quote.request_item_id AS quote_item_id,
               snapshot.snapshot AS quote_snapshot
        FROM offers AS o
        JOIN request_items AS ri ON ri.id=o.request_item_id
        LEFT JOIN request_item_financials AS financial
          ON financial.request_item_id=ri.id
        LEFT JOIN quote_snapshot_current AS current_quote ON current_quote.offer_id=o.id
        LEFT JOIN quote_snapshot_versions AS snapshot
          ON snapshot.id=current_quote.snapshot_id
         AND snapshot.offer_id=o.id
         AND snapshot.request_item_id=ri.id
        WHERE ri.id=$1 AND o.status<>'withdrawn'
        ORDER BY o.created_at,o.id
        """,
        item_id,
    )
    comparisons: list[dict[str, Any]] = []
    technical_inputs: list[dict[str, Any]] = []
    for row in sibling_rows:
        facts = _quote_facts(row["quote_snapshot"])
        item_matches = row["quote_item_id"] == row["request_item_id"]
        values = {
            "offer_id": str(row["offer_id"]),
            "request_item_id": str(row["request_item_id"]),
            "supplier_id": str(row["supplier_id"]),
            "offer_status": str(row["offer_status"]),
            "quantity": row["quantity"],
            "unit": row["unit"],
            "tender_unit_price_private": row["tender_unit_price_private"],
            "tender_currency": row["tender_currency"],
            "price_amount": _quote_value(facts, "price_amount") if item_matches else None,
            "currency": _quote_value(facts, "currency") if item_matches else None,
            "price_unit": _quote_value(facts, "price_unit") if item_matches else None,
            "pack_qty": _quote_value(facts, "pack_qty") if item_matches else None,
            "moq": _quote_value(facts, "moq") if item_matches else None,
            "order_multiple": _quote_value(facts, "order_multiple") if item_matches else None,
            "available_qty": _quote_value(facts, "available_qty") if item_matches else None,
            "availability_status": (
                _quote_value(facts, "availability_status") if item_matches else None
            ),
            "source_type": _quote_source(facts, "price_amount") if item_matches else "",
            "stock_source_type": (
                _quote_source(facts, "availability_status") if item_matches else ""
            ),
        }
        comparisons.append(values)
        technical_inputs.append(values)
    states = technical_states(technical_inputs, requirements, reviews)
    for value in comparisons:
        value["technical_state"] = states.get(value["offer_id"], "needs_confirmation")
    ranked = rank_goods_offers(comparisons)
    candidate = next((entry for entry in ranked if entry.offer_id == str(offer_id)), None)
    if candidate is None or not candidate.rankable:
        return False
    profitable = candidate.tender_difference is not None and candidate.tender_difference > 0
    new_best = candidate.rank == 1
    if not profitable and not new_best:
        return False

    text = _favorable_quote_text(
        item_name=str(selected["item_name"]),
        supplier_name=str(selected["supplier_name"]),
        comparison=candidate,
        profitable=profitable,
    )
    outbox_id = await enqueue_outbox(
        connection,
        event_type="telegram.send_message",
        payload={"chat_id": int(selected["telegram_id"]), "text": text},
        idempotency_key=stable_key("favorable-supplier-quote", selected["snapshot_id"]),
    )
    return outbox_id is not None


def _quote_facts(snapshot: Any) -> dict[str, dict[str, Any]]:
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            return {}
    if not isinstance(snapshot, Mapping):
        return {}
    facts = snapshot.get("facts")
    if not isinstance(facts, Mapping):
        return {}
    return {str(key): dict(value) for key, value in facts.items() if isinstance(value, Mapping)}


def _quote_value(facts: Mapping[str, Mapping[str, Any]], field: str) -> Any:
    return facts.get(field, {}).get("value")


def _quote_source(facts: Mapping[str, Mapping[str, Any]], field: str) -> str:
    return str(facts.get(field, {}).get("source_type") or "")


def _favorable_quote_text(
    *,
    item_name: str,
    supplier_name: str,
    comparison: GoodsComparison,
    profitable: bool,
) -> str:
    total = comparison.goods_total
    currency = comparison.currency or ""
    difference = comparison.tender_difference
    if profitable and difference is not None:
        detail = f"ниже тендерного ориентира на {_display_decimal(difference)} {currency}"
    elif difference is not None and difference < 0:
        detail = (
            "лучшее сопоставимое предложение, но выше ориентира на "
            f"{_display_decimal(-difference)} {currency}"
        )
    else:
        detail = "лучшее сопоставимое предложение на уровне тендерного ориентира"
    return (
        f"Подтверждённое предложение по позиции «{item_name}»: {supplier_name}. "
        f"Сумма на нужное количество — {_display_decimal(total)} {currency}; {detail}. "
        "Цена, единица, наличие и характеристики сопоставимы по подтверждённым данным."
    )


def _display_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


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
    if _REFERRAL.search(normalized) and _PHONE_CANDIDATE.search(normalized):
        return ClassifiedSupplierReply(
            ReplyClassification.REFERRAL,
            signals=("referral",),
            reason="supplier supplied another contact",
        )
    if _NEEDS_TIME.search(normalized):
        return ClassifiedSupplierReply(
            ReplyClassification.NEEDS_TIME,
            signals=("needs_time",),
            reason="supplier asked for time to clarify",
        )
    if _SPEC_MISMATCH.search(normalized):
        return ClassifiedSupplierReply(
            ReplyClassification.SPEC_MISMATCH,
            signals=("spec_mismatch",),
            reason="supplier reported a specification mismatch",
        )
    if _SPEC_CONFIRMED.search(normalized):
        return ClassifiedSupplierReply(
            ReplyClassification.SPEC_CONFIRMED,
            signals=("spec_confirmed",),
            reason="supplier explicitly confirmed specification",
        )

    unavailable = bool(_EXACT_UNAVAILABLE.fullmatch(normalized)) or any(
        pattern.search(normalized) for pattern in _UNAVAILABLE_PATTERNS
    )
    available = any(pattern.search(normalized) for pattern in _AVAILABLE_PATTERNS) or bool(
        _AVAILABLE_QUANTITY.search(normalized) or _AVAILABLE_PACK_QUANTITY.search(normalized)
    )
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
        "out_of_stock" if unavailable else "in_stock_unquantified" if available else "unknown"
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
    semantic_snapshot: SemanticReplySnapshot | None = None,
    semantic_proposal: ValidatedSemanticReplyProposal | None = None,
    semantic_proposal_error: str | None = None,
) -> SupplierReplyProcessingResult:
    """Project one immutable inbound reply without sending an outbound message.

    Semantic proposals are optional. Callers must obtain and validate them from
    a frozen supplier-safe snapshot outside this transaction; this function
    re-reads the snapshot under the commit transaction before applying facts.
    """

    if not isinstance(conversation_id, UUID) or not isinstance(message_id, UUID):
        raise TypeError("conversation_id and message_id must be UUID values")
    if semantic_snapshot is None and (
        semantic_proposal is not None or semantic_proposal_error is not None
    ):
        raise ValueError("semantic proposal or failure requires its input snapshot")
    if semantic_proposal is not None and semantic_proposal_error is not None:
        raise ValueError("semantic proposal and failure reason are mutually exclusive")
    if semantic_snapshot is not None and (
        semantic_snapshot.conversation_id != conversation_id
        or semantic_snapshot.message_id != message_id
        or (
            semantic_proposal is not None
            and semantic_proposal.context_sha256 != semantic_snapshot.context_sha256
        )
    ):
        raise SemanticReplyValidationError("semantic proposal does not match its input snapshot")
    event_key = stable_key("supplier-reply-processed", message_id)
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", event_key)
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
                m.text_content,m.external_message_id,m.received_at,m.created_at,
                c.case_id,c.supplier_id,c.state,c.opted_out,c.session_name,
                c.preferred_language,
                c.external_chat_id,procurement_case.status AS case_status,
                media.kind AS media_kind,media.state AS media_state,
                media.extracted_text AS media_extracted_text,
                media.needs_clarification AS media_needs_clarification,
                media.language AS media_language,media.sha256 AS media_sha256
            FROM supplier_conversation_messages AS m
            JOIN supplier_conversations AS c ON c.id=m.conversation_id
            JOIN procurement_cases AS procurement_case ON procurement_case.id=c.case_id
            LEFT JOIN supplier_message_media media ON media.message_id=m.id
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
        row = dict(row)
        original_message_text = str(row["text_content"] or "")
        row["text_content"] = supplier_evidence_text(original_message_text, row)
        if row.get("media_kind") and row.get("media_state") not in {"ready", "failed"}:
            raise SupplierReplyOrderPending("supplier media is still being processed")
        if row["case_status"] in {"closed", "cancelled"}:
            raise SupplierReplyProcessingConflict(
                "supplier reply targets a closed or cancelled case"
            )
        try:
            actor = await acquire_chat_actor_in_transaction(
                connection,
                session_name=row["session_name"],
                external_chat_id=row["external_chat_id"],
                worker_id=f"supplier-reply:{message_id}",
            )
        except ChatActorBusy as exc:
            raise SupplierReplyOrderPending(
                "physical WhatsApp chat is busy with another event"
            ) from exc

        # Queue workers may claim adjacent replies concurrently. The
        # conversation row lock above serializes their projections; this gate
        # keeps a later event from winning that lock before an earlier reply.
        preceding = await connection.fetch(
            """
            SELECT id FROM supplier_conversation_messages
            WHERE conversation_id=$1 AND direction='inbound'
              AND (received_at,created_at,id)<($2,$3,$4)
            ORDER BY received_at,created_at,id
            """,
            conversation_id,
            row["received_at"],
            row["created_at"],
            message_id,
        )
        for earlier in preceding:
            earlier_key = stable_key("supplier-reply-processed", earlier["id"])
            processed = await connection.fetchval(
                "SELECT 1 FROM workflow_events WHERE idempotency_key=$1",
                earlier_key,
            )
            if processed is None:
                raise SupplierReplyOrderPending("an earlier supplier reply must be projected first")

        previous_state = SupplierDialogueState(row["state"])
        classified = classify_supplier_reply(row["text_content"])
        language = detect_supplier_language(
            row["text_content"],
            fallback=SupplierLanguage(row["preferred_language"]),
        )
        if row.get("media_language") in {"ru", "kk"}:
            language = SupplierLanguage(row["media_language"])
        if language.value != row["preferred_language"]:
            await connection.execute(
                "UPDATE supplier_conversations SET preferred_language=$2,updated_at=now() "
                "WHERE id=$1",
                conversation_id, language.value,
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
            """,
            conversation_id,
            row["case_id"],
            row["supplier_id"],
            list(_ACTIVE_OFFER_STATUSES),
        )
        if semantic_snapshot is not None:
            current_snapshot = await load_semantic_reply_snapshot(
                connection,
                conversation_id=conversation_id,
                message_id=message_id,
            )
            try:
                assert_snapshot_current(semantic_snapshot, current_snapshot)
            except SemanticReplyValidationError as exc:
                raise SupplierReplyProcessingConflict("semantic reply snapshot is stale") from exc
            if semantic_snapshot.message_text != row["text_content"]:
                raise SupplierReplyProcessingConflict(
                    "semantic snapshot message differs from inbound"
                )
            if semantic_proposal_error is not None:
                candidate = None
                classified = ClassifiedSupplierReply(
                    ReplyClassification.AMBIGUOUS,
                    reason=semantic_proposal_error[:500],
                )
            elif semantic_proposal is not None:
                candidate = next(
                    (
                        value
                        for value in candidates
                        if value["offer_id"] == semantic_proposal.target_offer_id
                        and value["request_item_id"] == semantic_proposal.target_item_id
                    ),
                    None,
                )
                if semantic_proposal.target_offer_id is not None and candidate is None:
                    raise SupplierReplyProcessingConflict(
                        "semantic proposal target is no longer active"
                    )
                if semantic_proposal.classification == "opt_out":
                    raise SupplierReplyProcessingConflict(
                        "opt-out must be synchronously projected by inbound handling"
                    )
                classified = _classification_from_semantic(
                    semantic_proposal,
                )
            else:
                raise ValueError("semantic snapshot needs a proposal or validation failure")
            candidate_count = len(candidates)
        else:
            candidate = candidates[0] if len(candidates) == 1 else None
            candidate_count = len(candidates)
        outcome = _decide(
            state=previous_state,
            classified=classified,
            candidate=candidate,
            candidate_count=candidate_count,
            already_opted_out=bool(row["opted_out"]),
            language=language,
        )

        clarification = media_clarification(
            row, language=row.get("media_language") or language.value,
        )
        if clarification and not row["opted_out"]:
            classified = ClassifiedSupplierReply(
                ReplyClassification.AMBIGUOUS, reason="supplier media requires confirmation",
            )
            if previous_state not in {
                SupplierDialogueState.COMPLETE, SupplierDialogueState.CLOSED_NO_REPLY,
            }:
                if candidate is None and len(candidates) == 1:
                    candidate = candidates[0]
                if candidate is None and semantic_snapshot and semantic_snapshot.pending_turn:
                    candidate = next(
                        (value for value in candidates if value["offer_id"]
                         == semantic_snapshot.pending_turn.offer_id), None,
                    )
                outcome = _Outcome(previous_state, False,
                                   "supplier media requires text confirmation", clarification)
            else:
                outcome = _Outcome(previous_state, True, "late media requires text confirmation")

        if semantic_snapshot is not None:
            if semantic_snapshot.pending_turn is not None:
                turn_status = _turn_status_for_reply(classified.classification)
                updated = await connection.execute(
                    """
                    UPDATE supplier_conversation_item_turns
                    SET status=$3,inbound_message_id=$4,reason=$5,
                        next_check_at=CASE WHEN $3='needs_time'
                            THEN now()+interval '24 hours' ELSE NULL END,
                        updated_at=now()
                    WHERE id=$1 AND conversation_id=$2
                      AND status IN ('pending_reply','needs_time','ambiguous')
                    """,
                    semantic_snapshot.pending_turn.id,
                    conversation_id,
                    turn_status,
                    message_id,
                    outcome.reason[:500],
                )
                if updated != "UPDATE 1":
                    raise SupplierReplyProcessingConflict(
                        "pending semantic item turn changed before reply commit"
                    )
                if outcome.outbound_text:
                    next_turn_no = await connection.fetchval(
                        """
                        SELECT COALESCE(max(turn_no),0)+1
                        FROM supplier_conversation_item_turns
                        WHERE conversation_id=$1
                        """,
                        conversation_id,
                    )
                    await connection.execute(
                        """
                        INSERT INTO supplier_conversation_item_turns(
                            conversation_id,target_id,turn_no,status,draft_text
                        ) VALUES ($1,$2,$3,'queued',$4)
                        """,
                        conversation_id,
                        semantic_snapshot.pending_turn.target_id,
                        int(next_turn_no),
                        outcome.outbound_text,
                    )
                if outcome.state in {
                    SupplierDialogueState.COMPLETE,
                    SupplierDialogueState.CLOSED_NO_REPLY,
                } and previous_state not in {
                    SupplierDialogueState.CLOSED_NO_REPLY,
                    SupplierDialogueState.COMPLETE,
                }:
                    queued_turn = await connection.fetchval(
                        """
                        SELECT id FROM supplier_conversation_item_turns
                        WHERE conversation_id=$1 AND status='queued'
                        ORDER BY turn_no LIMIT 1
                        """,
                        conversation_id,
                    )
                    if queued_turn is not None:
                        outcome = _Outcome(
                            SupplierDialogueState.ESCALATED,
                            True,
                            "This item reply is saved; another item turn is queued for approval.",
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
                "original_text": original_message_text,
                "media_sha256": row.get("media_sha256"),
                "classification": classified.classification.value,
                "signals": list(classified.signals),
            }
            if semantic_proposal is not None:
                evidence_payload["semantic_proposal"] = {
                    "context_sha256": semantic_proposal.context_sha256,
                    "classification": semantic_proposal.classification,
                    "classification_quote": semantic_proposal.classification_quote,
                    "reason": semantic_proposal.reason,
                    "facts": [
                        {
                            "offer_id": str(fact.offer_id),
                            "request_item_id": str(fact.request_item_id),
                            "field": fact.field,
                            "value": fact.value,
                            "evidence_quote": fact.evidence_quote,
                        }
                        for fact in semantic_proposal.facts
                    ],
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
                    content_sha256=hashlib.sha256(row["text_content"].encode("utf-8")).hexdigest(),
                    raw_payload=evidence_payload,
                    idempotency_key=stable_key("supplier-message-evidence", message_id),
                ),
            )
            evidence_id = evidence.id
            if classified.classification == ReplyClassification.REFERRAL:
                for match in list(_PHONE_CANDIDATE.finditer(row["text_content"]))[:10]:
                    try:
                        referred_phone = normalize_phone(match.group())
                    except ValueError:
                        continue
                    await connection.execute(
                        """INSERT INTO supplier_contact_referrals(
                               conversation_id,message_id,source_evidence_id,normalized_phone
                           ) VALUES ($1,$2,$3,$4)
                           ON CONFLICT (message_id,normalized_phone) DO NOTHING""",
                        conversation_id,
                        message_id,
                        evidence_id,
                        referred_phone,
                    )
            if candidate is not None and semantic_proposal is not None:
                details = _semantic_quote_details(
                    semantic_proposal,
                    item_unit=str(candidate["unit"]),
                )
                # In semantic mode, only a price parsed and validated against the
                # selected target's own literal evidence quote may be projected.
                price = classified.price
            else:
                details = (
                    extract_quote_details(row["text_content"], item_unit=str(candidate["unit"]))
                    if candidate is not None
                    else QuoteDetails()
                )
                explicit_prices = _extract_explicit_prices(row["text_content"])
                price = (
                    classified.price or explicit_prices[0]
                    if len(explicit_prices) == 1
                    and classified.classification
                    not in {
                        ReplyClassification.AMBIGUOUS,
                        ReplyClassification.OPT_OUT,
                        ReplyClassification.REFERRAL,
                    }
                    else None
                )
            availability_status = classified.availability_status
            if details.available_qty is not None and availability_status != "out_of_stock":
                availability_status = "in_stock" if details.available_qty > 0 else "out_of_stock"
            if candidate is not None and (
                classified.classification
                in {
                    ReplyClassification.UNAVAILABLE,
                    ReplyClassification.AVAILABLE,
                    ReplyClassification.PRICE_RECEIVED,
                }
                or price is not None
                or details.available_qty is not None
            ):
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
                        price_unit=details.price_unit,
                        pack_qty=details.pack_qty,
                        moq=details.moq,
                        order_multiple=details.order_multiple,
                        available_qty=details.available_qty,
                        availability_status=availability_status,
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
        if outcome.state in {
            SupplierDialogueState.COMPLETE,
            SupplierDialogueState.CLOSED_NO_REPLY,
            SupplierDialogueState.OPTED_OUT,
        }:
            await connection.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended('supplier-dialogue-active-groups',0)
                )
                """
            )
            await connection.execute(
                """
                UPDATE supplier_dialogue_assignments
                SET status='complete',updated_at=now()
                WHERE conversation_id=$1 AND status IN ('assigned','waiting_reply')
                """,
                conversation_id,
            )

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
        await release_chat_actor_in_transaction(connection, actor)
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
        SupplierDialogueState.CLOSED_NO_REPLY,
        SupplierDialogueState.COMPLETE,
    }:
        if candidate is None:
            return _Outcome(
                state,
                True,
                "Late supplier reply is saved but could not be bound to one exact item.",
            )
        if classified.classification in {
            ReplyClassification.UNAVAILABLE,
            ReplyClassification.AVAILABLE,
            ReplyClassification.PRICE_RECEIVED,
            ReplyClassification.SPEC_CONFIRMED,
            ReplyClassification.SPEC_MISMATCH,
        }:
            return _Outcome(
                state,
                True,
                "Late supplier reply was saved against its exact offer; "
                "conversation remains terminal.",
            )
        return _Outcome(state, True, classified.reason or "Late supplier reply needs review.")
    if state in {
        SupplierDialogueState.COMPLETE,
    }:
        raise SupplierReplyProcessingConflict("reply targets a terminal conversation")
    if candidate is None:
        reason = (
            classified.reason
            if classified.classification
            in {ReplyClassification.AMBIGUOUS, ReplyClassification.UNRECOGNIZED}
            and classified.reason
            else "no active offer is uniquely linked to the conversation"
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
    if classified.classification == ReplyClassification.UNAVAILABLE and state in {
        SupplierDialogueState.WAITING_AVAILABILITY,
        SupplierDialogueState.WAITING_SPEC,
        SupplierDialogueState.WAITING_PRICE,
        SupplierDialogueState.ESCALATED,
    }:
        reply = SupplierReply.UNAVAILABLE
    elif (
        state == SupplierDialogueState.WAITING_AVAILABILITY
        and classified.classification == ReplyClassification.AVAILABLE
    ):
        reply = SupplierReply.AVAILABLE
    elif (
        state == SupplierDialogueState.WAITING_SPEC
        and classified.classification == ReplyClassification.SPEC_CONFIRMED
    ):
        reply = SupplierReply.SPEC_CONFIRMED
    elif classified.classification == ReplyClassification.SPEC_MISMATCH and state in {
        SupplierDialogueState.WAITING_SPEC,
        SupplierDialogueState.WAITING_PRICE,
    }:
        reply = SupplierReply.SPEC_MISMATCH
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


def _classification_from_semantic(
    proposal: ValidatedSemanticReplyProposal,
) -> ClassifiedSupplierReply:
    classification = ReplyClassification(proposal.classification)
    facts = {(fact.field): fact for fact in proposal.facts}
    availability = (
        "out_of_stock"
        if classification == ReplyClassification.UNAVAILABLE
        else "in_stock_unquantified"
        if classification == ReplyClassification.AVAILABLE
        else "unknown"
    )
    price: ExplicitPrice | None = None
    price_fact = facts.get("price")
    currency_fact = facts.get("currency")
    if price_fact is not None and currency_fact is not None:
        try:
            proposed_amount = Decimal(
                price_fact.value.replace(" ", "").replace("\u00a0", "").replace(",", ".")
            )
            proposed_currency = _CURRENCY_CODES[currency_fact.value.casefold()]
        except (InvalidOperation, KeyError):
            proposed_amount = Decimal("-1")
            proposed_currency = ""
        # Scope currency/amount validation to the selected target's exact quote.
        # Other item prices elsewhere in the same physical chat are irrelevant.
        literal_prices = _extract_explicit_prices(price_fact.evidence_quote)
        if (
            proposed_amount >= 0
            and len(literal_prices) == 1
            and literal_prices[0] == ExplicitPrice(proposed_amount, proposed_currency)
            and price_fact.evidence_quote == currency_fact.evidence_quote
        ):
            price = ExplicitPrice(proposed_amount, proposed_currency)
    return ClassifiedSupplierReply(
        classification=classification,
        availability_status=availability,
        price=price,
        signals=(classification.value,),
        reason=proposal.reason or f"semantic proposal: {classification.value}",
    )


def _semantic_quote_details(
    proposal: ValidatedSemanticReplyProposal,
    *,
    item_unit: str,
) -> QuoteDetails:
    """Project only explicit, validated quantities with a matching literal unit."""

    facts = {fact.field: fact for fact in proposal.facts}
    normalized_item_unit = normalize_goods_unit(item_unit)

    def amount(field: str) -> Decimal | None:
        fact = facts.get(field)
        if fact is None:
            return None
        try:
            value = Decimal(fact.value.replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except InvalidOperation:
            return None
        return value if value.is_finite() and value >= 0 else None

    price_unit_fact = facts.get("price_unit")
    price_unit = (
        normalize_goods_unit(price_unit_fact.value) if price_unit_fact is not None else None
    )
    pack_unit_fact = facts.get("pack_unit")
    pack_qty = amount("pack_qty")
    if pack_unit_fact is None or normalize_goods_unit(pack_unit_fact.value) != normalized_item_unit:
        pack_qty = None
    stock_unit_fact = facts.get("stock_unit")
    available_qty = amount("stock")
    if (
        stock_unit_fact is None
        or normalize_goods_unit(stock_unit_fact.value) != normalized_item_unit
    ):
        available_qty = None
    return QuoteDetails(
        price_unit=price_unit,
        pack_qty=pack_qty,
        available_qty=available_qty,
    )


def _turn_status_for_reply(classification: ReplyClassification) -> str:
    return {
        ReplyClassification.NEEDS_TIME: "needs_time",
        ReplyClassification.REFERRAL: "referred",
        ReplyClassification.UNAVAILABLE: "unavailable",
        ReplyClassification.AMBIGUOUS: "ambiguous",
        ReplyClassification.UNRECOGNIZED: "ambiguous",
    }.get(classification, "answered")


def _extract_explicit_prices(text: str) -> tuple[ExplicitPrice, ...]:
    found: list[tuple[Decimal, str]] = []
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
            found.append((amount, currency))
    return tuple(
        ExplicitPrice(amount=amount, currency=currency)
        for amount, currency in sorted(found, key=lambda value: (value[1], value[0]))
    )


def extract_quote_details(text: str, *, item_unit: str) -> QuoteDetails:
    """Keep only quantities explicitly stated in the tender item's unit."""

    required_unit = normalize_goods_unit(item_unit)

    def amount(match: re.Match[str] | None) -> Decimal | None:
        if match is None:
            return None
        try:
            return Decimal(match.group("amount").replace(",", "."))
        except InvalidOperation:
            return None

    price_match = _PRICE_UNIT.search(text)
    price_unit = normalize_goods_unit(price_match.group("unit")) if price_match else None
    pack_match = _PACK_QUANTITY.search(text)
    pack_qty = (
        amount(pack_match)
        if pack_match and normalize_goods_unit(pack_match.group("unit")) == required_unit
        else None
    )
    stock_match = _AVAILABLE_QUANTITY.search(text)
    available_qty = (
        amount(stock_match)
        if stock_match and normalize_goods_unit(stock_match.group("unit")) == required_unit
        else None
    )
    moq_match = _MOQ.search(text)
    moq_unit = normalize_goods_unit(moq_match.group("unit")) if moq_match else None
    moq = (
        amount(moq_match)
        if moq_unit and price_unit and _same_quote_unit(moq_unit, price_unit)
        else None
    )
    multiple_match = _ORDER_MULTIPLE.search(text)
    multiple_unit = (
        normalize_goods_unit(multiple_match.group("unit"))
        if multiple_match and multiple_match.group("unit")
        else None
    )
    order_multiple = (
        amount(multiple_match)
        if multiple_match
        and multiple_unit
        and price_unit
        and _same_quote_unit(multiple_unit, price_unit)
        else None
    )
    return QuoteDetails(
        price_unit=price_unit,
        pack_qty=pack_qty,
        moq=moq,
        order_multiple=order_multiple,
        available_qty=available_qty,
    )


def _same_quote_unit(left: str, right: str) -> bool:
    if left == right:
        return True
    return any(left in group and right in group for group in _PACKAGE_UNIT_GROUPS)


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
