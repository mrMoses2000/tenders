from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
import re
from typing import Mapping

from .models import LandedCost


TIYN = Decimal("0.01")


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value))


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(TIYN, rounding=ROUND_HALF_UP)


def calculate_purchase_quantity(
    required_quantity: Decimal | int | float | str,
    *,
    minimum_order: Decimal | int | float | str | None = None,
    order_multiple: Decimal | int | float | str | None = None,
) -> Decimal:
    """Return the quantity that must actually be bought after MOQ and order-step rounding."""
    required = Decimal(str(required_quantity))
    if required <= 0:
        raise ValueError("required_quantity must be positive")
    minimum = required if minimum_order is None else Decimal(str(minimum_order))
    if minimum <= 0:
        raise ValueError("minimum_order must be positive")
    purchase = max(required, minimum)
    if order_multiple is not None:
        multiple = Decimal(str(order_multiple))
        if multiple <= 0:
            raise ValueError("order_multiple must be positive")
        steps = (purchase / multiple).to_integral_value(rounding=ROUND_CEILING)
        purchase = steps * multiple
    return purchase


def calculate_landed_cost(
    *,
    tender_unit_price: Decimal | int | float | str,
    required_quantity: Decimal | int | float | str,
    offer_unit_price: Decimal | int | float | str | None,
    logistics: Decimal | int | float | str = 0,
    extra_cost: Decimal | int | float | str = 0,
    borderline_margin_percent: Decimal | int | float | str = 10,
    minimum_order: Decimal | int | float | str | None = None,
    order_multiple: Decimal | int | float | str | None = None,
) -> LandedCost:
    quantity = Decimal(str(required_quantity))
    if quantity <= 0:
        raise ValueError("required_quantity must be positive")
    tender_price = Decimal(str(tender_unit_price))
    logistics_cost = money(logistics)
    additional = money(extra_cost)
    purchase_quantity = calculate_purchase_quantity(
        quantity, minimum_order=minimum_order, order_multiple=order_multiple
    )
    if min(tender_price, logistics_cost, additional) < 0:
        raise ValueError("cost values cannot be negative")

    revenue = money(tender_price * quantity)
    if offer_unit_price is None:
        return LandedCost(
            tender_revenue=revenue,
            goods_cost=None,
            logistics_cost=logistics_cost,
            extra_cost=additional,
            total_cost=None,
            profit=None,
            margin_percent=None,
            economy="Не рассчитано",
            purchase_quantity=purchase_quantity,
        )

    offer_price = Decimal(str(offer_unit_price))
    if offer_price < 0:
        raise ValueError("offer_unit_price cannot be negative")
    goods = money(offer_price * purchase_quantity)
    total = money(goods + logistics_cost + additional)
    profit = money(revenue - total)
    margin = None if revenue == 0 else (profit / revenue * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    threshold = Decimal(str(borderline_margin_percent))
    if profit < 0:
        economy = "Невыгодно"
    elif margin is not None and margin < threshold:
        economy = "Погранично"
    else:
        economy = "Выгодно"
    return LandedCost(
        revenue, goods, logistics_cost, additional, total, profit, margin, economy,
        purchase_quantity,
    )


def allocate_delivery_cost(
    total_cost: Decimal | int | float | str,
    weights: Mapping[str, Decimal | int | float | str],
) -> dict[str, Decimal]:
    """Allocate to tiyn with a deterministic largest-remainder method."""
    total = money(total_cost)
    if total < 0:
        raise ValueError("total_cost cannot be negative")
    if not weights:
        raise ValueError("at least one allocation item is required")
    normalized = {key: Decimal(str(value)) for key, value in weights.items()}
    if any(value < 0 for value in normalized.values()):
        raise ValueError("weights cannot be negative")
    weight_sum = sum(normalized.values(), Decimal("0"))
    if weight_sum <= 0:
        raise ValueError("sum of weights must be positive")

    raw = {key: total * value / weight_sum for key, value in normalized.items()}
    allocated = {key: value.quantize(TIYN, rounding=ROUND_DOWN) for key, value in raw.items()}
    remainder_tiyn = int(((total - sum(allocated.values(), Decimal("0"))) / TIYN).to_integral_value())
    order = sorted(raw, key=lambda key: (-(raw[key] - allocated[key]), _natural_key(key)))
    for key in order[:remainder_tiyn]:
        allocated[key] += TIYN
    if sum(allocated.values(), Decimal("0")) != total:
        raise AssertionError("delivery allocation invariant failed")
    return allocated
