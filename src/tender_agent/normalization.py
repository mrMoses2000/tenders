from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

_UNIT_ALIASES = {
    "шт": "шт",
    "штука": "шт",
    "штуки": "шт",
    "штук": "шт",
    "pcs": "шт",
    "piece": "шт",
    "kg": "кг",
    "кг": "кг",
    "килограмм": "кг",
    "килограмма": "кг",
    "килограммов": "кг",
    "g": "г",
    "гр": "г",
    "г": "г",
    "грамм": "г",
    "l": "л",
    "л": "л",
    "литр": "л",
    "литра": "л",
    "литров": "л",
    "ml": "мл",
    "мл": "мл",
    "m": "м",
    "м": "м",
    "метр": "м",
    "meter": "м",
    "cm": "см",
    "см": "см",
    "mm": "мм",
    "мм": "мм",
    "мешок": "мешок",
    "мешка": "мешок",
    "мешков": "мешок",
    "упаковка": "упаковка",
    "уп": "упаковка",
    "пачка": "упаковка",
    "однапачка": "упаковка",
    "комплект": "комплект",
    "набор": "комплект",
    "рулон": "рулон",
    "бухта": "бухта",
    "пара": "пара",
    "пары": "пара",
    "пар": "пара",
    "метрпогонный": "м",
    "погонныйметр": "м",
}

_BASE_CONVERSIONS = {
    "г": ("кг", Decimal("0.001")),
    "кг": ("кг", Decimal("1")),
    "мл": ("л", Decimal("0.001")),
    "л": ("л", Decimal("1")),
    "мм": ("м", Decimal("0.001")),
    "см": ("м", Decimal("0.01")),
    "м": ("м", Decimal("1")),
}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits if digits else ""


def canonical_domain(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def normalize_unit(value: str) -> str:
    token = normalize_text(value).replace(" ", "")
    token = token.rstrip(".")
    return _UNIT_ALIASES.get(token, token)


@dataclass(frozen=True)
class UnitConversion:
    compatible: bool
    from_unit: str
    to_unit: str
    multiplier: Decimal | None


def conversion(from_unit: str, to_unit: str) -> UnitConversion:
    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)
    if source == target and source:
        return UnitConversion(True, source, target, Decimal("1"))
    source_base = _BASE_CONVERSIONS.get(source)
    target_base = _BASE_CONVERSIONS.get(target)
    if source_base and target_base and source_base[0] == target_base[0]:
        multiplier = source_base[1] / target_base[1]
        return UnitConversion(True, source, target, multiplier)
    return UnitConversion(False, source, target, None)


def effective_unit_price(
    price: Decimal,
    price_unit: str,
    required_unit: str,
    package_size: Decimal | None = None,
    package_unit: str = "",
) -> Decimal | None:
    """Return price per required unit without guessing package semantics."""
    direct = conversion(price_unit, required_unit)
    if direct.compatible and direct.multiplier:
        return price / direct.multiplier

    price_key = normalize_unit(price_unit)
    if price_key in {"мешок", "упаковка", "комплект", "рулон", "бухта"}:
        if package_size is None or not package_unit:
            return None
        package_conversion = conversion(package_unit, required_unit)
        if not package_conversion.compatible or package_conversion.multiplier is None:
            return None
        required_units_in_package = package_size * package_conversion.multiplier
        if required_units_in_package <= 0:
            return None
        return price / required_units_in_package
    return None
