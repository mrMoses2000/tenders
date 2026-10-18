from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.research import (
    AvailabilityStatus,
    EvidenceClaim,
    EvidenceType,
    GeoPoint,
    LocalityCandidate,
    LocalityResolutionStatus,
    PrivatePriceLeakError,
    ResearchSource,
    SupplierCandidate,
    SupplierEvidence,
    build_research_plan,
    decide_locality_resolution,
    olx_listing_id_matches_url,
)


def _request(*, scope: SearchScope = SearchScope.CITYWIDE) -> ParsedProcurementRequest:
    return ParsedProcurementRequest(
        city="Алматы",
        search_scope=scope,
        search_area_text="на первой Алмате" if scope == SearchScope.SPECIFIC_AREA else None,
        items=[ProcurementItem(name="Марля медицинская", quantity=100, unit="м")],
    )


def _locality(name: str, confidence: float, place_id: str) -> LocalityCandidate:
    return LocalityCandidate(
        canonical_name=name,
        place_type="район",
        city="Алматы",
        source=ResearchSource.TWO_GIS,
        provider_place_id=place_id,
        point=GeoPoint(latitude=43.34, longitude=76.95),
        source_url=f"https://2gis.kz/almaty/geo/{place_id}",
        raw_snapshot_sha256="a" * 64,
        confidence=confidence,
    )


def test_plan_has_deterministic_source_order_and_hash():
    first = build_research_plan(_request(), synonyms_by_item={0: ["медицинская марля"]})
    second = build_research_plan(_request(), synonyms_by_item={0: ["медицинская марля"]})

    assert [branch.source for branch in first.source_plans] == [
        ResearchSource.OWN_DATABASE,
        ResearchSource.TWO_GIS,
        ResearchSource.WEB,
    ]
    assert first.plan_sha256 == second.plan_sha256
    assert all("Алматы" in branch.rendered_query for branch in first.source_plans)
    assert all(branch.blocked_reason is None for branch in first.source_plans)


def test_colloquial_area_blocks_supplier_search_until_resolved():
    plan = build_research_plan(_request(scope=SearchScope.SPECIFIC_AREA))

    assert plan.locality_task is not None
    assert plan.locality_task.phrase == "на первой Алмате"
    assert all(
        branch.blocked_reason == "locality_requires_resolution"
        for branch in plan.source_plans
    )


def test_high_confidence_locality_unblocks_without_inventing_radius():
    resolution = decide_locality_resolution(
        phrase="на первой Алмате",
        city="Алматы",
        candidates=[_locality("Алматы-1", 0.96, "station-area")],
    )
    plan = build_research_plan(
        _request(scope=SearchScope.SPECIFIC_AREA),
        locality_resolution=resolution,
    )

    assert resolution.status == LocalityResolutionStatus.RESOLVED
    assert plan.locality_task is None
    assert all(branch.blocked_reason is None for branch in plan.source_plans)
    assert plan.source_plans[0].locality.canonical_place_id == "station-area"
    assert "radius" not in plan.model_dump_json()


def test_ambiguous_locality_requires_one_question():
    result = decide_locality_resolution(
        phrase="Алматы-1",
        city="Алматы",
        candidates=[
            _locality("Вокзал Алматы-1", 0.88, "station"),
            _locality("Район Алматы-1", 0.82, "district"),
        ],
    )
    assert result.status == LocalityResolutionStatus.NEEDS_CLARIFICATION
    assert result.selected is None
    assert "Вокзал Алматы-1" in (result.clarification_question or "")
    assert "Район Алматы-1" in (result.clarification_question or "")


def test_private_tender_price_marker_is_rejected_from_query_terms():
    with pytest.raises(PrivatePriceLeakError):
        build_research_plan(
            _request(),
            synonyms_by_item={0: ["марля, тендерная цена 500 тенге"]},
        )


def test_olx_listing_identity_normalizes_host_case_escaped_path_and_trailing_slash():
    assert olx_listing_id_matches_url(
        "qZ9sJ",
        "HTTPS://WWW.OLX.KZ/d/obyavlenie/tool-IDqZ9%73J.html/?utm_source=test",
    )
    assert olx_listing_id_matches_url(
        "IDqZ9sJ",
        "https://olx.kz/d/obyavlenie/tool-IDqZ9sJ.html",
    )
    assert not olx_listing_id_matches_url(
        "qZ9sJ", "https://olx.kz.evil.example/d/obyavlenie/tool-IDqZ9sJ.html"
    )


