from datetime import UTC, datetime

import pytest

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import SupplierBrowserResult, UnsafeBrowserResult
from procurement_bot.providers.staged_research import (
    _card_candidate_id_matches,
    canonicalize_exact_card_result,
)
from procurement_bot.research import (
    EvidenceClaim,
    EvidenceType,
    ResearchSource,
    SupplierCandidate,
    SupplierEvidence,
    build_research_plan,
)


def _sample(url: str):
    plan = build_research_plan(ParsedProcurementRequest(
        city="Алматы", search_scope=SearchScope.CITYWIDE,
        items=[ProcurementItem(name="Валик", quantity=5, unit="шт")],
    ), include_olx=True)
    branch = next(p for p in plan.source_plans if p.source == ResearchSource.OLX)
    branch = branch.model_copy(update={
        "rendered_query": "https://www.olx.kz/d/obyavlenie/instrument-IDqZ9sJ.html",
    })
    result = SupplierBrowserResult(
        spec_sha256="a" * 64, request_sha256="b" * 64, exhausted=False,
        pages_examined=1,
        candidates=[SupplierCandidate(
            source=ResearchSource.OLX, external_id="398759121", name="RA",
            evidence=[SupplierEvidence(
                source=ResearchSource.OLX, evidence_type=EvidenceType.PRODUCT_PAGE,
                source_url=url, observed_at=datetime.now(UTC),
                raw_snapshot_sha256="c" * 64,
                claims=[EvidenceClaim.CONTACT],
            )],
        )],
    )
    return branch, result


def test_olx_numeric_display_id_uses_observed_exact_url_slug():
    plan, result = _sample("https://www.olx.kz/d/obyavlenie/instrument-IDqZ9sJ.html")
    normalized = canonicalize_exact_card_result(plan, result)
    assert normalized.candidates[0].external_id == "qZ9sJ"
    assert result.candidates[0].external_id == "398759121"


def test_olx_legacy_prefixed_id_is_canonicalized_to_bound_url_suffix():
    plan, result = _sample("https://www.olx.kz/d/obyavlenie/instrument-IDqZ9sJ.html")
    result = result.model_copy(update={
        "candidates": [result.candidates[0].model_copy(update={"external_id": "IDqZ9sJ"})],
    })

    normalized = canonicalize_exact_card_result(plan, result)

    assert normalized.candidates[0].external_id == "qZ9sJ"


def test_cached_legacy_card_id_matches_candidate_by_bound_listing_url():
    plan, result = _sample("https://www.olx.kz/d/obyavlenie/instrument-IDqZ9sJ.html")
    candidate = result.candidates[0].model_copy(update={"external_id": "qZ9sJ"})

    assert _card_candidate_id_matches(candidate, {
        "external_id": "IDqZ9sJ",
        "card_url": plan.rendered_query,
    })
    assert not _card_candidate_id_matches(candidate.model_copy(update={
        "external_id": "wrong1",
    }), {
        "external_id": "IDqZ9sJ",
        "card_url": plan.rendered_query,
    })


def test_other_card_evidence_cannot_rebind_numeric_id():
    plan, result = _sample("https://www.olx.kz/d/obyavlenie/other-IDabc123.html")
    with pytest.raises(UnsafeBrowserResult, match="exact card evidence"):
        canonicalize_exact_card_result(plan, result)
