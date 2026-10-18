from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .economics import money


@dataclass(frozen=True)
class MarketItemHypothesis:
    item_id: str
    name: str
    quantity: Decimal
    fallback_landed_unit_price: Decimal
    market_unit_price_low: Decimal
    market_unit_price_high: Decimal
    probability_found: Decimal
    unit: str = "ед."
    evidence: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "quantity", "fallback_landed_unit_price", "market_unit_price_low",
            "market_unit_price_high", "probability_found",
        ):
            object.__setattr__(self, field_name, Decimal(str(getattr(self, field_name))))
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if min(self.fallback_landed_unit_price, self.market_unit_price_low, self.market_unit_price_high) < 0:
            raise ValueError("prices cannot be negative")
        if self.market_unit_price_low > self.market_unit_price_high:
            raise ValueError("market price low cannot exceed high")
        if not Decimal("0") <= self.probability_found <= Decimal("1"):
            raise ValueError("probability_found must be between 0 and 1")


@dataclass(frozen=True)
class TripEconomics:
    trip_cost: Decimal
    gross_best_case_savings: Decimal
    expected_savings_before_trip: Decimal
    expected_net_savings: Decimal
    break_even_success_probability: Decimal | None
    recommendation: str
    route_sheet: tuple[str, ...]


def evaluate_market_trip(
    *,
    market_name: str,
    city: str,
    trip_cost: Decimal | int | float | str,
    items: Iterable[MarketItemHypothesis],
    minimum_net_savings: Decimal | int | float | str = 0,
) -> TripEconomics:
    cost = money(trip_cost)
    minimum = money(minimum_net_savings)
    if cost < 0 or minimum < 0:
        raise ValueError("trip and minimum savings cannot be negative")
    hypotheses = list(items)
    if not hypotheses:
        raise ValueError("at least one market hypothesis is required")

    best = Decimal("0")
    expected = Decimal("0")
    route: list[str] = []
    for item in hypotheses:
        market_mid = (item.market_unit_price_low + item.market_unit_price_high) / Decimal("2")
        best_item = max(Decimal("0"), item.fallback_landed_unit_price - item.market_unit_price_low) * item.quantity
        expected_item = max(Decimal("0"), item.fallback_landed_unit_price - market_mid) * item.quantity * item.probability_found
        best += best_item
        expected += expected_item
        route.append(
            f"{item.item_id}: {item.name} — искать {item.quantity} {item.unit}; ориентир "
            f"{money(item.market_unit_price_low)}–{money(item.market_unit_price_high)} ₸; "
            f"вероятность {int(item.probability_found * 100)}%; проверить ТЗ, остаток, документы и контакт"
        )

    best = money(best)
    expected = money(expected)
    net = money(expected - cost)
    break_even = None if best <= 0 else (cost / best).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if net >= minimum:
        recommendation = "Поездка экономически оправдана как гипотеза"
    elif best <= cost:
        recommendation = "Поездка не окупается даже в лучшем ценовом сценарии"
    else:
        recommendation = "Поездка возможна только при дополнительном подтверждении наличия или объединении позиций"

    header = f"{market_name}, {city}: начать с подтвержденных контактов и профильных рядов"
    return TripEconomics(cost, best, expected, net, break_even, recommendation, tuple([header, *route]))
