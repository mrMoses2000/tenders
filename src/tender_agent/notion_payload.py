from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from typing import Any

from .dedupe import supplier_key
from .economics import calculate_landed_cost
from .models import CandidateOffer, RequestLine, SupplierIdentity, ValidationResult
from .normalization import canonical_domain, normalize_text


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def offer_observation_key(
    request: RequestLine,
    supplier: SupplierIdentity,
    offer: CandidateOffer,
) -> str:
    source = canonical_domain(offer.source_url) or offer.source_type.casefold()
    identity = "|".join(
        [
            request.notion_page_url or request.request_id,
            supplier.notion_page_url or supplier_key(supplier),
            source,
            offer.source_url,
            normalize_text(offer.model_article or offer.supplier_item),
            offer.checked_at.isoformat(),
        ]
    )
    # Notion auto-links URLs inside rich-text fields. Persist only a compact digest so
    # fetch/render round-trips cannot rewrite the idempotency key as Markdown.
    return "offer:v3:" + sha256(identity.encode("utf-8")).hexdigest()


def pack_notion_offer(
    request: RequestLine,
    supplier: SupplierIdentity,
    offer: CandidateOffer,
    validation: ValidationResult,
) -> dict[str, Any]:
    """Create a deterministic, transport-neutral payload. No Notion writes happen here."""
    title = f"{request.request_id} · {supplier.name} · {offer.supplier_item}"
    confirmations = "; ".join(validation.confirmations)
    landed = None
    if (
        request.procurement_quantity_known
        and request.procurement_quantity is not None
        and request.procurement_quantity > 0
    ):
        landed = calculate_landed_cost(
            tender_unit_price=request.tender_unit_price,
            required_quantity=request.procurement_quantity,
            offer_unit_price=validation.effective_unit_price,
            logistics=offer.forecast_logistics or 0,
            minimum_order=offer.minimum_order,
            order_multiple=offer.order_multiple,
        )
    if validation.technical_status == "❌ Не соответствует" or offer.stock_status == "Нет":
        safe_economy = "Заблокировано"
    elif landed is None or landed.total_cost is None:
        safe_economy = "Не рассчитано"
    elif landed.economy == "Невыгодно":
        safe_economy = "Невыгодно"
    elif validation.readiness != "Подтверждено":
        safe_economy = "Предварительно — подтвердить"
    else:
        safe_economy = landed.economy
    key = offer_observation_key(request, supplier, offer)
    properties: dict[str, Any] = {
        "Ключ наблюдения": key,
        "Предложение": title,
        "Товар поставщика": offer.supplier_item,
        "Модель / артикул": offer.model_article,
        "Фасовка / размер": (
            f"{offer.package_size} {offer.package_unit}" if offer.package_size is not None else ""
        ).strip(),
        "Количество в ценовой единице": _number(offer.package_size),
        "Единица содержимого": offer.package_unit or None,
        "Тип источника": offer.source_type,
        "Источник": offer.source_url,
        "Источник фото": offer.source_photo_url,
        "Дата проверки": offer.checked_at.isoformat(),
        "Цена предложения": _number(offer.price),
        "Единица цены": offer.price_unit,
        "Город поставщика": offer.supplier_city or None,
        "Цена и НДС": offer.vat_status,
        "Наличие": offer.stock_status,
        "Количество доступно": _number(offer.available_quantity),
        "Доставка подтверждена": offer.delivery_status,
        "Документы подтверждены": offer.documents_status,
        "Способ получения": offer.fulfillment_method,
        "Точка отгрузки / самовывоза": offer.pickup_point,
        "Срок / дата доступности": offer.availability_note,
        "Прогноз логистики": _number(offer.forecast_logistics),
        "Соответствие ТЗ": validation.technical_status,
        "Статус работы": validation.readiness,
        "Доказательства": offer.evidence,
        "Что подтвердить": confirmations,
        "Количество к закупке": _number(request.procurement_quantity),
        "Минимальный заказ": _number(offer.minimum_order),
        "Кратность заказа": _number(offer.order_multiple),
        "Расчётное количество покупки": _number(landed.purchase_quantity if landed else None),
        "Безопасная себестоимость прогноз": _number(landed.total_cost if landed else None),
        "Безопасная прибыль прогноз": _number(landed.profit if landed else None),
        "Безопасная прибыль, %": _number(landed.margin_percent if landed else None),
        "Безопасная экономика": safe_economy,
    }
    if request.notion_page_url:
        properties["Заявка"] = [request.notion_page_url]
    if supplier.notion_page_url:
        properties["Поставщик"] = [supplier.notion_page_url]
    return {
        "idempotency_key": key,
        "properties": properties,
    }
