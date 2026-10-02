import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    StaleBrowserResult,
    StaticBrowserExecutor,
    StructuredBrowserProvider,
    UnsafeBrowserResult,
)
from procurement_bot.providers.codex_chrome import CodexChromeExecutor
from procurement_bot.research import (
    AvailabilityStatus,
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    LocalityConstraint,
    LocalityResearchTask,
    ResearchPurpose,
    ResearchSource,
    SourcePlan,
    SupplierCandidate,
    SupplierContact,
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


def _plan_for_cursor_tests(source: ResearchSource) -> SourcePlan:
    if source != ResearchSource.OLX:
        return _plan(source)
    return SourcePlan(
        item_index=0,
        source=source,
        purpose=ResearchPurpose.PRODUCT_EVIDENCE,
        query_terms=["аппарат для сварки пластиковых труб"],
        rendered_query="аппарат для сварки пластиковых труб Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=1,
        result_ttl_hours=1,
        empty_page_limit=1,
    )
def _candidate(source: ResearchSource = ResearchSource.TWO_GIS) -> SupplierCandidate:
    return SupplierCandidate(
        source=source,
        external_id="1",
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
    assert "highway, market area" in call.trusted_instruction
    assert "not supplier search" in call.trusted_instruction


def test_runtime_plan_payload_hash_includes_only_execution_batch_fields():
    plan = _plan(ResearchSource.TWO_GIS).model_copy(update={
        "batch_candidate_limit": 2,
        "excluded_external_ids": ["listing-1"],
    })
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)

    request = json.loads(call.untrusted_request_json)
    assert request == plan.browser_payload()
    assert request["batch_candidate_limit"] == 2
    assert request["excluded_external_ids"] == ["listing-1"]
    assert hashlib.sha256(call.untrusted_request_json.encode()).hexdigest() == call.request_sha256
    assert "batch_candidate_limit" not in plan.model_dump(mode="json")


def test_search_cursor_is_runtime_only_but_is_pinned_into_provider_call():
    plan = _plan(ResearchSource.TWO_GIS).model_copy(update={
        "search_cursor": "https://2gis.kz/almaty/search/page/2?query=shops#results",
    })
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = provider.prepare_supplier_search(plan)

    assert "search_cursor" not in plan.model_dump(mode="json")
    assert plan.browser_payload()["search_cursor"] == (
        "https://2gis.kz/almaty/search/page/2?query=shops#results"
    )
    request = json.loads(call.untrusted_request_json)
    assert request["search_cursor"] == "https://2gis.kz/almaty/search/page/2?query=shops"
    assert call.request_sha256 == hashlib.sha256(
        call.untrusted_request_json.encode()
    ).hexdigest()


@pytest.mark.parametrize(("source", "cursor"), [
    (ResearchSource.TWO_GIS, "https://example.com/search?page=2"),
    (ResearchSource.OLX, "https://olx.ru/search?page=2"),
    (ResearchSource.WEB, "http://vendor.example/search?page=2"),
    (ResearchSource.WEB, "https://127.0.0.1/search?page=2"),
    (ResearchSource.WEB, "https://service.internal/search?page=2"),
    (ResearchSource.WEB, "https://user:pass@vendor.example/search?page=2"),
])
def test_search_cursor_rejects_invalid_or_nonpublic_hosts(source, cursor):
    plan = _plan_for_cursor_tests(source).model_copy(update={"search_cursor": cursor})

    with pytest.raises(UnsafeBrowserResult):
        StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)


def test_search_result_next_cursor_is_normalized_and_host_validated():
    plan = _plan_for_cursor_tests(ResearchSource.OLX)
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = provider.prepare_supplier_search(plan)
    base = {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "candidates": [],
        "exhausted": False,
        "pages_examined": 1,
        "next_cursor": "https://www.olx.kz/list/?page=2#offers",
    }

    result = provider.accept_supplier_search(call, plan, base)
    assert result.next_cursor == "https://www.olx.kz/list/?page=2"

    with pytest.raises(UnsafeBrowserResult, match="official olx.kz"):
        provider.accept_supplier_search(call, plan, {
            **base,
            "next_cursor": "https://example.com/page/2",
        })


def test_web_cursor_must_stay_on_approved_search_host():
    plan = _plan(ResearchSource.WEB)
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = provider.prepare_supplier_search(plan)
    base = {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "candidates": [],
        "exhausted": False,
        "pages_examined": 1,
    }

    assert provider.accept_supplier_search(call, plan, {
        **base,
        "next_cursor": "https://www.google.com/search?q=marble&start=10",
    }).next_cursor == "https://www.google.com/search?q=marble&start=10"
    with pytest.raises(UnsafeBrowserResult, match="current search host"):
        provider.accept_supplier_search(call, plan, {
            **base,
            "next_cursor": "https://vendor.example/products?page=2",
        })


def test_codex_batch_timeout_allows_300_seconds():
    executor = CodexChromeExecutor(
        executable=Path("/bin/true"),
        batch_timeout_seconds=300,
    )
    assert executor.batch_timeout_seconds == 300


