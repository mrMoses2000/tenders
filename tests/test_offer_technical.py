from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from procurement_bot.offer_technical import (
    TechnicalAssessmentClaim,
    TechnicalAssessmentProposal,
    TechnicalReviewConflict,
    enqueue_technical_assessment,
    load_technical_assessment_snapshot,
    persist_technical_assessment_proposal,
    propose_technical_assessment,
    technical_assessment_context_hash,
    technical_states,
)


class AssessmentExtractor:
    def __init__(self, claims: list[dict[str, Any]] | None = None) -> None:
        self.claims = claims or []
        self.calls: list[dict[str, Any]] = []

    async def extract(
        self,
        text: str,
        output_model: type[BaseModel],
        *,
        context: Any = None,
        spec_sha256: str = "",
        context_sha256: str = "",
    ) -> BaseModel:
        self.calls.append({"text": text, "context": context})
        return output_model.model_validate(
            {
                "spec_sha256": spec_sha256,
                "context_sha256": context_sha256,
                "claims": self.claims,
            }
        )


@pytest.fixture
def requirements() -> list[dict[str, object]]:
    return [
        {
            "id": "req-1",
            "requirement_key": "width",
            "operator": "minimum",
            "expected_value": {"value": 10, "unit": "cm"},
            "is_hard": True,
            "source_pointer": "p. 2",
        },
        {
            "id": "req-2",
            "requirement_key": "material",
            "operator": "equals",
            "expected_value": {"value": "cotton", "unit": None},
            "is_hard": True,
            "source_pointer": "p. 2",
        },
    ]


@pytest.fixture
def evidence() -> list[dict[str, object]]:
    return [
        {
            "id": "evidence-1",
            "source_type": "supplier_message",
            "source_locator": "supplier-message:42",
            "title": "Supplier reply",
            "text": "Our model X has width 12 cm. Material: polyester.",
        }
    ]


@pytest.mark.asyncio
async def test_assessment_is_a_cited_operator_proposal(requirements, evidence) -> None:
    extractor = AssessmentExtractor(
        [
            {
                "requirement_id": "req-1",
                "verdict": "match",
                "source_evidence_id": "evidence-1",
                "evidence_quote": "width 12 cm",
                "note": "The seller states the width is 12 cm.",
            },
            {
                "requirement_id": "req-2",
                "verdict": "mismatch",
                "source_evidence_id": "evidence-1",
                "evidence_quote": "Material: polyester.",
                "note": "The seller identifies polyester, not cotton.",
            },
        ]
    )

    proposal = await propose_technical_assessment(
        extractor, offer_id="offer-1", requirements=requirements, evidence=evidence
    )

    assert proposal.status == "operator_review_required"
    assert [claim.verdict for claim in proposal.claims] == ["match", "mismatch"]
    assert proposal.claims[0].evidence_quote == "width 12 cm"
    assert extractor.calls
    offer = {"offer_id": "offer-1", "request_item_id": "item-1", "status": "exact"}
    reqs = [dict(row, request_item_id="item-1") for row in requirements]
    assert technical_states([offer], reqs, {})["offer-1"] == "needs_confirmation"


@pytest.mark.asyncio
async def test_product_page_web_excerpt_can_support_a_proposal(requirements, evidence) -> None:
    extractor = AssessmentExtractor([
        {
            "requirement_id": "req-1", "verdict": "match",
            "source_evidence_id": "evidence-1", "evidence_quote": "width 12 cm",
            "note": "Product page states 12 cm.",
        },
        {"requirement_id": "req-2", "verdict": "unknown", "note": "Material is unclear."},
    ])
    page = [{**evidence[0], "source_type": "web"}]
    proposal = await propose_technical_assessment(
        extractor, offer_id="offer-1", requirements=requirements, evidence=page,
    )
    assert proposal.claims[0].source_evidence_id == "evidence-1"
    assert proposal.status == "operator_review_required"


@pytest.mark.asyncio
async def test_missing_evidence_returns_unknown_without_calling_model(requirements) -> None:
    extractor = AssessmentExtractor()

    proposal = await propose_technical_assessment(
        extractor, offer_id="offer-1", requirements=requirements, evidence=[]
    )

    assert [claim.verdict for claim in proposal.claims] == ["unknown", "unknown"]
    assert all(claim.source_evidence_id is None for claim in proposal.claims)
    assert extractor.calls == []


