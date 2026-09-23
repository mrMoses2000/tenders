from decimal import Decimal

import pytest
from pydantic import ValidationError

from procurement_bot.costing import (
    CostComponent,
    CostStatus,
    OfferCostInput,
    OrderBasis,
    PriceBasis,
    PriceQuote,
    VatStatus,
    normalize_offer_cost,
    rank_normalized_costs,
)


def _not_applicable(code: str) -> CostComponent:
    return CostComponent(code=code, status=CostStatus.NOT_APPLICABLE)


def _base_offer(identifier: str = "offer-1") -> OfferCostInput:
    return OfferCostInput(
        offer_id=identifier,
        supplier_id=f"supplier-{identifier}",
        technically_eligible=True,
        availability_confirmed=True,
        required_quantity=Decimal("10"),
        required_unit="roll",
        order_basis=OrderBasis.UNIT,
        price=PriceQuote(amount=Decimal("5"), basis=PriceBasis.UNIT),
        currency="kzt",
        vat_status=VatStatus.INCLUDED,
        delivery=_not_applicable("delivery"),
    )


def test_unit_quote_produces_complete_landed_cost() -> None:
    value = _base_offer().model_copy(
        update={
            "delivery": CostComponent(
                code="delivery", status=CostStatus.KNOWN, amount=Decimal("100")
            )
        }
    )

    result = normalize_offer_cost(value)

    assert result.complete is True
    assert result.currency == "KZT"
    assert result.order_quantity == Decimal("10")
    assert result.purchasable_quantity == Decimal("10")
    assert result.quoted_subtotal == Decimal("50")
    assert result.vat_added == Decimal("0")
    assert result.landed_total == Decimal("150")


def test_pack_moq_multiple_and_excluded_vat_are_calculated_exactly() -> None:
    value = OfferCostInput(
        offer_id="pack-offer",
        supplier_id="supplier-a",
        technically_eligible=True,
        availability_confirmed=True,
        required_quantity=Decimal("11"),
        required_unit="piece",
        order_basis=OrderBasis.PACK,
        pack_quantity=Decimal("6"),
        moq=Decimal("3"),
        order_multiple=Decimal("2"),
        price=PriceQuote(amount=Decimal("1000"), basis=PriceBasis.PACK),
        currency="KZT",
        vat_status=VatStatus.EXCLUDED,
        vat_rate=Decimal("0.12"),
        delivery=CostComponent(
            code="delivery", status=CostStatus.KNOWN, amount=Decimal("500")
        ),
        other_costs=[
            CostComponent(code="handling", status=CostStatus.KNOWN, amount=Decimal("20"))
        ],
    )

    result = normalize_offer_cost(value)

    assert result.blockers == []
    assert result.package_count == Decimal("4")
    assert result.purchasable_quantity == Decimal("24")
    assert result.overage_quantity == Decimal("13")
    assert result.quoted_subtotal == Decimal("4000")
    assert result.vat_added == Decimal("480.00")
    assert result.landed_total == Decimal("5000.00")


def test_unit_price_uses_rounded_purchasable_pack_quantity() -> None:
    value = _base_offer().model_copy(
        update={
            "required_quantity": Decimal("11"),
            "order_basis": OrderBasis.PACK,
            "pack_quantity": Decimal("6"),
            "price": PriceQuote(amount=Decimal("10"), basis=PriceBasis.UNIT),
        }
    )

    result = normalize_offer_cost(value)

    assert result.package_count == Decimal("2")
    assert result.purchasable_quantity == Decimal("12")
    assert result.quoted_subtotal == Decimal("120")
    assert result.landed_total == Decimal("120")


def test_unknown_inputs_fail_closed_with_explicit_blockers() -> None:
    value = OfferCostInput(
        offer_id="unknown",
        supplier_id="supplier-unknown",
        technically_eligible=True,
        availability_confirmed=True,
        required_quantity=Decimal("1"),
        required_unit="piece",
        order_basis=OrderBasis.UNKNOWN,
        moq=None,
        order_multiple=None,
        currency=None,
        vat_status=VatStatus.UNKNOWN,
        delivery=CostComponent(code="delivery", status=CostStatus.UNKNOWN),
        other_costs=[CostComponent(code="installation", status=CostStatus.UNKNOWN)],
    )

    result = normalize_offer_cost(value)

    assert result.complete is False
    assert result.landed_total is None
    assert result.blockers == [
        "currency_unknown",
        "conversion_unknown",
        "moq_unknown",
        "order_multiple_unknown",
        "price_unknown",
        "vat_unknown",
        "delivery_unknown",
        "additional_cost_unknown:installation",
    ]


def test_pack_price_without_pack_conversion_is_not_guessed() -> None:
    value = _base_offer().model_copy(
        update={"price": PriceQuote(amount=Decimal("100"), basis=PriceBasis.PACK)}
    )

    result = normalize_offer_cost(value)

    assert result.complete is False
    assert result.quoted_subtotal is None
    assert "price_conversion_unknown" in result.blockers


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("required_quantity", 10.5),
        ("moq", 1.0),
        ("order_multiple", 1.0),
        ("pack_quantity", 6.0),
    ],
)
def test_binary_floats_are_rejected(field: str, bad_value: float) -> None:
    data = _base_offer().model_dump()
    data.update({"order_basis": OrderBasis.PACK, "pack_quantity": Decimal("6")})
    data[field] = bad_value

    with pytest.raises(ValidationError):
        OfferCostInput.model_validate(data)


def test_ranking_filters_ineligible_incomplete_and_foreign_currency() -> None:
    alpha = normalize_offer_cost(_base_offer("alpha"))
    beta = normalize_offer_cost(
        _base_offer("beta").model_copy(update={"supplier_id": "a-supplier"})
    )
    wrong_currency = normalize_offer_cost(
        _base_offer("usd").model_copy(update={"currency": "USD"})
    )
    unavailable = normalize_offer_cost(
        _base_offer("unavailable").model_copy(update={"availability_confirmed": None})
    )
    incomplete = normalize_offer_cost(
        _base_offer("incomplete").model_copy(
            update={"delivery": CostComponent(code="delivery", status=CostStatus.UNKNOWN)}
        )
    )
    mismatch = normalize_offer_cost(
        _base_offer("mismatch").model_copy(update={"technically_eligible": False})
    )

    ranked = rank_normalized_costs(
        [alpha, wrong_currency, unavailable, incomplete, mismatch, beta],
        comparison_currency="kzt",
    )
    by_id = {item.cost.offer_id: item for item in ranked}

    assert by_id["beta"].rank == 1
    assert by_id["alpha"].rank == 2
    assert by_id["usd"].blockers == ["currency_not_comparable"]
    assert by_id["unavailable"].blockers == ["availability_unconfirmed"]
    assert by_id["incomplete"].blockers == ["cost_incomplete"]
    assert by_id["mismatch"].blockers == ["technical_ineligible"]
    assert all(by_id[key].rank is None for key in ("usd", "unavailable", "incomplete", "mismatch"))


def test_cost_component_state_is_strict() -> None:
    with pytest.raises(ValidationError):
        CostComponent(code="delivery", status=CostStatus.KNOWN)
    with pytest.raises(ValidationError):
        CostComponent(
            code="delivery",
            status=CostStatus.UNKNOWN,
            amount=Decimal("0"),
        )
