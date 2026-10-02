"""Conservative, source-backed comparison of tender and public market prices.

This projection has no network access. Research providers must capture a current
product page before passing a quote here. Public listings never prove stock,
delivery, VAT treatment or that a seller will honour the advertised price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class TenderLine:
    number: int
    name: str
    specification: str
    unit: str
    quantity: Decimal
    tender_unit_price: Decimal

    @property
    def tender_total(self) -> Decimal:
        return self.quantity * self.tender_unit_price


@dataclass(frozen=True, slots=True)
class MarketQuote:
    line_number: int
    supplier: str
    product: str
    source_url: str
    checked_at: datetime
    price: Decimal
    price_unit: str
    tender_units_per_price_unit: Decimal
    specification_match: str  # exact, uncertain, mismatch
    price_state: str  # published, confirmed, ask_supplier
    availability: str  # advertised, confirmed, unknown, unavailable
    vat_state: str  # excluded, included, unknown
    delivery_state: str  # included, extra, unknown
    evidence_excerpt: str
    available_quantity: Decimal | None = None
    order_multiple: Decimal | None = None  # tender units sold only in this increment
    discovery_url: str | None = None  # 2GIS firm card, when that path found the shop


@dataclass(frozen=True, slots=True)
class ValuationRow:
    line: TenderLine
    quote: MarketQuote | None
    market_unit_price: Decimal | None
    market_purchase_quantity: Decimal | None
    market_total: Decimal | None
    difference: Decimal | None
    ratio: Decimal | None
    status: str
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Valuation:
    rows: tuple[ValuationRow, ...]
    tender_total: Decimal
    tender_total_covered: Decimal
    market_total_covered: Decimal
    difference_covered: Decimal
    complete_market_total: Decimal | None


def _purchase_quantity(line: TenderLine, quote: MarketQuote) -> Decimal:
    if quote.order_multiple is None or quote.order_multiple <= 0:
        return line.quantity
    return (
        (line.quantity / quote.order_multiple).to_integral_value(rounding=ROUND_CEILING)
        * quote.order_multiple
    )


def _quote_issues(line: TenderLine, quote: MarketQuote, now: datetime) -> list[str]:
    issues: list[str] = []
    parsed = urlparse(quote.source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        issues.append("Нет прямой HTTPS-ссылки на товар")
    if quote.checked_at.tzinfo is None or quote.checked_at > now + timedelta(minutes=5):
        issues.append("Некорректное время проверки")
    elif now - quote.checked_at > timedelta(hours=24):
        issues.append("Карточка не проверена за последние 24 часа")
    if quote.specification_match != "exact":
        issues.append("Соответствие ТЗ не доказано")
    if quote.price_state not in {"published", "confirmed"}:
        issues.append("Цена требует уточнения у продавца")
    if quote.availability not in {"advertised", "confirmed", "unknown", "unavailable"}:
        issues.append("Статус наличия не распознан")
    if quote.vat_state not in {"included", "excluded", "unknown"}:
        issues.append("Статус НДС не распознан")
    if quote.delivery_state not in {"included", "extra", "unknown"}:
        issues.append("Статус доставки не распознан")
    if quote.price <= 0 or quote.tender_units_per_price_unit <= 0:
        issues.append("Цена или пересчёт единиц некорректны")
    if quote.order_multiple is not None and quote.order_multiple <= 0:
        issues.append("Кратность заказа некорректна")
    if quote.availability == "unavailable":
        issues.append("Товар указан как недоступный")
    if quote.available_quantity is not None and (
        quote.available_quantity * quote.tender_units_per_price_unit
        < _purchase_quantity(line, quote)
    ):
        issues.append("Доступного количества меньше потребности")
    if not quote.evidence_excerpt.strip():
        issues.append("Не сохранён фрагмент карточки с ценой и характеристиками")
    if quote.discovery_url:
        discovery = urlparse(quote.discovery_url)
        if (discovery.scheme != "https" or discovery.hostname not in
                {"2gis.kz", "www.2gis.kz"} or "/firm/" not in discovery.path):
            issues.append("Поиск через 2GIS не ссылается на карточку фирмы")
    return issues


def value_tender(
    lines: list[TenderLine], quotes: list[MarketQuote], *, now: datetime | None = None
) -> Valuation:
    """Select the least priced eligible quote per line without inventing gaps.

    Totals for the entire tender are withheld until every line has a current,
    technically matching price. The covered subtotal is never presented as the
    full market cost. Tender prices and market sources stay separate.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if len({line.number for line in lines}) != len(lines):
        raise ValueError("duplicate tender line number")
    if any(line.quantity <= 0 or line.tender_unit_price < 0 for line in lines):
        raise ValueError("invalid tender quantity or price")
    by_line: dict[int, list[MarketQuote]] = {}
    for quote in quotes:
        by_line.setdefault(quote.line_number, []).append(quote)
    rows: list[ValuationRow] = []
    for line in lines:
        candidates = by_line.get(line.number, [])
        valid = [quote for quote in candidates if not _quote_issues(line, quote, now)]
        if not valid:
            notes = tuple(dict.fromkeys(
                issue for quote in candidates for issue in _quote_issues(line, quote, now)
            ))
            rows.append(ValuationRow(
                line, None, None, None, None, None, None,
                "нет сопоставимой цены", notes or ("Источник не найден",),
            ))
            continue
        selected = min(
            valid,
            key=lambda quote: (
                _purchase_quantity(line, quote)
                * quote.price / quote.tender_units_per_price_unit,
                quote.source_url,
            ),
        )
        unit_price = selected.price / selected.tender_units_per_price_unit
        purchase_quantity = _purchase_quantity(line, selected)
        market_total = purchase_quantity * unit_price
        notes = []
        if purchase_quantity > line.quantity:
            notes.append(f"Кратность заказа: нужно купить {purchase_quantity} {line.unit}")
        if selected.price_state != "confirmed":
            notes.append("Публичная цена; продавец не подтвердил её")
        if selected.availability != "confirmed":
            notes.append("Наличие нужной партии не подтверждено")
        if selected.vat_state != "excluded":
            notes.append("Сравнение с тендером без НДС условно: НДС источника не исключён")
        if selected.delivery_state != "included":
            notes.append("Доставка до заказчика не включена или неизвестна")
        rows.append(ValuationRow(
            line, selected, unit_price, purchase_quantity, market_total,
            line.tender_total - market_total,
            (line.tender_unit_price / unit_price) if unit_price else None,
            "подтверждено продавцом" if selected.price_state == "confirmed"
            and selected.availability == "confirmed" else "публичная оценка",
            tuple(notes),
        ))
    covered = [row for row in rows if row.market_total is not None]
    tender_total = sum((line.tender_total for line in lines), Decimal(0))
    tender_covered = sum((row.line.tender_total for row in covered), Decimal(0))
    market_covered = sum((row.market_total for row in covered), Decimal(0))
    return Valuation(
        tuple(rows), tender_total, tender_covered, market_covered,
        tender_covered - market_covered,
        market_covered if len(covered) == len(lines) else None,
    )