@pytest.mark.asyncio
async def test_non_verbatim_quote_is_rejected(requirements, evidence) -> None:
    extractor = AssessmentExtractor(
        [
            {
                "requirement_id": "req-1",
                "verdict": "match",
                "source_evidence_id": "evidence-1",
                "evidence_quote": "width is 12 cm",
                "note": "The seller states the width is 12 cm.",
            },
            {
                "requirement_id": "req-2",
                "verdict": "unknown",
                "note": "The evidence is unclear.",
            },
        ]
    )

    with pytest.raises(ValueError, match="not verbatim"):
        await propose_technical_assessment(
            extractor, offer_id="offer-1", requirements=requirements, evidence=evidence
        )


@pytest.mark.asyncio
async def test_incomplete_requirements_and_non_product_sources_fail_closed(
    requirements, evidence
) -> None:
    with pytest.raises(ValueError, match="supplier or product evidence"):
        await propose_technical_assessment(
            AssessmentExtractor(),
            offer_id="offer-1",
            requirements=requirements,
            evidence=[{**evidence[0], "source_type": "2gis"}],
        )

    extractor = AssessmentExtractor(
        [
            {
                "requirement_id": "req-1",
                "verdict": "unknown",
                "note": "No evidence about this requirement.",
            }
        ]
    )
    with pytest.raises(ValueError, match="cover every requirement"):
        await propose_technical_assessment(
            extractor, offer_id="offer-1", requirements=requirements, evidence=evidence
        )


def test_claim_schema_does_not_allow_final_verified_verdict() -> None:
    with pytest.raises(ValueError):
        TechnicalAssessmentClaim(
            requirement_id="req-1", verdict="verified", note="Final decision"
        )


def test_context_hash_changes_with_requirement_or_offer_evidence(requirements, evidence) -> None:
    initial = technical_assessment_context_hash("offer-1", requirements, evidence)
    changed = technical_assessment_context_hash(
        "offer-1", requirements, [{**evidence[0], "text": "Model X is 9 cm wide."}]
    )

    assert initial != changed