def test_legacy_unbound_olx_seller_id_is_accepted_but_not_used_as_identity():
    legacy = SupplierCandidate.model_validate({
        "source": "olx",
        "external_id": "qZ9sJ",
        "name": "Seller",
        # Matches the old saved result shape: a generic token plus listing evidence only.
        "seller_profile_id": "1lYldf",
        "evidence": [{
            "evidence_type": "product_page",
            "source": "olx",
            "source_url": "https://www.olx.kz/d/obyavlenie/tool-IDqZ9sJ.html",
            "observed_at": datetime.now(UTC).isoformat(),
            "raw_snapshot_sha256": "a" * 64,
            "claims": ["product_match"],
        }],
    })

    assert legacy.external_id == "qZ9sJ"
    assert legacy.seller_profile_id is None
    assert legacy.seller_profile_url is None


def test_olx_profile_id_must_be_derived_from_cited_business_profile_url():
    listing_url = "https://olx.kz/d/obyavlenie/tool-IDqZ9sJ.html"
    profile_url = "HTTPS://WWW.OLX.KZ/Profile/User/%31lYldf/"
    candidate = SupplierCandidate(
        source=ResearchSource.OLX,
        external_id="qZ9sJ",
        name="Seller",
        seller_profile_id="1lYldf",
        seller_profile_url=profile_url,
        evidence=[
            SupplierEvidence(
                evidence_type=EvidenceType.PRODUCT_PAGE,
                source=ResearchSource.OLX,
                source_url=listing_url,
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="b" * 64,
                claims=[EvidenceClaim.PRODUCT_MATCH],
            ),
            SupplierEvidence(
                evidence_type=EvidenceType.SELLER_PROFILE,
                source=ResearchSource.OLX,
                source_url="https://olx.kz/profile/user/1lYldf?from=listing",
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="c" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY],
            ),
        ],
    )

    assert candidate.seller_profile_id == "1lYldf"

    with pytest.raises(ValidationError, match="cited by business identity evidence"):
        SupplierCandidate(
            source=ResearchSource.OLX,
            external_id="qZ9sJ",
            name="Seller",
            seller_profile_id="1lYldf",
            seller_profile_url=profile_url,
            evidence=[SupplierEvidence(
                evidence_type=EvidenceType.PRODUCT_PAGE,
                source=ResearchSource.OLX,
                source_url=listing_url,
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="d" * 64,
                claims=[EvidenceClaim.PRODUCT_MATCH],
            )],
        )

    with pytest.raises(ValidationError, match="differs from its source URL"):
        SupplierCandidate(
            source=ResearchSource.OLX,
            external_id="qZ9sJ",
            name="Seller",
            seller_profile_id="different1",
            seller_profile_url=profile_url,
            evidence=[
                SupplierEvidence(
                    evidence_type=EvidenceType.PRODUCT_PAGE,
                    source=ResearchSource.OLX,
                    source_url=listing_url,
                    observed_at=datetime.now(UTC),
                    raw_snapshot_sha256="d" * 64,
                    claims=[EvidenceClaim.PRODUCT_MATCH],
                ),
                SupplierEvidence(
                    evidence_type=EvidenceType.SELLER_PROFILE,
                    source=ResearchSource.OLX,
                    source_url=profile_url,
                    observed_at=datetime.now(UTC),
                    raw_snapshot_sha256="e" * 64,
                    claims=[EvidenceClaim.BUSINESS_IDENTITY],
                ),
            ],
        )

    with pytest.raises(ValidationError, match="recognized OLX profile URL"):
        SupplierCandidate(
            source=ResearchSource.OLX,
            external_id="qZ9sJ",
            name="Seller",
            seller_profile_url="https://example.org/profile/user/1lYldf",
            evidence=[SupplierEvidence(
                evidence_type=EvidenceType.SELLER_PROFILE,
                source=ResearchSource.OLX,
                source_url="https://example.org/profile/user/1lYldf",
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="e" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY],
            )],
        )


def test_business_listing_cannot_claim_product_stock_or_price():
    with pytest.raises(ValidationError, match="business listing cannot prove"):
        SupplierEvidence(
            evidence_type=EvidenceType.BUSINESS_LISTING,
            source=ResearchSource.TWO_GIS,
            source_url="https://2gis.kz/almaty/firm/1",
            observed_at=datetime.now(UTC),
            raw_snapshot_sha256="b" * 64,
            claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.AVAILABILITY],
            availability=AvailabilityStatus.CONFIRMED_IN_STOCK,
            price_amount=Decimal("500"),
            currency="KZT",
        )


def test_candidate_from_business_listing_keeps_stock_unknown():
    evidence = SupplierEvidence(
        evidence_type=EvidenceType.BUSINESS_LISTING,
        source=ResearchSource.TWO_GIS,
        source_url="https://2gis.kz/almaty/firm/1",
        observed_at=datetime.now(UTC),
        raw_snapshot_sha256="b" * 64,
        claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.ADDRESS],
    )
    candidate = SupplierCandidate(
        source=ResearchSource.TWO_GIS,
        external_id="1",
        name="Медснаб",
        evidence=[evidence],
    )
    assert candidate.product_availability == AvailabilityStatus.UNKNOWN
