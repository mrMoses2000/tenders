from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.approvals import require_and_consume_approval
from procurement_bot.errors import ValidationBlocked
from procurement_bot.queue import enqueue_outbox, stable_key


class SupplierDialogueState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    WAITING_AVAILABILITY = "waiting_availability"
    WAITING_SPEC = "waiting_spec"
    WAITING_PRICE = "waiting_price"
    COMPLETE = "complete"
    ESCALATED = "escalated"
    CLOSED_NO_REPLY = "closed_no_reply"
    OPTED_OUT = "opted_out"


class SupplierLanguage(StrEnum):
    RUSSIAN = "ru"
    KAZAKH = "kk"


class SupplierReply(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    SPEC_CONFIRMED = "spec_confirmed"
    SPEC_MISMATCH = "spec_mismatch"
    PRICE_RECEIVED = "price_received"
    NO_REPLY = "no_reply"
    OPT_OUT = "opt_out"


@dataclass(frozen=True)
class SupplierInquiry:
    product_name: str
    quantity: str
    required_spec: str


@dataclass(frozen=True)
class DialogueDecision:
    state: SupplierDialogueState
    outbound_text: str | None = None
    needs_human: bool = False
    reason: str = ""


@dataclass(frozen=True)
class SendContext:
    enabled: bool
    opted_out: bool = False
    messages_sent_today: int = 0
    messages_sent_in_conversation: int = 0
    followups_sent: int = 0
    last_sent_at: datetime | None = None


@dataclass(frozen=True)
class SendPolicy:
    timezone: str = "Asia/Almaty"
    quiet_start: time = time(20, 0)
    quiet_end: time = time(9, 0)
    daily_limit: int = 10
    conversation_limit: int = 4
    followup_limit: int = 1
    minimum_interval: timedelta = timedelta(minutes=2)


DEFAULT_SEND_POLICY = SendPolicy()


@dataclass(frozen=True)
class WhatsAppSendCommand:
    case_id: UUID
    recipient: str
    text: str
    conversation_id: UUID
    logical_action_id: str
    is_followup: bool = False

    def approval_payload(self) -> dict[str, Any]:
        return {
            "case_id": str(self.case_id),
            "channel": "whatsapp",
            "recipient": self.recipient,
            "text": self.text,
            "conversation_id": str(self.conversation_id),
            "logical_action_id": self.logical_action_id,
            "is_followup": self.is_followup,
        }


_PRIVATE_PRICE_PATTERNS = (
    re.compile(r"\bбюджет\w*\b", re.IGNORECASE),
    re.compile(r"\bнаш(?:\s+максимальн\w*)?\s+бюджет\b", re.IGNORECASE),
    re.compile(r"\bтендерн\w*\s+цен\w*\b", re.IGNORECASE),
    re.compile(r"\b(?:максимальн|предельн|закупочн)\w*\s+цен\w*\b", re.IGNORECASE),
    re.compile(r"\b(?:наш\w*\s+)?лимит\w*\s*[:—-]?\s*\d", re.IGNORECASE),
    re.compile(r"\bзаложен[а-я]*\s+(?:цен[а-я]*|бюджет)\b", re.IGNORECASE),
    re.compile(r"\binternal\s+(?:budget|limit|(?:target\s+)?price)\b", re.IGNORECASE),
    re.compile(r"\bour\s+(?:maximum\s+)?budget\b", re.IGNORECASE),
    re.compile(r"\binternal\s+(?:target\s+)?price\b", re.IGNORECASE),
)
_OPT_OUT_PATTERNS = (
    re.compile(r"\bне\s+(?:пишите|беспокойте|звоните)\b", re.IGNORECASE),
    re.compile(r"\b(?:стоп|отпис(?:ка|аться)|удалите\s+(?:мой\s+)?номер)\b", re.IGNORECASE),
    re.compile(r"\b(?:stop|unsubscribe|do\s+not\s+(?:contact|message))\b", re.IGNORECASE),
)


def contains_private_price(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PRIVATE_PRICE_PATTERNS)


def assert_supplier_safe_text(text: str) -> None:
    if not text.strip():
        raise ValidationBlocked("supplier message must be non-empty")
    if contains_private_price(text):
        raise ValidationBlocked("supplier message may not reveal budget or tender price")


def is_opt_out(text: str) -> bool:
    return any(pattern.search(text) for pattern in _OPT_OUT_PATTERNS)


def detect_supplier_language(
    text: str,
    *,
    fallback: SupplierLanguage | str = SupplierLanguage.RUSSIAN,
) -> SupplierLanguage:
    """Use only distinctive Kazakh letters; otherwise preserve the stored preference."""

    fallback_language = SupplierLanguage(fallback)
    if re.search(r"[әғқңөұүһі]", text, re.IGNORECASE):
        return SupplierLanguage.KAZAKH
    return fallback_language


def build_availability_inquiry(
    inquiry: SupplierInquiry,
    language: SupplierLanguage | str = SupplierLanguage.RUSSIAN,
) -> str:
    if SupplierLanguage(language) == SupplierLanguage.KAZAKH:
        message = (
            f"Сәлеметсіз бе! Мына тауар қолда бар ма: {inquiry.product_name}, "
            f"саны — {inquiry.quantity}. Талаптар: {inquiry.required_spec}. "
            "Қолда барын және көрсетілген талаптарға сәйкестігін растаңыз."
        )
    else:
        message = (
            f"Здравствуйте! Подскажите, пожалуйста, есть ли в наличии: {inquiry.product_name}, "
            f"количество — {inquiry.quantity}. Требования: {inquiry.required_spec}. "
            "Подтвердите наличие и соответствие указанным параметрам."
        )
    assert_supplier_safe_text(message)
    return message


def build_specification_followup(
    inquiry: SupplierInquiry,
    language: SupplierLanguage | str = SupplierLanguage.RUSSIAN,
) -> str:
    if SupplierLanguage(language) == SupplierLanguage.KAZAKH:
        message = (
            f"Рақмет. {inquiry.product_name} сипаттамаларын растаңыз: "
            f"{inquiry.required_spec}. Мүмкін болса, моделін/артикулын және таңбалау "
            "фотосын жіберіңіз."
        )
    else:
        message = (
            f"Спасибо. Подтвердите, пожалуйста, характеристики для {inquiry.product_name}: "
            f"{inquiry.required_spec}. Если возможно, пришлите модель/артикул и фото маркировки."
        )
    assert_supplier_safe_text(message)
    return message


def build_price_inquiry(
    inquiry: SupplierInquiry,
    language: SupplierLanguage | str = SupplierLanguage.RUSSIAN,
) -> str:
    if SupplierLanguage(language) == SupplierLanguage.KAZAKH:
        message = (
            f"Растағаныңызға рақмет. {inquiry.product_name} тауарының {inquiry.quantity} "
            "санына ең жақсы бағаңызды, ҚҚС-пен немесе ҚҚС-сыз екенін және алып кету/"
            "жеткізу шарттарын жазыңыз."
        )
    else:
        message = (
            f"Спасибо за подтверждение. Подскажите вашу лучшую цену на {inquiry.quantity} "
            f"для {inquiry.product_name}, с НДС или без, а также условия самовывоза/доставки."
        )
    assert_supplier_safe_text(message)
    return message


def advance_dialogue(
    state: SupplierDialogueState,
    reply: SupplierReply,
    inquiry: SupplierInquiry,
    language: SupplierLanguage | str = SupplierLanguage.RUSSIAN,
) -> DialogueDecision:
    """Advance the deterministic availability -> specification -> price flow."""

    if reply == SupplierReply.OPT_OUT:
        return DialogueDecision(SupplierDialogueState.OPTED_OUT, reason="supplier opted out")
    if reply == SupplierReply.UNAVAILABLE:
        return DialogueDecision(SupplierDialogueState.COMPLETE, reason="unavailable")
    if reply == SupplierReply.SPEC_MISMATCH:
        return DialogueDecision(
            SupplierDialogueState.ESCALATED,
            needs_human=True,
            reason="mandatory specification mismatch",
        )
    if state == SupplierDialogueState.WAITING_AVAILABILITY and reply == SupplierReply.AVAILABLE:
        return DialogueDecision(
            SupplierDialogueState.WAITING_SPEC,
            build_specification_followup(inquiry, language),
        )
    if state == SupplierDialogueState.WAITING_SPEC and reply == SupplierReply.SPEC_CONFIRMED:
        return DialogueDecision(
            SupplierDialogueState.WAITING_PRICE,
            build_price_inquiry(inquiry, language),
        )
    if state == SupplierDialogueState.WAITING_PRICE and reply == SupplierReply.PRICE_RECEIVED:
        return DialogueDecision(SupplierDialogueState.COMPLETE, reason="offer captured")
    if reply == SupplierReply.NO_REPLY:
        return DialogueDecision(state, reason="follow-up requires send-policy check")
    raise ValidationBlocked(f"reply {reply} is invalid while dialogue is {state}")


def _inside_quiet_hours(local: time, *, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= local < end
    return local >= start or local < end


def assert_send_allowed(
    context: SendContext,
    *,
    now: datetime,
    policy: SendPolicy = DEFAULT_SEND_POLICY,
    is_followup: bool = False,
) -> None:
    if context.opted_out:
        raise ValidationBlocked("supplier opted out")
    if not context.enabled:
        raise ValidationBlocked("WhatsApp sending is disabled")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if context.messages_sent_today >= policy.daily_limit:
        raise ValidationBlocked("daily WhatsApp limit reached")
    if context.messages_sent_in_conversation >= policy.conversation_limit:
        raise ValidationBlocked("conversation message limit reached")
    if is_followup and context.followups_sent >= policy.followup_limit:
        raise ValidationBlocked("follow-up limit reached")
    local = now.astimezone(ZoneInfo(policy.timezone)).timetz().replace(tzinfo=None)
    if _inside_quiet_hours(local, start=policy.quiet_start, end=policy.quiet_end):
        raise ValidationBlocked("supplier quiet hours are active")
    if context.last_sent_at is not None:
        if context.last_sent_at.tzinfo is None:
            raise ValueError("last_sent_at must be timezone-aware")
        if now - context.last_sent_at < policy.minimum_interval:
            raise ValidationBlocked("minimum WhatsApp interval has not elapsed")


async def enqueue_approved_whatsapp_send(
    pool: asyncpg.Pool,
    *,
    command: WhatsAppSendCommand,
    context: SendContext,
    now: datetime,
    policy: SendPolicy = DEFAULT_SEND_POLICY,
) -> UUID:
    """Consume the exact grant and enqueue the send in one DB transaction."""

    assert_supplier_safe_text(command.text)
    assert_send_allowed(
        context,
        now=now,
        policy=policy,
        is_followup=command.is_followup,
    )
    payload = command.approval_payload()
    idempotency_key = stable_key(
        "whatsapp-send", command.conversation_id, command.logical_action_id
    )
    async with pool.acquire() as connection, connection.transaction():
        # Serialize retries for the same logical action. A caller may lose the
        # response after commit and retry; that must return the existing event
        # without demanding a second approval.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            idempotency_key,
        )
        existing = await connection.fetchval(
            "SELECT id FROM outbox_events WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            status = await connection.fetchval(
                "SELECT status FROM outbox_events WHERE id=$1",
                existing,
            )
            if status == "dead":
                raise ValidationBlocked(
                    "dead WhatsApp event requires an operator redrive with a new logical action"
                )
            return existing
        approval = await require_and_consume_approval(
            connection,
            case_id=command.case_id,
            action_type="send_whatsapp",
            payload=payload,
        )
        event_id = await enqueue_outbox(
            connection,
            event_type="whatsapp.send_text",
            payload=payload,
            idempotency_key=idempotency_key,
            max_attempts=3,
            approval_id=approval.id,
        )
        if event_id is None:
            existing_after_conflict = await connection.fetchval(
                "SELECT id FROM outbox_events WHERE idempotency_key=$1",
                idempotency_key,
            )
            if existing_after_conflict is None:
                raise RuntimeError("approved WhatsApp outbox event was not created")
            return existing_after_conflict
        return event_id
