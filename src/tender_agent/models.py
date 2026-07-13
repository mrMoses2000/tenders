from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Mapping


def _decimal(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class RequestLine:
    request_id: str
    name: str
    technical_spec: str
    required_quantity: Decimal
    required_unit: str
    tender_unit_price: Decimal
    delivery_city: str = ""
    hard_requirements: Mapping[str, str] = field(default_factory=dict)
    procurement_quantity: Decimal | None = None
    notion_page_url: str = ""
    procurement_quantity_known: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_quantity", _decimal(self.required_quantity))
        object.__setattr__(self, "tender_unit_price", _decimal(self.tender_unit_price))
        procurement_quantity = _decimal(self.procurement_quantity)
        object.__setattr__(self, "procurement_quantity", (
            self.required_quantity
            if procurement_quantity is None and self.procurement_quantity_known
            else procurement_quantity
        ))
        if not self.request_id.strip():
            raise ValueError("request_id is required")
        if self.required_quantity is None or self.required_quantity <= 0:
            raise ValueError("required_quantity must be positive")
        if self.tender_unit_price is None or self.tender_unit_price < 0:
            raise ValueError("tender_unit_price cannot be negative")
        if self.procurement_quantity_known:
            if self.procurement_quantity is None or self.procurement_quantity < 0:
                raise ValueError("procurement_quantity cannot be negative when it is known")
            if self.procurement_quantity > self.required_quantity:
                raise ValueError("procurement_quantity cannot exceed required_quantity")
        elif self.procurement_quantity is not None:
            raise ValueError("unknown procurement quantity must be None")


@dataclass(frozen=True)
class SupplierIdentity:
    name: str
    city: str = ""
    site_url: str = ""
    phone: str = ""
    bin_iin: str = ""
    stable_id: str = ""
    notion_page_url: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("supplier name is required")


@dataclass(frozen=True)
class CandidateOffer:
    supplier_item: str
    source_url: str
    checked_at: date
    price: Decimal | None = None
    price_unit: str = ""
    package_size: Decimal | None = None
    package_unit: str = ""
    available_quantity: Decimal | None = None
    stock_status: str = "Не проверено"
    delivery_status: str = "Не проверено"
    documents_status: str = "Не проверено"
    vat_status: str = "Неизвестно"
    forecast_logistics: Decimal | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)
    evidence: str = ""
    source_type: str = "Сайт"
    model_article: str = ""
    fulfillment_method: str = "Неизвестно"
    pickup_point: str = ""
    availability_note: str = ""
    technical_match_proven: bool = False
    supplier_city: str = ""
    source_photo_url: str = ""
    minimum_order: Decimal | None = None
    order_multiple: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price))
        object.__setattr__(self, "package_size", _decimal(self.package_size))
        object.__setattr__(self, "available_quantity", _decimal(self.available_quantity))
        object.__setattr__(self, "forecast_logistics", _decimal(self.forecast_logistics))
        object.__setattr__(self, "minimum_order", _decimal(self.minimum_order))
        object.__setattr__(self, "order_multiple", _decimal(self.order_multiple))
        if not self.supplier_item.strip():
            raise ValueError("supplier_item is required")
        if not isinstance(self.checked_at, date):
            raise TypeError("checked_at must be a date")
        for field_name in ("price", "available_quantity", "forecast_logistics"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        for field_name in ("package_size", "minimum_order", "order_multiple"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive")
        allowed = {
            "stock_status": {"Не проверено", "Есть", "Частично", "Нет"},
            "delivery_status": {"Не проверено", "Да", "Нет"},
            "documents_status": {"Не проверено", "Да", "Нет"},
            "vat_status": {"Неизвестно", "С НДС", "Без НДС"},
        }
        for field_name, values in allowed.items():
            if getattr(self, field_name) not in values:
                raise ValueError(f"invalid {field_name}: {getattr(self, field_name)}")


@dataclass(frozen=True)
class ValidationResult:
    technical_status: str
    readiness: str
    errors: tuple[str, ...] = ()
    confirmations: tuple[str, ...] = ()
    normalized_unit: str = ""
    effective_unit_price: Decimal | None = None
    enough_quantity: bool | None = None
    purchase_quantity: Decimal | None = None

    @property
    def usable(self) -> bool:
        return (
            self.technical_status != "❌ Не соответствует"
            and self.readiness != "Отклонено"
            and self.enough_quantity is not False
        )


@dataclass(frozen=True)
class LandedCost:
    tender_revenue: Decimal
    goods_cost: Decimal | None
    logistics_cost: Decimal
    extra_cost: Decimal
    total_cost: Decimal | None
    profit: Decimal | None
    margin_percent: Decimal | None
    economy: str
    purchase_quantity: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in self.__dict__.items()
        }
