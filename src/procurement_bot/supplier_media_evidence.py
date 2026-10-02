"""Keep immutable supplier originals separate from derived readings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def supplier_evidence_text(original: str, row: Mapping[str, Any]) -> str:
    derived = row.get("media_extracted_text")
    if row.get("media_state") != "ready" or not derived:
        return original
    return original + "\n\n" + str(derived)


def media_clarification(row: Mapping[str, Any], *, language: str) -> str | None:
    if not row.get("media_kind"):
        return None
    if row.get("media_state") not in {"ready", "failed"}:
        return None
    if not row.get("media_needs_clarification") and row.get("media_state") != "failed":
        return None
    if language == "kk":
        return (
            "Рақмет! Мәліметті нақтылау үшін тауардың атауын, сипаттамасын, бағасын, "
            "валютасын, бағаның өлшем бірлігін және қолда бар санын мәтінмен жазыңызшы."
        )
    return (
        "Спасибо! Чтобы точно учесть ваше предложение, напишите, пожалуйста, текстом "
        "название и характеристики товара, цену, валюту, за какую единицу указана цена "
        "и доступное количество."
    )
