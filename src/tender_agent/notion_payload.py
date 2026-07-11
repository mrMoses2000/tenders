from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from .dedupe import supplier_key
from .models import CandidateOffer, RequestLine, SupplierIdentity, ValidationResult
from .normalization import canonical_domain


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def offer_observation_key(
    request: RequestLine,
    supplier: SupplierIdentity,
    offer: CandidateOffer,
) -> str:
    source = canonical_domain(offer.source_url) or offer.source_type.casefold()
    identity = "|".join(
        [request.request_id, supplier_key(supplier), source, offer.source_url, offer.checked_at.isoformat()]
    )
    return "offer:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def pack_notion_offer(
    request: RequestLine,
    supplier: SupplierIdentity,
    offer: CandidateOffer,
    validation: ValidationResult,
) -> dict[str, Any]:
    """Create a deterministic, transport-neutral payload. No Notion writes happen here."""
    title = f"{request.request_id} · {supplier.name} · {offer.supplier_item}"
    confirmations = "; ".join(validation.confirmations)
    return {
        "idempotency_key": offer_observation_key(request, supplier, offer),
        "properties": {
            "Предложение": title,
            "Товар поставщика": offer.supplier_item,
            "Модель / артикул": offer.model_article,
            "Фасовка / размер": (
                f"{offer.package_size} {offer.package_unit}" if offer.package_size is not None else ""
            ).strip(),
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
        },
    }
