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
