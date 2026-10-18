from __future__ import annotations

import hashlib

from .models import SupplierIdentity
from .normalization import canonical_domain, normalize_phone, normalize_text

_SHARED_PLATFORM_DOMAINS = {
    "2gis.kz",
    "goszakup.gov.kz",
    "instagram.com",
    "kaspi.kz",
    "olx.kz",
}


def supplier_match_keys(supplier: SupplierIdentity) -> tuple[str, ...]:
    keys: list[str] = []
    domain = canonical_domain(supplier.site_url)
    phone = normalize_phone(supplier.phone)
    bin_iin = "".join(ch for ch in supplier.bin_iin if ch.isdigit())
    if bin_iin:
        keys.append("bin:" + bin_iin)
    if phone:
        keys.append("phone:" + phone)
    if domain and domain not in _SHARED_PLATFORM_DOMAINS:
        keys.append("domain:" + domain)
    name_city = f"{normalize_text(supplier.name)}|{normalize_text(supplier.city)}"
    keys.append("name-city:" + name_city)
    return tuple(keys)


def supplier_key(supplier: SupplierIdentity) -> str:
    identity = (
        ("stable:" + supplier.stable_id.strip())
        if supplier.stable_id.strip()
        else ("notion:" + supplier.notion_page_url.strip())
        if supplier.notion_page_url.strip()
        else "name-city:"
        + f"{normalize_text(supplier.name)}|{normalize_text(supplier.city)}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"supplier:{digest}"


def dedupe_suppliers(suppliers: list[SupplierIdentity]) -> list[list[SupplierIdentity]]:
    """Group suppliers when any strong or fallback identity key overlaps."""
    groups: list[list[SupplierIdentity]] = []
    group_keys: list[set[str]] = []
    for supplier in suppliers:
        keys = set(supplier_match_keys(supplier))
        matches = [i for i, existing in enumerate(group_keys) if keys & existing]
        if not matches:
            groups.append([supplier])
            group_keys.append(keys)
            continue
        target = matches[0]
        groups[target].append(supplier)
        group_keys[target].update(keys)
        for index in reversed(matches[1:]):
            groups[target].extend(groups.pop(index))
            group_keys[target].update(group_keys.pop(index))
    return groups