class _FakeConnection:
    def __init__(
        self, *, authorized: bool = True, requirements_present: bool = True,
    ) -> None:
        self.authorized = authorized
        self.requirements_present = requirements_present
        self.fetch_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_queries: list[tuple[str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetch_queries.append((query, args))
        if "FROM offers o" in query:
            if not self.authorized:
                return None
            return {
                "request_item_id": UUID("00000000-0000-0000-0000-000000000002"),
                "specification_text": "Hammer, steel head, 500 g",
                "has_requirements": self.requirements_present,
                "owner_user_id": UUID("00000000-0000-0000-0000-000000000003"),
                "status": "evaluating",
            }
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_queries.append((query, args))
        if "FROM item_requirements" in query:
            if not self.requirements_present:
                return []
            return [
                {
                    "id": UUID("00000000-0000-0000-0000-000000000004"),
                    "requirement_key": "width",
                    "operator": "minimum",
                    "expected_value": {"value": 10, "unit": "cm"},
                    "is_hard": True,
                    "source_pointer": "p. 1",
                }
            ]
        return [
            {
                "id": UUID("00000000-0000-0000-0000-000000000005"),
                "source_type": "supplier_message",
                "source_locator": "supplier-message:12",
                "title": "Supplier reply",
                "raw_payload": {"text": "Model X is 12 cm wide."},
            }
        ]

    async def fetchval(self, query: str, *args: Any) -> UUID | None:
        self.fetchval_queries.append((query, args))
        return UUID("00000000-0000-0000-0000-000000000006")


@pytest.mark.asyncio
async def test_persist_assessment_passes_json_array_to_pool_codec() -> None:
    class _WriteConnection:
        async def execute(self, query: str, *args: Any) -> str:
            assert "claims=$5::jsonb" in query
            assert isinstance(args[4], list)
            assert args[4][0]["verdict"] == "unknown"
            return "UPDATE 1"

    proposal = TechnicalAssessmentProposal(
        offer_id="00000000-0000-0000-0000-000000000007",
        claims=(TechnicalAssessmentClaim(
            requirement_id="req-1",
            verdict="unknown",
            note="No product evidence.",
        ),),
        spec_sha256="a" * 64,
        context_sha256="b" * 64,
    )
    assert await persist_technical_assessment_proposal(
        _WriteConnection(),
        proposal_id=UUID("00000000-0000-0000-0000-000000000006"),
        case_id=UUID("00000000-0000-0000-0000-000000000001"),
        owner_user_id=UUID("00000000-0000-0000-0000-000000000003"),
        proposal=proposal,
        stale=False,
    )


@pytest.mark.asyncio
async def test_snapshot_loads_quoteable_evidence_only_through_selected_offer() -> None:
    connection = _FakeConnection()
    snapshot = await load_technical_assessment_snapshot(
        connection,
        case_id=UUID("00000000-0000-0000-0000-000000000001"),
        offer_id=UUID("00000000-0000-0000-0000-000000000007"),
        owner_user_id=UUID("00000000-0000-0000-0000-000000000003"),
    )

    assert snapshot.evidence[0]["text"] == "Model X is 12 cm wide."
    evidence_query = next(
        query for query, _ in connection.fetch_queries if "offer_observations" in query
    )
    assert "observation.offer_id=$1" in evidence_query
    assert "source_evidence" in evidence_query


@pytest.mark.asyncio
async def test_imported_spec_without_requirements_stays_one_whole_spec_advisory() -> None:
    connection = _FakeConnection(requirements_present=False)
    snapshot = await load_technical_assessment_snapshot(
        connection,
        case_id=UUID("00000000-0000-0000-0000-000000000001"),
        offer_id=UUID("00000000-0000-0000-0000-000000000007"),
        owner_user_id=UUID("00000000-0000-0000-0000-000000000003"),
    )
    requirement = snapshot.requirements[0]
    assert len(snapshot.requirements) == 1
    assert requirement["scope"] == "whole_specification"
    assert requirement["expected_value"] == "Hammer, steel head, 500 g"
    assert str(requirement["id"]).startswith("whole-spec:")

    extractor = AssessmentExtractor([{
        "requirement_id": requirement["id"],
        "verdict": "unknown",
        "note": "The available message does not establish every specification detail.",
    }])
    proposal = await propose_technical_assessment(
        extractor,
        offer_id=snapshot.offer_id,
        requirements=snapshot.requirements,
        evidence=snapshot.evidence,
    )
    assert len(proposal.claims) == 1
    assert proposal.claims[0].verdict == "unknown"
    context_requirement = extractor.calls[0]["context"]["requirements"][0]
    assert context_requirement["scope"] == "whole_specification"
    assert (
        "do not split it into invented atomic requirements"
        in extractor.calls[0]["context"]["task"]
    )


@pytest.mark.asyncio
async def test_assessment_enqueue_requires_case_owner_authorization() -> None:
    connection = _FakeConnection(authorized=False)

    with pytest.raises(TechnicalReviewConflict, match="not editable by owner"):
        await enqueue_technical_assessment(
            connection,
            case_id=UUID("00000000-0000-0000-0000-000000000001"),
            offer_id=UUID("00000000-0000-0000-0000-000000000007"),
            owner_user_id=UUID("00000000-0000-0000-0000-000000000003"),
            request_key="request-1",
        )

    assert connection.fetchval_queries == []


@pytest.mark.asyncio
async def test_owner_can_enqueue_idempotent_assessment_job() -> None:
    connection = _FakeConnection(requirements_present=False)

    proposal_id = await enqueue_technical_assessment(
        connection,
        case_id=UUID("00000000-0000-0000-0000-000000000001"),
        offer_id=UUID("00000000-0000-0000-0000-000000000007"),
        owner_user_id=UUID("00000000-0000-0000-0000-000000000003"),
        request_key="request-1",
    )

    assert proposal_id == UUID("00000000-0000-0000-0000-000000000006")
    job_insert = next(
        query for query, _ in connection.fetchval_queries if "INSERT INTO jobs" in query
    )
    assert "assess_offer_technical" in str(
        next(args for query, args in connection.fetchval_queries if query == job_insert)
    )
    offer_query = next(query for query, _ in connection.fetch_queries if "FROM offers o" in query)
    assert "i.specification_text" in offer_query
