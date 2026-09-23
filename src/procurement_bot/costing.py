"""Deterministic normalization of supplier quotes into landed costs.

All arithmetic in this module uses :class:`~decimal.Decimal`.  MOQ and order
multiples are expressed in *order units*: individual required units for
``OrderBasis.UNIT`` and packages for ``OrderBasis.PACK``.  Additional cost
components are assumed to already be denominated in the quote currency; this
module deliberately performs no foreign-exchange conversion.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _reject_binary_float(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("decimal values must not be supplied as floats or booleans")
    return value


DecimalValue = Annotated[Decimal, BeforeValidator(_reject_binary_float)]
PositiveDecimal = Annotated[DecimalValue, Field(gt=0)]
NonNegativeDecimal = Annotated[DecimalValue, Field(ge=0)]
VatRate = Annotated[DecimalValue, Field(ge=0, le=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
    )


class OrderBasis(StrEnum):
    UNIT = "unit"
    PACK = "pack"
    UNKNOWN = "unknown"


class PriceBasis(StrEnum):
    UNIT = "unit"
    PACK = "pack"


class VatStatus(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNKNOWN = "unknown"


class CostStatus(StrEnum):
    KNOWN = "known"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class PriceQuote(StrictModel):
    amount: NonNegativeDecimal
    basis: PriceBasis


class CostComponent(StrictModel):
    """A landed-cost component in the quote currency."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: CostStatus
    amount: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_amount_for_status(self) -> CostComponent:
        if self.status == CostStatus.KNOWN and self.amount is None:
            raise ValueError("a known cost component requires an amount")
        if self.status != CostStatus.KNOWN and self.amount is not None:
            raise ValueError("only a known cost component may have an amount")
        return self


def _unknown_delivery() -> CostComponent:
    return CostComponent(code="delivery", status=CostStatus.UNKNOWN)


class OfferCostInput(StrictModel):
    offer_id: str = Field(min_length=1, max_length=100)
    supplier_id: str = Field(min_length=1, max_length=100)
    technically_eligible: bool
    availability_confirmed: bool | None

    required_quantity: PositiveDecimal
    required_unit: str = Field(min_length=1, max_length=50)
    order_basis: OrderBasis = OrderBasis.UNKNOWN
    pack_quantity: PositiveDecimal | None = None
    moq: NonNegativeDecimal | None = Decimal("0")
    order_multiple: PositiveDecimal | None = Decimal("1")

    price: PriceQuote | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    vat_status: VatStatus = VatStatus.UNKNOWN
    vat_rate: VatRate | None = None
    delivery: CostComponent = Field(default_factory=_unknown_delivery)
    other_costs: list[CostComponent] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.upper()
        if not normalized.isalpha() or not normalized.isascii():
            raise ValueError("currency must be a three-letter ASCII code")
        return normalized

    @model_validator(mode="after")
    def validate_components(self) -> OfferCostInput:
        if self.delivery.code != "delivery":
            raise ValueError("delivery component must use the 'delivery' code")
        codes = [component.code for component in self.other_costs]
        if "delivery" in codes:
            raise ValueError("delivery must be supplied through the delivery field")
        if len(codes) != len(set(codes)):
            raise ValueError("other cost component codes must be unique")
        if self.order_basis != OrderBasis.PACK and self.pack_quantity is not None:
            raise ValueError("pack_quantity is only valid for pack ordering")
        return self


class NormalizedOfferCost(StrictModel):
    offer_id: str
    supplier_id: str
    technically_eligible: bool
    availability_confirmed: bool | None
    currency: str | None
    complete: bool
    blockers: list[str]

    required_quantity: PositiveDecimal
    required_unit: str
    order_basis: OrderBasis
    order_quantity: PositiveDecimal | None = None
    package_count: PositiveDecimal | None = None
    purchasable_quantity: PositiveDecimal | None = None
    overage_quantity: NonNegativeDecimal | None = None

    quoted_subtotal: NonNegativeDecimal | None = None
    vat_added: NonNegativeDecimal | None = None
    product_total: NonNegativeDecimal | None = None
    delivery_total: NonNegativeDecimal | None = None
    other_cost_total: NonNegativeDecimal | None = None
    landed_total: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_completion_state(self) -> NormalizedOfferCost:
        complete_values = (
            self.currency,
            self.order_quantity,
            self.purchasable_quantity,
            self.overage_quantity,
            self.quoted_subtotal,
            self.vat_added,
            self.product_total,
            self.delivery_total,
            self.other_cost_total,
            self.landed_total,
        )
        if self.complete and (self.blockers or any(value is None for value in complete_values)):
            raise ValueError("a complete cost requires every total and no blockers")
        if not self.complete and self.landed_total is not None:
            raise ValueError("an incomplete cost cannot expose a landed total")
        return self


class RankedOfferCost(StrictModel):
    cost: NormalizedOfferCost
    eligible: bool
    blockers: list[str]
    rank: int | None = None


