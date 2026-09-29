from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from procurement_bot.offer_comparison import compare_offer, rank_goods_offers
from procurement_bot.offer_technical import technical_states
from procurement_bot.supplier_reply_processing import extract_quote_details


def _quote(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "offer_id": str(uuid4()), "request_item_id": "item-1",
        "supplier_id": str(uuid4()), "quantity": Decimal("20"), "unit": "шт",
        "price_amount": Decimal("100"), "currency": "KZT",
        "price_unit": "шт", "source_type": "supplier_message",
        "available_qty": Decimal("50"), "stock_source_type": "supplier_message",
        "availability_status": "in_stock", "technical_state": "verified",
        "tender_unit_price_private": Decimal("150"), "tender_currency": "KZT",
        "pack_qty": None, "moq": None, "order_multiple": None,
    }
    row.update(changes)
    return row


def test_goods_only_ranking_uses_confirmed_batch_cost() -> None:
    one = _quote(price_amount=Decimal("90"), pack_qty=Decimal("6"),
                 price_unit="упаковка")
    two = _quote(price_amount=Decimal("20"))
    values = rank_goods_offers([one, two])

    assert values[0].goods_total == Decimal("360")
    assert values[0].purchased_quantity == Decimal("24")
    assert values[0].tender_total == Decimal("3000")
    assert values[0].tender_difference == Decimal("2640")
    assert values[0].rank == 1
    assert values[1].rank == 2


def test_published_price_or_unconfirmed_stock_does_not_enter_top_five() -> None:
    published = compare_offer(_quote(source_type="marketplace"))
    unconfirmed_stock = compare_offer(_quote(stock_source_type="2gis"))

    assert "price_not_confirmed" in published.blockers
    assert "stock_not_confirmed" in unconfirmed_stock.blockers
    assert not published.rankable
    assert not unconfirmed_stock.rankable


def test_technical_review_requires_whole_spec_and_hard_checks() -> None:
    offer = _quote(offer_status="exact")
    requirement = {"id": "hard-1", "request_item_id": "item-1", "is_hard": True}
    whole = {"id": "review-1", "offer_id": offer["offer_id"],
             "requirement_id": None, "verdict": "match", "created_at": "2026-09-28"}
    check = {"id": "review-2", "offer_id": offer["offer_id"],
             "requirement_id": "hard-1", "verdict": "match", "created_at": "2026-09-28"}

    assert technical_states([offer], [requirement], [])[str(offer["offer_id"])] == (
        "needs_confirmation"
    )
    assert technical_states([offer], [requirement], [whole])[str(offer["offer_id"])] == (
        "needs_confirmation"
    )
    assert technical_states([offer], [requirement], [whole, check])[
        str(offer["offer_id"])
    ] == "verified"


def test_extracts_only_explicit_quote_units_and_stock() -> None:
    details = extract_quote_details(
        "Цена 900 тг за упаковку. В упаковке 6 шт. Есть 40 шт. "
        "Минимальная партия 2 упаковки.", item_unit="шт"
    )

    assert details.price_unit == "упаковку"
    assert details.pack_qty == Decimal("6")
    assert details.available_qty == Decimal("40")
    assert details.moq == Decimal("2")
