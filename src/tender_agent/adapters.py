from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .extractor import ExtractedProduct
from .normalization import canonical_domain


@dataclass(frozen=True)
class SourceStrategy:
    source_type: str
    adapter: str
    programmatic_fields: tuple[str, ...]
    browser_capable: bool
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class EscalationPlan:
    source_type: str
    adapter: str
    first_step: str
    missing_fields: tuple[str, ...]
    browser_required: bool
    computer_use_required: bool
    supplier_contact_required: bool
    reason: str


GENERIC = SourceStrategy(
    "Сайт",
    "generic-html",
    ("product_name", "description", "sku", "brand", "image_url", "price", "currency", "availability_signal"),
    True,
    ("Dynamic price or variants may be absent from HTML", "Public stock is not supplier confirmation"),
)

STRATEGIES: tuple[tuple[tuple[str, ...], SourceStrategy], ...] = (
    (("kaspi.kz",), SourceStrategy(
        "Kaspi", "kaspi-public", ("product_name", "image_url", "price", "sku", "availability_signal"), True,
        ("Seller/city/variant data may be dynamic", "Displayed availability is not a reserved quantity"),
    )),
    (("olx.kz", "olx.com"), SourceStrategy(
        "OLX", "olx-public", ("product_name", "description", "image_url", "price", "currency"), True,
        ("Individual ads expire", "Seller answer is required for quantity and documents"),
    )),
    (("2gis.kz", "2gis.com"), SourceStrategy(
        "2ГИС", "2gis-business", ("product_name", "description", "image_url"), True,
        ("Primary value is company identity/location", "Product price and stock usually need another source or contact"),
    )),
    (("instagram.com",), SourceStrategy(
        "Instagram", "instagram-public", ("product_name", "description", "image_url"), True,
        ("Public metadata is often incomplete", "Login or messaging must not be bypassed"),
    )),
)


def strategy_for_url(url: str) -> SourceStrategy:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("source URL must use http or https")
    domain = canonical_domain(url)
    for suffixes, strategy in STRATEGIES:
        if any(domain == suffix or domain.endswith("." + suffix) for suffix in suffixes):
            return strategy
    return GENERIC


def plan_source_escalation(
    url: str,
    extracted: ExtractedProduct | None = None,
    *,
    required_fields: tuple[str, ...] = ("product_name", "price"),
    browser_already_failed: bool = False,
    visual_interaction_required: bool = False,
) -> EscalationPlan:
    strategy = strategy_for_url(url)
    missing = tuple(
        field for field in required_fields
        if extracted is None or not str(getattr(extracted, field, "") or "").strip()
    )
    supplier_only = {"confirmed_stock", "confirmed_quantity", "documents", "delivery_quote", "vat"}
    supplier_contact = any(field in supplier_only for field in required_fields)
    browser_missing = tuple(field for field in missing if field not in supplier_only)
    browser_required = bool(browser_missing and strategy.browser_capable and not browser_already_failed)
    computer_required = bool(
        visual_interaction_required and browser_already_failed and browser_missing
    )

    if extracted is None:
        first_step = "http-extractor"
        reason = "Start with bounded public HTML extraction"
    elif not missing:
        first_step = "agent-validation"
        reason = "Required public fields are already extracted"
    elif browser_required:
        first_step = "chrome"
        reason = "Required public fields are missing and may be rendered dynamically"
    elif computer_required:
        first_step = "computer-use"
        reason = "Visual interaction is required after semantic browser extraction failed"
    elif supplier_contact:
        first_step = "draft-contact"
        reason = "Confirmation cannot be proven from a public page"
    else:
        first_step = "record-partial"
        reason = "Source remains incomplete; keep an explicit partial result"

    return EscalationPlan(
        source_type=strategy.source_type,
        adapter=strategy.adapter,
        first_step=first_step,
        missing_fields=missing,
        browser_required=browser_required,
        computer_use_required=computer_required,
        supplier_contact_required=supplier_contact,
        reason=reason,
    )