def normalize_offer_cost(value: OfferCostInput) -> NormalizedOfferCost:
    """Normalize one offer, returning explicit blockers instead of assumptions."""

    blockers: list[str] = []

    def block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    if value.currency is None:
        block("currency_unknown")

    quantity_per_order_unit: Decimal | None = None
    if value.order_basis == OrderBasis.UNIT:
        quantity_per_order_unit = Decimal("1")
    elif value.order_basis == OrderBasis.PACK:
        if value.pack_quantity is None:
            block("conversion_unknown")
        else:
            quantity_per_order_unit = value.pack_quantity
    else:
        block("conversion_unknown")

    if value.moq is None:
        block("moq_unknown")
    if value.order_multiple is None:
        block("order_multiple_unknown")

    order_quantity: Decimal | None = None
    purchasable_quantity: Decimal | None = None
    overage_quantity: Decimal | None = None
    if (
        quantity_per_order_unit is not None
        and value.moq is not None
        and value.order_multiple is not None
    ):
        needed_order_units = value.required_quantity / quantity_per_order_unit
        target_order_units = max(needed_order_units, value.moq)
        order_quantity = _round_up_to_multiple(target_order_units, value.order_multiple)
        purchasable_quantity = order_quantity * quantity_per_order_unit
        overage_quantity = purchasable_quantity - value.required_quantity

    quoted_subtotal: Decimal | None = None
    if value.price is None:
        block("price_unknown")
    elif order_quantity is not None and purchasable_quantity is not None:
        if value.price.basis == PriceBasis.PACK:
            if value.order_basis != OrderBasis.PACK:
                block("price_conversion_unknown")
            else:
                quoted_subtotal = order_quantity * value.price.amount
        else:
            quoted_subtotal = purchasable_quantity * value.price.amount

    vat_added: Decimal | None = None
    product_total: Decimal | None = None
    if value.vat_status == VatStatus.UNKNOWN:
        block("vat_unknown")
    elif value.vat_status == VatStatus.EXCLUDED and value.vat_rate is None:
        block("vat_rate_unknown")
    elif quoted_subtotal is not None:
        if value.vat_status == VatStatus.EXCLUDED:
            # The missing-rate branch above guarantees this is a Decimal.
            assert value.vat_rate is not None
            vat_added = quoted_subtotal * value.vat_rate
        else:
            vat_added = Decimal("0")
        product_total = quoted_subtotal + vat_added

    delivery_total = _component_amount(value.delivery, block)
    other_amounts = [
        _component_amount(component, block, prefix="additional_cost_unknown")
        for component in value.other_costs
    ]
    other_cost_total = (
        sum((amount for amount in other_amounts if amount is not None), Decimal("0"))
        if all(amount is not None for amount in other_amounts)
        else None
    )

    landed_total: Decimal | None = None
    if product_total is not None and delivery_total is not None and other_cost_total is not None:
        landed_total = product_total + delivery_total + other_cost_total

    complete = not blockers and landed_total is not None
    if not complete:
        landed_total = None

    return NormalizedOfferCost(
        offer_id=value.offer_id,
        supplier_id=value.supplier_id,
        technically_eligible=value.technically_eligible,
        availability_confirmed=value.availability_confirmed,
        currency=value.currency,
        complete=complete,
        blockers=blockers,
        required_quantity=value.required_quantity,
        required_unit=value.required_unit,
        order_basis=value.order_basis,
        order_quantity=order_quantity,
        package_count=(order_quantity if value.order_basis == OrderBasis.PACK else None),
        purchasable_quantity=purchasable_quantity,
        overage_quantity=overage_quantity,
        quoted_subtotal=quoted_subtotal,
        vat_added=vat_added,
        product_total=product_total,
        delivery_total=delivery_total,
        other_cost_total=other_cost_total,
        landed_total=landed_total,
    )


def rank_normalized_costs(
    costs: list[NormalizedOfferCost],
    *,
    comparison_currency: str,
) -> list[RankedOfferCost]:
    """Rank only technically eligible, available, complete comparable offers."""

    currency = _normalize_comparison_currency(comparison_currency)
    results: list[RankedOfferCost] = []
    for cost in costs:
        blockers: list[str] = []
        if not cost.technically_eligible:
            blockers.append("technical_ineligible")
        if cost.availability_confirmed is not True:
            blockers.append("availability_unconfirmed")
        if not cost.complete or cost.landed_total is None:
            blockers.append("cost_incomplete")
        if cost.currency != currency:
            blockers.append("currency_not_comparable")
        results.append(RankedOfferCost(cost=cost, eligible=not blockers, blockers=blockers))

    eligible_indices = sorted(
        (index for index, result in enumerate(results) if result.eligible),
        key=lambda index: (
            results[index].cost.landed_total,
            results[index].cost.overage_quantity,
            results[index].cost.supplier_id.casefold(),
            results[index].cost.offer_id,
            index,
        ),
    )
    ranks = {result_index: rank for rank, result_index in enumerate(eligible_indices, start=1)}
    return [
        result.model_copy(update={"rank": ranks.get(index)})
        for index, result in enumerate(results)
    ]


def _round_up_to_multiple(value: Decimal, multiple: Decimal) -> Decimal:
    return (value / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple


def _component_amount(
    component: CostComponent,
    block: Callable[[str], None],
    *,
    prefix: str | None = None,
) -> Decimal | None:
    if component.status == CostStatus.KNOWN:
        return component.amount
    if component.status == CostStatus.NOT_APPLICABLE:
        return Decimal("0")
    reason = f"{prefix}:{component.code}" if prefix else f"{component.code}_unknown"
    block(reason)
    return None


def _normalize_comparison_currency(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("comparison_currency must be a three-letter ASCII code")
    return normalized
