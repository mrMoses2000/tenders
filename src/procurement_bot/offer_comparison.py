"""Conservative, goods-only comparison of quotes for one tender position.

Delivery is accounted for after the owner records the actual run. Quoted VAT is
never added or removed: the quoted amount is compared as received.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

_PACK_UNITS = {"pack", "package", "упаковка", "упаковку", "упаковки",
               "упак", "пачка", "пачку", "коробка", "коробку"}
_UNIT_ALIASES = {
    "piece": "piece", "pcs": "piece", "pc": "piece", "шт": "piece",
    "штука": "piece", "штуку": "piece", "штуки": "piece",
    "штук": "piece", "ед": "piece", "дана": "piece", "данасы": "piece",
    "kg": "kg", "кг": "kg", "килограмм": "kg", "килограмма": "kg",
    "g": "g", "г": "g", "грамм": "g",
    "m": "m", "м": "m", "метр": "m", "метра": "m", "метров": "m",
    "m2": "m2", "м2": "m2", "м²": "m2", "кв м": "m2",
    "roll": "roll", "рулон": "roll", "рулона": "roll", "рулонов": "roll",
    "liter": "liter", "l": "liter", "л": "liter", "литр": "liter",
}


def normalize_goods_unit(value: object) -> str:
    text = str(value or "").strip().casefold().rstrip(".")
    return _UNIT_ALIASES.get(text, text)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return None
    return result if result.is_finite() else None


def _ceil_multiple(value: Decimal, multiple: Decimal) -> Decimal:
    return (value / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple


@dataclass(frozen=True, slots=True)
class GoodsComparison:
    offer_id: str
    request_item_id: str
    supplier_id: str
    rank: int | None
    goods_total: Decimal | None
    purchased_quantity: Decimal | None
    tender_total: Decimal | None
    tender_difference: Decimal | None
    currency: str | None
    price_state: str
    technical_state: str
    blockers: tuple[str, ...]

    @property
    def rankable(self) -> bool:
        return not self.blockers and self.goods_total is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "request_item_id": self.request_item_id,
            "supplier_id": self.supplier_id,
            "rank": self.rank,
            "goods_total": self.goods_total,
            "purchased_quantity": self.purchased_quantity,
            "tender_total": self.tender_total,
            "tender_difference": self.tender_difference,
            "currency": self.currency,
            "price_state": self.price_state,
            "technical_state": self.technical_state,
            "blockers": list(self.blockers),
        }


def compare_offer(row: Mapping[str, Any]) -> GoodsComparison:
    """Compare one persisted quote without inventing packaging or stock facts.

    ``available_qty`` and ``pack_qty`` are expressed in the tender item's unit.
    ``moq`` and ``order_multiple`` are expressed in price units (packs when
    ``price_unit`` is pack, otherwise tender-item units).
    """

    blockers: list[str] = []

    def block(code: str) -> None:
        if code not in blockers:
            blockers.append(code)

    item_id = str(row["request_item_id"])
    offer_id = str(row["offer_id"] if "offer_id" in row else row["id"])
    supplier_id = str(row["supplier_id"])
    required = _decimal(row.get("quantity"))
    price = _decimal(row.get("price_amount"))
    available = _decimal(row.get("available_qty"))
    pack_qty = _decimal(row.get("pack_qty"))
    moq = _decimal(row.get("moq"))
    multiple = _decimal(row.get("order_multiple"))
    tender_unit_price = _decimal(row.get("tender_unit_price_private"))
    currency = str(row.get("currency") or "").upper() or None
    tender_currency = str(row.get("tender_currency") or "KZT").upper()
    source_type = str(row.get("source_type") or "")
    stock_source_type = str(row.get("stock_source_type") or source_type)
    technical_state = str(row.get("technical_state") or "needs_confirmation")
    price_state = (
        "supplier_confirmed" if source_type == "supplier_message"
        else "operator_recorded" if source_type == "manual"
        else "published" if price is not None else "unknown"
    )
    if technical_state != "verified":
        block("technical_not_verified")
    if price_state not in {"supplier_confirmed", "operator_recorded"}:
        block("price_not_confirmed")
    if required is None or required <= 0:
        block("required_quantity_unknown")
    if price is None or price < 0 or currency is None:
        block("price_unknown")
    if currency is not None and currency != tender_currency:
        block("currency_not_comparable")
    if available is None or available < 0:
        block("stock_quantity_unknown")
    elif stock_source_type not in {"supplier_message", "manual"}:
        block("stock_not_confirmed")
    if str(row.get("availability_status") or "") == "out_of_stock":
        block("out_of_stock")

    order_units: Decimal | None = None
    purchased_quantity: Decimal | None = None
    price_unit = normalize_goods_unit(row.get("price_unit"))
    item_unit = normalize_goods_unit(row.get("unit"))
    if required is not None and required > 0:
        if price_unit in _PACK_UNITS:
            if pack_qty is None or pack_qty <= 0:
                block("pack_quantity_unknown")
            else:
                order_units = required / pack_qty
        elif price_unit and price_unit == item_unit:
            order_units = required
        else:
            block("price_unit_unknown_or_mismatch")
    if order_units is not None:
        if moq is not None:
            if moq < 0:
                block("invalid_moq")
            else:
                order_units = max(order_units, moq)
        if multiple is not None:
            if multiple <= 0:
                block("invalid_order_multiple")
            else:
                order_units = _ceil_multiple(order_units, multiple)
        elif price_unit in _PACK_UNITS:
            order_units = _ceil_multiple(order_units, Decimal("1"))
        elif order_units != order_units.to_integral_value():
            block("order_multiple_unknown")
        purchased_quantity = (
            order_units * pack_qty if price_unit in _PACK_UNITS and pack_qty is not None
            else order_units
        )
    if purchased_quantity is not None and available is not None and available < purchased_quantity:
        block("insufficient_stock")
    goods_total = order_units * price if order_units is not None and price is not None else None
    tender_total = (
        required * tender_unit_price
        if required is not None and tender_unit_price is not None and tender_unit_price >= 0
        else None
    )
    tender_difference = (
        tender_total - goods_total
        if tender_total is not None and goods_total is not None and currency == tender_currency
        else None
    )
    return GoodsComparison(
        offer_id=offer_id, request_item_id=item_id, supplier_id=supplier_id,
        rank=None, goods_total=goods_total, purchased_quantity=purchased_quantity,
        tender_total=tender_total, tender_difference=tender_difference,
        currency=currency, price_state=price_state,
        technical_state=technical_state, blockers=tuple(blockers),
    )


def rank_goods_offers(rows: Sequence[Mapping[str, Any]]) -> list[GoodsComparison]:
    """Rank comparable, confirmed quotes independently within each item."""

    from dataclasses import replace

    comparisons = [compare_offer(row) for row in rows]
    eligible = sorted(
        (entry for entry in comparisons if entry.rankable),
        key=lambda entry: (
            entry.request_item_id, entry.goods_total,
            entry.supplier_id, entry.offer_id,
        ),
    )
    ranks: dict[str, int] = {}
    last_item: str | None = None
    position = 0
    for entry in eligible:
        if entry.request_item_id != last_item:
            position = 0
            last_item = entry.request_item_id
        position += 1
        ranks[entry.offer_id] = position
    return [replace(entry, rank=ranks.get(entry.offer_id)) for entry in comparisons]
