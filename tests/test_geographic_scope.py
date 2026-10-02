from __future__ import annotations

import pytest

from procurement_bot.geographic_scope import GeographicDecision, decide_geographic_scope
from procurement_bot.intake import SearchScope
from procurement_bot.research import GeoPoint, LocalityConstraint


@pytest.mark.parametrize(
    "address",
    [
        "Северное Кольцо шоссе, 36, Алматы",
        "шоссе Северное Кольцо, дом 36",
        "г. Алматы, ул. Северное Кольцо, 36",
    ],
)
def test_specific_area_accepts_normalized_area_tokens(address: str) -> None:
    locality = LocalityConstraint(
        city="Алматы",
        scope=SearchScope.SPECIFIC_AREA,
        raw_area_text="шоссе Северное Кольцо",
    )

    assert decide_geographic_scope(locality, address_text=address) == (
        GeographicDecision.ACCEPTED_BY_ADDRESS
    )


def test_specific_area_does_not_infer_a_radius_or_match_generic_street_words() -> None:
    locality = LocalityConstraint(
        city="Алматы",
        scope=SearchScope.SPECIFIC_AREA,
        raw_area_text="Северное Кольцо",
    )

    assert decide_geographic_scope(
        locality,
        address_text="улица Северная, дом 36",
        point=GeoPoint(latitude=43.25, longitude=76.90),
    ) == GeographicDecision.UNKNOWN
    assert decide_geographic_scope(
        locality,
        address_text=None,
        point=GeoPoint(latitude=43.25, longitude=76.90),
    ) == GeographicDecision.UNKNOWN


def test_specific_area_reports_conflicting_city_without_rejecting_the_lead() -> None:
    locality = LocalityConstraint(
        city="Алматы",
        scope=SearchScope.SPECIFIC_AREA,
        raw_area_text="Северное Кольцо",
    )

    assert decide_geographic_scope(
        locality,
        address_city="Астана",
        address_text="Северное Кольцо шоссе, 36",
    ) == GeographicDecision.OUTSIDE_AREA


def test_citywide_needs_matching_or_provider_selected_city() -> None:
    locality = LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE)

    assert decide_geographic_scope(locality, address_city="г. Алматы") == (
        GeographicDecision.CITY_VERIFIED
    )
    assert decide_geographic_scope(
        locality, provider_selected_city="Алматы"
    ) == GeographicDecision.CITY_VERIFIED
    assert decide_geographic_scope(locality, address_city="Астана") == (
        GeographicDecision.OUTSIDE_AREA
    )
    assert decide_geographic_scope(locality) == GeographicDecision.UNKNOWN


def test_online_delivery_has_no_geographic_scope_decision() -> None:
    locality = LocalityConstraint(city="Алматы", scope=SearchScope.ONLINE_WITH_DELIVERY)

    assert decide_geographic_scope(locality) == GeographicDecision.NOT_APPLICABLE
