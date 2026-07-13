from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Iterable

from .economics import money
from .normalization import normalize_text


@dataclass(frozen=True)
class RouteObservation:
    origin: str
    destination: str
    cargo_type: str
    cost: Decimal
    observed_at: date
    evidence_level: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "cost", Decimal(str(self.cost)))
        if self.cost < 0:
            raise ValueError("route cost cannot be negative")
        if self.evidence_level not in range(1, 6):
            raise ValueError("evidence_level must be between 1 and 5")


@dataclass(frozen=True)
class RouteSummary:
    count: int
    minimum: Decimal | None
    median: Decimal | None
    maximum: Decimal | None
    confidence: str


@dataclass(frozen=True)
class ProcurementScenario:
    name: str
    unit_price: Decimal
    quantity: Decimal
    logistics: Decimal = Decimal("0")
    extra_cost: Decimal = Decimal("0")
    risk_reserve_percent: Decimal = Decimal("0")
    lead_days: int | None = None
    technical_valid: bool = True
    evidence_level: int = 1

    def __post_init__(self) -> None:
        for field_name in ("unit_price", "quantity", "logistics", "extra_cost", "risk_reserve_percent"):
            object.__setattr__(self, field_name, Decimal(str(getattr(self, field_name))))
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if min(self.unit_price, self.logistics, self.extra_cost, self.risk_reserve_percent) < 0:
            raise ValueError("scenario cost and risk values cannot be negative")
        if self.evidence_level not in range(1, 6):
            raise ValueError("evidence_level must be between 1 and 5")


@dataclass(frozen=True)
class RankedScenario:
    name: str
    goods_cost: Decimal
    landed_cost: Decimal
    risk_reserve: Decimal
    risk_adjusted_cost: Decimal
    lead_days: int | None
    evidence_level: int
    rank: int | None
    usable: bool


def summarize_route(observations: Iterable[RouteObservation]) -> RouteSummary:
    items = list(observations)
    if not items:
        return RouteSummary(0, None, None, None, "Нет данных")
    groups = {
        (normalize_text(item.origin), normalize_text(item.destination), normalize_text(item.cargo_type))
        for item in items
    }
    if len(groups) != 1:
        raise ValueError("route summary requires one origin, destination and cargo type")
    costs = sorted(item.cost for item in items)
    count = len(costs)
    if count == 1:
        confidence = "Низкая"
    elif count < 4:
        confidence = "Средняя"
    else:
        confidence = "Высокая"
    return RouteSummary(count, money(costs[0]), money(median(costs)), money(costs[-1]), confidence)


def compare_procurement_scenarios(scenarios: Iterable[ProcurementScenario]) -> list[RankedScenario]:
    calculated: list[RankedScenario] = []
    for scenario in scenarios:
        goods = money(scenario.unit_price * scenario.quantity)
        landed = money(goods + scenario.logistics + scenario.extra_cost)
        reserve = money(landed * scenario.risk_reserve_percent / Decimal("100"))
        adjusted = money(landed + reserve)
        calculated.append(RankedScenario(
            scenario.name, goods, landed, reserve, adjusted, scenario.lead_days,
            scenario.evidence_level, None, scenario.technical_valid,
        ))

    usable = sorted(
        ((index, item) for index, item in enumerate(calculated) if item.usable),
        key=lambda pair: (
            pair[1].risk_adjusted_cost,
            pair[1].lead_days if pair[1].lead_days is not None else 10**9,
            -pair[1].evidence_level,
            pair[1].name,
            pair[0],
        ),
    )
    ranks = {source_index: rank for rank, (source_index, _) in enumerate(usable, start=1)}
    ranked = [RankedScenario(
        item.name, item.goods_cost, item.landed_cost, item.risk_reserve,
        item.risk_adjusted_cost, item.lead_days, item.evidence_level,
        ranks.get(index), item.usable,
    ) for index, item in enumerate(calculated)]
    return sorted(
        ranked,
        key=lambda item: (
            item.rank if item.rank is not None else 10**9,
            item.name,
            item.risk_adjusted_cost,
        ),
    )
