from datetime import UTC, datetime

import pytest

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    StaleBrowserResult,
    StaticBrowserExecutor,
    StructuredBrowserProvider,
    UnsafeBrowserResult,
)
from procurement_bot.research import (
    AvailabilityStatus,
    EvidenceClaim,
    EvidenceType,
    LocalityResearchTask,
    ResearchSource,
    SupplierCandidate,
    SupplierEvidence,
    build_research_plan,
)


def _plan(source: ResearchSource):
    request = ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.CITYWIDE,
        items=[ProcurementItem(name="Марля")],
    )
    return next(
        branch
        for branch in build_research_plan(request).source_plans
        if branch.source == source
    )


def _candidate(source: ResearchSource = ResearchSource.TWO_GIS) -> SupplierCandidate:
    return SupplierCandidate(
        source=source,
        external_id="firm-1",
        name="Медснаб",
        evidence=[
            SupplierEvidence(
                evidence_type=EvidenceType.BUSINESS_LISTING,
                source=source,
                source_url="https://2gis.kz/almaty/firm/1",
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="c" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY],
            )
        ],
    )


def test_prepare_is_pure_and_contains_safety_contract():
    executor = StaticBrowserExecutor({})
    provider = StructuredBrowserProvider(executor)
    call = provider.prepare_locality(
        LocalityResearchTask(city="Алматы", phrase="первая Алматы")
    )

    assert executor.calls == []
    assert call.spec_sha256 == BROWSER_SPEC_SHA256
    assert "untrusted data" in call.trusted_instruction
    assert "Do not send messages" in call.trusted_instruction
    assert "budget/tender price" in call.trusted_instruction


@pytest.mark.asyncio
async def test_fake_executor_round_trip_validates_hashes_and_listing_semantics():
    plan = _plan(ResearchSource.TWO_GIS)
    temporary = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = temporary.prepare_supplier_search(plan)
    executor = StaticBrowserExecutor(
        {
            "spec_sha256": call.spec_sha256,
            "request_sha256": call.request_sha256,
            "candidates": [_candidate().model_dump(mode="json")],
            "exhausted": True,
            "pages_examined": 2,
        }
    )
    provider = StructuredBrowserProvider(executor)

    result = await provider.search_suppliers(plan)

    assert len(executor.calls) == 1
    assert result.candidates[0].product_availability == AvailabilityStatus.UNKNOWN


def test_stale_result_is_rejected():
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    plan = _plan(ResearchSource.WEB)
    call = provider.prepare_supplier_search(plan)

    with pytest.raises(StaleBrowserResult, match="task changed"):
        provider.accept_supplier_search(
            call,
            plan,
            {
                "spec_sha256": call.spec_sha256,
                "request_sha256": "f" * 64,
                "candidates": [],
                "exhausted": False,
                "pages_examined": 1,
            },
        )


def test_browser_rejects_supplier_statement_even_if_domain_model_is_valid():
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    plan = _plan(ResearchSource.WEB)
    call = provider.prepare_supplier_search(plan)
    evidence = SupplierEvidence(
        evidence_type=EvidenceType.SUPPLIER_STATEMENT,
        source=ResearchSource.WEB,
        source_url="https://example.kz/product",
        observed_at=datetime.now(UTC),
        raw_snapshot_sha256="d" * 64,
        claims=[EvidenceClaim.PRODUCT_MATCH, EvidenceClaim.AVAILABILITY],
        availability=AvailabilityStatus.CONFIRMED_IN_STOCK,
    )
    candidate = SupplierCandidate(
        source=ResearchSource.WEB,
        name="Поставщик",
        evidence=[evidence],
        product_availability=AvailabilityStatus.CONFIRMED_IN_STOCK,
    )

    with pytest.raises(UnsafeBrowserResult, match="supplier statement"):
        provider.accept_supplier_search(
            call,
            plan,
            {
                "spec_sha256": call.spec_sha256,
                "request_sha256": call.request_sha256,
                "candidates": [candidate.model_dump(mode="json")],
                "exhausted": False,
                "pages_examined": 1,
            },
        )


def test_browser_adapter_does_not_accept_database_branch():
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    with pytest.raises(ValueError, match="PostgreSQL adapter"):
        provider.prepare_supplier_search(_plan(ResearchSource.OWN_DATABASE))
