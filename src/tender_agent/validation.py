from __future__ import annotations

from decimal import Decimal

from .economics import calculate_purchase_quantity
from .models import CandidateOffer, RequestLine, ValidationResult
from .normalization import effective_unit_price, normalize_text, normalize_unit

TECH_EXACT = "✅ Точное соответствие"
TECH_PENDING = "⚠️ Вероятно — подтвердить"
TECH_REJECT = "❌ Не соответствует"


def _same_value(expected: str, actual: str) -> bool:
    return normalize_text(expected) == normalize_text(actual)


def validate_candidate(request: RequestLine, offer: CandidateOffer) -> ValidationResult:
    errors: list[str] = []
    confirmations: list[str] = []
    purchase_quantity = None
    if request.procurement_quantity_known and request.procurement_quantity is not None:
        purchase_quantity = (
            Decimal("0")
            if request.procurement_quantity == 0
            else calculate_purchase_quantity(
                request.procurement_quantity,
                minimum_order=offer.minimum_order,
                order_multiple=offer.order_multiple,
            )
        )
    else:
        confirmations.append("Уточнить остаток к закупке")
    if purchase_quantity is not None and purchase_quantity > request.procurement_quantity:
        confirmations.append(
            f"Закупка увеличивается с {request.procurement_quantity} до {purchase_quantity} "
            "из-за минимального заказа или кратности"
        )

    effective_price: Decimal | None = None
    if offer.price is None:
        confirmations.append("Подтвердить цену")
    else:
        effective_price = effective_unit_price(
            offer.price,
            offer.price_unit,
            request.required_unit,
            offer.package_size,
            offer.package_unit,
        )
        if effective_price is None:
            confirmations.append(
                f"Нельзя сопоставить единицу цены '{offer.price_unit}' с требуемой единицей "
                f"'{request.required_unit}' без подтвержденной фасовки"
            )

    missing_hard = not request.hard_requirements and not offer.technical_match_proven
    if missing_hard:
        confirmations.append("Подтвердить обязательные параметры технического соответствия")
    for key, expected in request.hard_requirements.items():
        actual = offer.attributes.get(key)
        if actual is None or not str(actual).strip():
            confirmations.append(f"Подтвердить {key}: требуется {expected}")
            missing_hard = True
        elif not _same_value(str(expected), str(actual)):
            errors.append(f"{key}: требуется '{expected}', найдено '{actual}'")

    enough_quantity: bool | None
    if offer.stock_status == "Нет":
        enough_quantity = False
        errors.append("Поставщик подтвердил отсутствие товара")
    elif purchase_quantity == 0:
        enough_quantity = True
    elif offer.available_quantity is None or purchase_quantity is None:
        enough_quantity = None
        if offer.available_quantity is None:
            confirmations.append("Подтвердить доступное количество")
    else:
        enough_quantity = offer.available_quantity >= purchase_quantity
        if not enough_quantity:
            confirmations.append(
                f"Доступно {offer.available_quantity}, для покупки требуется {purchase_quantity}"
            )

    if offer.stock_status == "Не проверено":
        confirmations.append("Подтвердить наличие")
    if offer.delivery_status == "Не проверено":
        confirmations.append("Подтвердить доставку или самовывоз")
    if offer.documents_status == "Не проверено":
        confirmations.append("Подтвердить документы")
    if offer.vat_status == "Неизвестно":
        confirmations.append("Подтвердить НДС")

    unit_name = normalize_unit(request.required_unit)
    technical_errors = [error for error in errors if "отсутствие" not in error]
    if technical_errors:
        technical_status = TECH_REJECT
    elif missing_hard:
        technical_status = TECH_PENDING
    else:
        technical_status = TECH_EXACT

    if technical_status == TECH_REJECT or offer.stock_status == "Нет":
        readiness = "Отклонено"
    elif confirmations:
        readiness = "Нужно связаться"
    else:
        readiness = "Подтверждено"

    return ValidationResult(
        technical_status=technical_status,
        readiness=readiness,
        errors=tuple(dict.fromkeys(errors)),
        confirmations=tuple(dict.fromkeys(confirmations)),
        normalized_unit=unit_name,
        effective_unit_price=effective_price,
        enough_quantity=enough_quantity,
        purchase_quantity=purchase_quantity,
    )
