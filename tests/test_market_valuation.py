from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from procurement_bot.market_valuation import MarketQuote, TenderLine, value_tender


def _line(number: int = 1) -> TenderLine:
    return TenderLine(number, "Полотно", "80 см, 100 м", "рулон", Decimal(10), Decimal(23860))


def _quote(**changes: object) -> MarketQuote:
    quote = MarketQuote(
        line_number=1,
        supplier="Магазин",
        product="Полотно 80 см, 100 м",
        source_url="https://shop.kz/product/1",
        checked_at=datetime.now(UTC),
        price=Decimal(545),
        price_unit="метр",
        tender_units_per_price_unit=Decimal("0.01"),
        specification_match="exact",
        price_state="published",
        availability="advertised",
        vat_state="unknown",
        delivery_state="unknown",
        evidence_excerpt="80 см, 100 м, 545 ₸/м",
    )
    return replace(quote, **changes)


def test_converts_metres_to_rolls_and_keeps_partial_total_separate() -> None:
    result = value_tender([_line(), _line(2)], [_quote()])

    assert result.tender_total == Decimal(477200)
    assert result.tender_total_covered == Decimal(238600)
    assert result.market_total_covered == Decimal(545000)
    assert result.difference_covered == Decimal(-306400)
    assert result.complete_market_total is None
    assert result.rows[0].market_unit_price == Decimal(54500)
    assert result.rows[1].market_total is None


def test_stale_mismatch_and_insufficient_quantity_are_excluded() -> None:
    result = value_tender(
        [_line()],
        [
            _quote(checked_at=datetime.now(UTC) - timedelta(days=2)),
            _quote(specification_match="uncertain"),
            _quote(available_quantity=Decimal("0.04")),
            _quote(price_state="ask_supplier"),
        ],
    )

    assert result.rows[0].quote is None
    assert result.complete_market_total is None
    assert "Доступного количества меньше потребности" in result.rows[0].notes


def test_invalid_discovery_link_and_unknown_states_are_excluded() -> None:
    result = value_tender(
        [_line()],
        [_quote(discovery_url="https://2gis.kz/almaty/search/shop", availability="maybe")],
    )

    assert result.rows[0].quote is None
    assert "Поиск через 2GIS не ссылается на карточку фирмы" in result.rows[0].notes


def test_order_multiple_counts_required_extra_units_and_selects_lowest_landed_cost() -> None:
    line = TenderLine(1, "Стаканы", "PP 200 мл", "штука", Decimal(25000), Decimal(6))
    bulk = _quote(
        price=Decimal("7.7"), tender_units_per_price_unit=Decimal(1),
        order_multiple=Decimal(4000),
    )
    loose = _quote(
        price=Decimal(8), tender_units_per_price_unit=Decimal(1),
        source_url="https://shop.kz/product/2",
    )

    result = value_tender([line], [bulk, loose])

    assert result.rows[0].quote == loose
    assert result.market_total_covered == Decimal(200000)
    bulk_only = value_tender([line], [bulk]).rows[0]
    assert bulk_only.market_purchase_quantity == Decimal(28000)
    assert bulk_only.market_total == Decimal(215600)
