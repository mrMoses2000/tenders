from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from procurement_bot.errors import ValidationBlocked
from procurement_bot.supplier_dialogue import (
    DEFAULT_SEND_POLICY,
    SendContext,
    SendPolicy,
    SupplierDialogueState,
    SupplierInquiry,
    SupplierReply,
    WhatsAppSendDeferred,
    advance_dialogue,
    assert_send_allowed,
    assert_send_allowed_or_defer,
    assert_supplier_safe_text,
    build_availability_inquiry,
    contains_private_price,
    detect_supplier_language,
    is_opt_out,
)


def test_dialogue_enforces_availability_then_specification_then_price() -> None:
    inquiry = SupplierInquiry(
        product_name="марля медицинская",
        quantity="10 рулонов",
        required_spec="ширина 90 см, плотность не менее 36 г/м²",
    )
    initial = build_availability_inquiry(inquiry)
    assert "есть ли в наличии" in initial
    assert "цен" not in initial.casefold()

    specification = advance_dialogue(
        SupplierDialogueState.WAITING_AVAILABILITY,
        SupplierReply.AVAILABLE,
        inquiry,
    )
    assert specification.state == SupplierDialogueState.WAITING_SPEC
    assert specification.outbound_text is not None
    assert "Подтвердите" in specification.outbound_text
    assert "цен" not in specification.outbound_text.casefold()

    price = advance_dialogue(
        specification.state,
        SupplierReply.SPEC_CONFIRMED,
        inquiry,
    )
    assert price.state == SupplierDialogueState.WAITING_PRICE
    assert price.outbound_text is not None
    assert "лучшую цену" in price.outbound_text

    complete = advance_dialogue(
        price.state,
        SupplierReply.PRICE_RECEIVED,
        inquiry,
    )
    assert complete.state == SupplierDialogueState.COMPLETE
    assert complete.outbound_text is None


def test_kazakh_dialogue_and_high_precision_language_detection() -> None:
    inquiry = SupplierInquiry("медициналық дәке", "10 орам", "ені 90 см")

    initial = build_availability_inquiry(inquiry, "kk")
    followup = advance_dialogue(
        SupplierDialogueState.WAITING_AVAILABILITY,
        SupplierReply.AVAILABLE,
        inquiry,
        "kk",
    )

    assert "Сәлеметсіз бе" in initial
    assert "қолда бар ма" in initial
    assert followup.outbound_text is not None and "сипаттамаларын" in followup.outbound_text
    assert detect_supplier_language("Қолда бар", fallback="ru").value == "kk"
    assert detect_supplier_language("Есть в наличии", fallback="ru").value == "ru"


@pytest.mark.parametrize(
    "text",
    [
        "Наш максимальный бюджет — 120 000 тенге",
        "Тендерная цена составляет 150 000",
        "Бюджет: 100 000 тенге",
        "Максимальная цена 50 000",
        "У нас лимит 70000",
        "Наша закупочная цена 40000",
        "У нас заложена цена 100 000",
        "Our maximum budget is USD 500",
        "The internal target price is 300",
    ],
)
def test_supplier_messages_must_not_disclose_private_price(text: str) -> None:
    assert contains_private_price(text)
    with pytest.raises(ValidationBlocked, match="budget or tender price"):
        assert_supplier_safe_text(text)


@pytest.mark.parametrize(
    "text",
    ["Не пишите мне больше", "СТОП", "Удалите мой номер", "Do not contact us"],
)
def test_opt_out_language_is_recognized(text: str) -> None:
    assert is_opt_out(text)


def test_opt_out_immediately_ends_dialogue() -> None:
    decision = advance_dialogue(
        SupplierDialogueState.WAITING_PRICE,
        SupplierReply.OPT_OUT,
        SupplierInquiry("товар", "1 шт.", "спецификация"),
    )
    assert decision.state == SupplierDialogueState.OPTED_OUT
    assert decision.outbound_text is None


def test_send_policy_blocks_disabled_opted_out_and_quiet_hours() -> None:
    timezone = ZoneInfo("Asia/Almaty")
    daytime = datetime(2026, 9, 22, 12, tzinfo=timezone)
    nighttime = datetime(2026, 9, 22, 21, tzinfo=timezone)

    with pytest.raises(ValidationBlocked, match="disabled"):
        assert_send_allowed(SendContext(enabled=False), now=daytime)
    with pytest.raises(ValidationBlocked, match="opted out"):
        assert_send_allowed(SendContext(enabled=True, opted_out=True), now=daytime)
    with pytest.raises(ValidationBlocked, match="quiet hours"):
        assert_send_allowed(SendContext(enabled=True), now=nighttime)

    assert_send_allowed(SendContext(enabled=True), now=daytime)


def test_out_of_order_supplier_reply_is_blocked() -> None:
    with pytest.raises(ValidationBlocked, match="invalid while dialogue"):
        advance_dialogue(
            SupplierDialogueState.WAITING_AVAILABILITY,
            SupplierReply.PRICE_RECEIVED,
            SupplierInquiry("товар", "1 шт.", "спецификация"),
        )


def test_auto_send_defers_quiet_hours_spacing_and_daily_quota() -> None:
    timezone = ZoneInfo("Asia/Almaty")
    base_policy = SendPolicy(minimum_interval=timedelta(minutes=2))

    night = datetime(2026, 9, 22, 21, tzinfo=timezone)
    with pytest.raises(WhatsAppSendDeferred) as quiet:
        assert_send_allowed_or_defer(
            SendContext(enabled=True),
            now=night,
            policy=base_policy,
            autonomous_case=True,
        )
    assert quiet.value.reason == "supplier quiet hours are active"
    assert quiet.value.available_at == datetime(2026, 9, 23, 9, tzinfo=timezone).astimezone(UTC)

    midday = datetime(2026, 9, 22, 12, tzinfo=timezone)
    with pytest.raises(WhatsAppSendDeferred) as spacing:
        assert_send_allowed_or_defer(
            SendContext(enabled=True, last_sent_at=midday - timedelta(seconds=30)),
            now=midday,
            policy=base_policy,
            autonomous_case=True,
        )
    assert spacing.value.available_at == midday.astimezone(UTC) + timedelta(seconds=90)

    autonomous_policy = SendPolicy(
        daily_limit=30, conversation_limit=60, minimum_interval=timedelta(minutes=2)
    )
    with pytest.raises(WhatsAppSendDeferred) as daily:
        assert_send_allowed_or_defer(
            SendContext(enabled=True, messages_sent_today=30),
            now=midday,
            policy=autonomous_policy,
            autonomous_case=True,
        )
    assert daily.value.reason == "daily WhatsApp limit reached"
    assert daily.value.available_at == datetime(2026, 9, 23, 9, tzinfo=timezone).astimezone(UTC)

    with pytest.raises(ValidationBlocked, match="quiet hours"):
        assert_send_allowed_or_defer(
            SendContext(enabled=True),
            now=night,
            policy=DEFAULT_SEND_POLICY,
            autonomous_case=False,
        )