def test_web_evidence_requires_public_https_url():
    plan = _plan(ResearchSource.WEB)
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    raw = {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "candidates": [{
            "source": "web",
            "external_id": "product-1",
            "name": "Product",
            "evidence": [{
                "evidence_type": "product_page",
                "source": "web",
                "source_url": "http://vendor.example/product-1",
                "observed_at": datetime.now(UTC).isoformat(),
                "raw_snapshot_sha256": "b" * 64,
                "claims": ["product_match"],
            }],
        }],
        "exhausted": False,
        "pages_examined": 1,
    }

    with pytest.raises(UnsafeBrowserResult, match="public HTTPS"):
        StructuredBrowserProvider(StaticBrowserExecutor({})).accept_supplier_search(
            call, plan, raw,
        )


def test_full_configured_batch_is_not_marked_exhausted():
    plan = _plan(ResearchSource.TWO_GIS).model_copy(update={"batch_candidate_limit": 2})
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = provider.prepare_supplier_search(plan)
    first = _candidate()
    second = first.model_copy(update={
        "external_id": "2",
        "evidence": [first.evidence[0].model_copy(update={
            "source_url": "https://2gis.kz/almaty/firm/2",
        })],
    })
    raw = {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "candidates": [first.model_dump(mode="json"), second.model_dump(mode="json")],
        "exhausted": True,
        "pages_examined": 1,
    }

    result = provider.accept_supplier_search(call, plan, raw)

    assert len(result.candidates) == 2
    assert result.exhausted is False


@pytest.mark.asyncio
async def test_codex_can_verify_locality_without_a_supplier_plan(monkeypatch):
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_locality(
        LocalityResearchTask(city="Алматы", phrase="Северное Кольцо")
    )
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    seen = []

    async def invoke(prepared, *, source, direct_firm_card):
        seen.append((prepared.operation, source, direct_firm_card))
        return {"spec_sha256": prepared.spec_sha256,
                "request_sha256": prepared.request_sha256,
                "candidates": [], "pages_examined": 1}

    monkeypatch.setattr(executor, "_invoke", invoke)
    result = await executor.execute(call)
    assert seen == [(call.operation, "2gis", False)]
    assert StructuredBrowserProvider(executor).accept_locality(call, result).pages_examined == 1


def test_empty_locality_without_page_inspection_is_an_operational_error():
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = provider.prepare_locality(LocalityResearchTask(city="Алматы", phrase="Алматы 1"))
    with pytest.raises(UnsafeBrowserResult, match="inspected no pages"):
        provider.accept_locality(call, {
            "spec_sha256": call.spec_sha256, "request_sha256": call.request_sha256,
            "candidates": [], "access_issue": "mcp_error",
        })


def test_2gis_instruction_routes_site_and_instagram_to_product_evidence():
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(
        _plan(ResearchSource.TWO_GIS)
    )

    assert "kind=website" in call.trusted_instruction
    assert "kind=instagram" in call.trusted_instruction
    assert "own product URL" in call.trusted_instruction
    assert "'from' price" in call.trusted_instruction


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


@pytest.mark.asyncio
async def test_zero_page_search_without_diagnostic_is_classified_for_fallback():
    plan = _plan(ResearchSource.TWO_GIS)
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    call = provider.prepare_supplier_search(plan)

    with pytest.raises(UnsafeBrowserResult) as caught:
        provider.accept_supplier_search(
            call,
            plan,
            {
                "spec_sha256": call.spec_sha256,
                "request_sha256": call.request_sha256,
                "candidates": [],
                "exhausted": False,
                "pages_examined": 0,
            },
        )

    assert caught.value.access_issue == "unknown"


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


def test_olx_phone_requires_contact_evidence_on_listing():
    provider = StructuredBrowserProvider(StaticBrowserExecutor({}))
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.OLX,
        purpose=ResearchPurpose.PRODUCT_EVIDENCE,
        query_terms=["аппарат для сварки пластиковых труб"],
        rendered_query="аппарат для сварки пластиковых труб Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=1,
        result_ttl_hours=1,
        empty_page_limit=1,
    )
    call = provider.prepare_supplier_search(plan)
    assert "click its public Show phone control" in call.trusted_instruction
    candidate = SupplierCandidate(
        source=ResearchSource.OLX,
        external_id="IDqzMzJ",
        name="Продавец",
        contacts=[SupplierContact(kind=ContactKind.PHONE, value="+87782435785", confidence=1)],
        evidence=[SupplierEvidence(
            evidence_type=EvidenceType.PRODUCT_PAGE,
            source=ResearchSource.OLX,
            source_url="https://www.olx.kz/d/obyavlenie/apparat-IDqzMzJ.html",
            observed_at=datetime.now(UTC),
            raw_snapshot_sha256="d" * 64,
            claims=[EvidenceClaim.PRODUCT_MATCH],
        )],
    )
    raw = {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "candidates": [candidate.model_dump(mode="json")],
        "exhausted": False,
        "pages_examined": 1,
    }
    with pytest.raises(UnsafeBrowserResult, match="OLX contact must cite"):
        provider.accept_supplier_search(call, plan, raw)
    raw["candidates"][0]["evidence"][0]["claims"].append(EvidenceClaim.CONTACT.value)
    assert provider.accept_supplier_search(call, plan, raw).candidates[0].contacts
