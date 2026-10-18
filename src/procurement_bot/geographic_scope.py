from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from procurement_bot.intake import SearchScope
from procurement_bot.research import GeoPoint, LocalityConstraint


class GeographicDecision(StrEnum):
    CITY_VERIFIED = "city_verified"
    ACCEPTED_BY_ADDRESS = "accepted_by_address"
    OUTSIDE_AREA = "outside_area"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


_GENERIC_ADDRESS_TOKENS = frozenset({
    "г", "город", "улица", "ул", "шоссе", "проспект", "просп", "пр", "бульвар",
    "бул", "переулок", "пер", "микрорайон", "мкр", "район", "д", "дом",
})


def _tokens(value: str | None) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return tuple(
        token
        for token in re.findall(r"[0-9a-zа-я]+", normalized)
        if token not in _GENERIC_ADDRESS_TOKENS
    )


def _contains_ordered_tokens(container: tuple[str, ...], target: tuple[str, ...]) -> bool:
    if not target or len(target) > len(container):
        return False
    return any(
        container[index : index + len(target)] == target
        for index in range(len(container) - len(target) + 1)
    )


def decide_geographic_scope(
    locality: LocalityConstraint,
    *,
    address_city: str | None = None,
    address_text: str | None = None,
    point: GeoPoint | None = None,
    provider_selected_city: str | None = None,
) -> GeographicDecision:
    """Classify scope from explicit locality evidence without inventing geofences.

    A point alone is insufficient for specific-area classification because the approved
    plan does not provide a polygon. ``provider_selected_city`` is accepted only for a
    citywide search where the provider explicitly selected that city.
    """
    if locality.scope == SearchScope.ONLINE_WITH_DELIVERY:
        return GeographicDecision.NOT_APPLICABLE

    city_tokens = _tokens(locality.city)
    observed_city = _tokens(address_city)
    city_matches = bool(city_tokens and observed_city == city_tokens)
    if observed_city and not city_matches:
        return GeographicDecision.OUTSIDE_AREA

    if locality.scope == SearchScope.CITYWIDE:
        if city_matches or _tokens(provider_selected_city) == city_tokens:
            return GeographicDecision.CITY_VERIFIED
        # Coordinates without an approved city boundary cannot prove containment.
        _ = point
        return GeographicDecision.UNKNOWN

    if locality.scope == SearchScope.SPECIFIC_AREA:
        area_tokens = _tokens(locality.raw_area_text or locality.canonical_place_name)
        address_tokens = _tokens(address_text)
        if _contains_ordered_tokens(address_tokens, area_tokens):
            return GeographicDecision.ACCEPTED_BY_ADDRESS
        # There is no area polygon, so a non-matching/missing address is unknown.
        _ = point
        return GeographicDecision.UNKNOWN

    return GeographicDecision.UNKNOWN
