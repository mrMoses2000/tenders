"""Evidence-backed operator checks for a supplier's specific proposed product."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from procurement_bot.providers.agy import StructuredExtractor
from procurement_bot.queue import enqueue_job, stable_key


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TechnicalAssessmentClaim(_StrictModel):
    """Untrusted AI suggestion about one atomic requirement."""

    requirement_id: str = Field(min_length=1, max_length=100)
    verdict: Literal["match", "mismatch", "unknown"] = Field(
        description="Proposal only; use unknown when evidence does not explicitly settle it."
    )
    source_evidence_id: str | None = Field(
        default=None, description="ID of the supplied supplier message or product evidence."
    )
    evidence_quote: str | None = Field(
        default=None,
        max_length=1000,
        description="Verbatim, contiguous excerpt from that evidence; never paraphrase it.",
    )
    note: str = Field(min_length=1, max_length=1000)


class _TechnicalAssessmentOutput(_StrictModel):
    """Schema returned by AGY; hashes bind the proposal to this exact input."""

    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claims: list[TechnicalAssessmentClaim] = Field(max_length=100)


@dataclass(frozen=True, slots=True)
class TechnicalAssessmentProposal:
    """AI suggestions that always require a separate operator review."""

    offer_id: str
    claims: tuple[TechnicalAssessmentClaim, ...]
    spec_sha256: str
    context_sha256: str
    status: Literal["operator_review_required"] = "operator_review_required"


@dataclass(frozen=True, slots=True)
class TechnicalAssessmentSnapshot:
    case_id: UUID
    request_item_id: UUID
    offer_id: UUID
    owner_user_id: UUID
    requirements: tuple[Mapping[str, object], ...]
    evidence: tuple[Mapping[str, object], ...]


def _whole_spec_requirement(
    request_item_id: str | UUID, specification_text: str,
) -> dict[str, object]:
    """One explicitly whole-spec row for advisory checks on unparsed imports."""

    return {
        "id": f"whole-spec:{request_item_id}",
        "requirement_key": "whole_specification",
        "operator": "whole_specification",
        "expected_value": specification_text,
        "is_hard": True,
        "source_pointer": "specification_text",
        "scope": "whole_specification",
    }


_ASSESSMENT_SPEC = {
    "id": "offer-technical-assessment-proposal",
    "version": 2,
    "purpose": "suggest whether product evidence addresses tender requirements",
    "allowed_verdicts": ["match", "mismatch", "unknown"],
    "unknown_policy": "use unknown unless supplied evidence explicitly settles the fact",
    "citations": "match and mismatch require a verbatim quote and supplied evidence id",
    "no_side_effects": True,
    "never_decide_final_conformity": True,
}


def _assessment_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize_evidence(evidence: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    allowed_source_types = {
        "supplier_message", "supplier_site", "marketplace", "web", "manual", "import"
    }
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for row in evidence:
        evidence_id = str(row.get("id", row.get("source_evidence_id", ""))).strip()
        source_type = str(row.get("source_type", "")).strip()
        raw_text = row.get("text")
        if not isinstance(raw_text, str):
            raise ValueError("evidence text must be a string")
        source_text = raw_text
        if not evidence_id or evidence_id in seen_ids:
            raise ValueError("evidence ids must be present and unique")
        if source_type not in allowed_source_types:
            raise ValueError("technical assessment requires supplier or product evidence")
        if not source_text.strip():
            raise ValueError("evidence text must be non-empty")
        if len(source_text) > 50_000:
            raise ValueError("evidence text exceeds the assessment input limit")
        seen_ids.add(evidence_id)
        normalized.append(
            {
                "id": evidence_id,
                "source_type": source_type,
                "source_locator": str(row.get("source_locator", "")),
                "title": str(row.get("title", "")),
                "text": source_text,
            }
        )
    if sum(len(item["text"]) for item in normalized) > 45_000:
        raise ValueError("combined evidence exceeds the assessment input limit")
    return normalized


def _prepare_assessment_context(
    offer_id: str | UUID,
    requirements: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, str]], dict[str, object], str, str]:
    reqs = [dict(row) for row in requirements]
    if len(reqs) > 100:
        raise ValueError("technical assessment supports at most 100 requirements")
    requirement_ids = [str(row.get("id", "")).strip() for row in reqs]
    if not reqs or any(not requirement_id for requirement_id in requirement_ids):
        raise ValueError("assessment requires identified tender requirements")
    if len(set(requirement_ids)) != len(requirement_ids):
        raise ValueError("requirement ids must be unique")

    def expected_value(row: Mapping[str, object]) -> object:
        value = row.get("expected_value")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    reqs = [
        {
            "id": requirement_id,
            "requirement_key": str(row.get("requirement_key", "")),
            "operator": str(row.get("operator", "equals")),
            "expected_value": expected_value(row),
            "is_hard": bool(row.get("is_hard", True)),
            "source_pointer": str(row.get("source_pointer", "")),
            "scope": str(row.get("scope", "atomic_requirement")),
        }
        for row, requirement_id in zip(reqs, requirement_ids, strict=True)
    ]
    normalized_evidence = _normalize_evidence(evidence)
    spec_sha256 = _assessment_hash(_ASSESSMENT_SPEC)
    trusted_state: dict[str, object] = {
        "task": (
            "Assess each supplied tender requirement against only the supplied "
            "product evidence. If scope is whole_specification, assess the "
            "specification as one indivisible unit; do not split it into invented "
            "atomic requirements."
        ),
        "rules": [
            "A supplier's generic claim that a product matches is not proof of attributes.",
            "Use unknown when a requirement is not explicitly evidenced.",
            "Do not infer missing values, substitute another product, or assess "
            "price, VAT, or delivery.",
            "Return exactly one claim for each requirement id.",
            "A match or mismatch must cite one supplied evidence id and an exact quote.",
            "For a whole_specification requirement, match means all material "
            "specification clauses are explicitly supported; mismatch means the "
            "evidence explicitly contradicts a material clause; otherwise use unknown.",
            "These are proposals only; a human operator makes the final review.",
        ],
        "offer_id": str(offer_id),
        "requirements": reqs,
        "evidence_manifest": [
            {
                "id": row["id"],
                "source_type": row["source_type"],
                "text_sha256": _assessment_hash(row["text"]),
            }
            for row in normalized_evidence
        ],
    }
    return reqs, normalized_evidence, trusted_state, spec_sha256, _assessment_hash(trusted_state)


def technical_assessment_context_hash(
    offer_id: str | UUID,
    requirements: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
) -> str:
    """Fingerprint the exact offer requirements and linked evidence packet."""

    return _prepare_assessment_context(offer_id, requirements, evidence)[4]


async def propose_technical_assessment(
    extractor: StructuredExtractor,
    *,
    offer_id: str | UUID,
    requirements: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
) -> TechnicalAssessmentProposal:
    """Ask AGY for evidence-backed suggestions without changing persisted review state.

    ``evidence`` rows must contain the exact supplier-message/product text plus its
    persisted source-evidence id and allowed source type. Quotes are checked against
    that exact text after extraction. This function only returns a proposal; the
    existing immutable operator review API remains the only path to verification.
    """

    reqs, normalized_evidence, trusted_state, spec_sha256, context_sha256 = (
        _prepare_assessment_context(offer_id, requirements, evidence)
    )
    requirement_ids = [str(row["id"]) for row in reqs]

    if not normalized_evidence:
        return TechnicalAssessmentProposal(
            offer_id=str(offer_id),
            claims=tuple(
                TechnicalAssessmentClaim(
                    requirement_id=requirement_id,
                    verdict="unknown",
                    note="No supplier message or product evidence was supplied.",
                )
                for requirement_id in requirement_ids
            ),
            spec_sha256=spec_sha256,
            context_sha256=context_sha256,
        )

    untrusted_evidence_text = json.dumps(
        {"supplier_product_evidence": normalized_evidence},
        ensure_ascii=False,
        sort_keys=True,
    )
    if len(untrusted_evidence_text) > 50_000:
        raise ValueError("serialized assessment evidence exceeds the extractor input limit")
    output = await extractor.extract(
        untrusted_evidence_text,
        _TechnicalAssessmentOutput,
        context=trusted_state,
        spec_sha256=spec_sha256,
        context_sha256=context_sha256,
    )
    if output.spec_sha256 != spec_sha256 or output.context_sha256 != context_sha256:
        raise ValueError("technical assessment proposal was generated for stale inputs")
    by_evidence_id = {row["id"]: row["text"] for row in normalized_evidence}
    claims_by_requirement: dict[str, TechnicalAssessmentClaim] = {}
    for claim in output.claims:
        if claim.requirement_id not in requirement_ids:
            raise ValueError("technical assessment cited an unknown requirement")
        if claim.requirement_id in claims_by_requirement:
            raise ValueError("technical assessment repeated a requirement")
        if claim.verdict in {"match", "mismatch"}:
            evidence_id = claim.source_evidence_id or ""
            quote = claim.evidence_quote or ""
            if evidence_id not in by_evidence_id or len(quote.strip()) < 3:
                raise ValueError("match/mismatch proposal requires supplied evidence and a quote")
            if quote not in by_evidence_id[evidence_id]:
                raise ValueError("technical assessment quote is not verbatim source evidence")
        elif (claim.source_evidence_id is None) != (claim.evidence_quote is None):
            raise ValueError("technical assessment citation must include both id and quote")
        elif claim.source_evidence_id is not None:
            evidence_id = claim.source_evidence_id or ""
            quote = claim.evidence_quote or ""
            if (
                evidence_id not in by_evidence_id
                or len(quote.strip()) < 3
                or quote not in by_evidence_id[evidence_id]
            ):
                raise ValueError("technical assessment citation is not in supplied evidence")
        claims_by_requirement[claim.requirement_id] = claim
    if set(claims_by_requirement) != set(requirement_ids):
        raise ValueError("technical assessment must cover every requirement exactly once")
    return TechnicalAssessmentProposal(
        offer_id=str(offer_id),
        claims=tuple(claims_by_requirement[item] for item in requirement_ids),
        spec_sha256=spec_sha256,
        context_sha256=context_sha256,
    )


async def enqueue_technical_assessment(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    offer_id: UUID,
    owner_user_id: UUID,
    request_key: str,
) -> UUID:
    """Owner-authorize and enqueue one idempotent advisory assessment request."""

    request_key = request_key.strip()
    if not request_key or len(request_key) > 200:
        raise TechnicalReviewConflict("technical assessment request key is invalid")
    async with connection.transaction():
        offer = await connection.fetchrow(
            """SELECT o.request_item_id,i.specification_text,
                      EXISTS(SELECT 1 FROM item_requirements r
                             WHERE r.request_item_id=o.request_item_id) AS has_requirements
               FROM offers o
               JOIN request_items i ON i.id=o.request_item_id
               JOIN procurement_cases c ON c.id=i.case_id
               JOIN users u ON u.id=c.owner_user_id
               WHERE o.id=$1 AND c.id=$2 AND c.owner_user_id=$3 AND u.active
                 AND c.status NOT IN ('closed','cancelled')
                 AND o.status<>'withdrawn'
               FOR SHARE OF o,i,c,u""",
            offer_id, case_id, owner_user_id,
        )
        if offer is None:
            raise TechnicalReviewConflict("offer is absent or case is not editable by owner")
        if not offer["has_requirements"] and not str(
            offer["specification_text"] or ""
        ).strip():
            raise TechnicalReviewConflict("tender item has no specification to assess")
        idempotency_key = stable_key(
            "technical-assessment-request", case_id, offer_id, owner_user_id, request_key
        )
        proposal_id = await connection.fetchval(
            """INSERT INTO offer_technical_assessment_proposals(
                   case_id,request_item_id,offer_id,requested_by,idempotency_key
               ) VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id""",
            case_id, offer["request_item_id"], offer_id, owner_user_id, idempotency_key,
        )
        if proposal_id is None:
            existing = await connection.fetchrow(
                """SELECT id,case_id,request_item_id,offer_id,requested_by
                   FROM offer_technical_assessment_proposals WHERE idempotency_key=$1""",
                idempotency_key,
            )
            if existing is None:
                raise RuntimeError("technical assessment idempotency conflict vanished")
            if (
                existing["case_id"] != case_id
                or existing["request_item_id"] != offer["request_item_id"]
                or existing["offer_id"] != offer_id
                or existing["requested_by"] != owner_user_id
            ):
                raise TechnicalReviewConflict("assessment request key has different facts")
            proposal_id = existing["id"]
        await enqueue_job(
            connection,
            kind="assess_offer_technical",
            payload={
                "proposal_id": str(proposal_id),
                "case_id": str(case_id),
                "offer_id": str(offer_id),
                "owner_user_id": str(owner_user_id),
            },
            idempotency_key=stable_key("technical-assessment-job", proposal_id),
        )
        return proposal_id


async def load_technical_assessment_snapshot(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    offer_id: UUID,
    owner_user_id: UUID,
    allow_closed: bool = False,
) -> TechnicalAssessmentSnapshot:
    offer = await connection.fetchrow(
        """SELECT o.request_item_id,i.specification_text,c.owner_user_id,c.status
           FROM offers o
           JOIN request_items i ON i.id=o.request_item_id
           JOIN procurement_cases c ON c.id=i.case_id
           JOIN users u ON u.id=c.owner_user_id
           WHERE o.id=$1 AND c.id=$2 AND c.owner_user_id=$3 AND u.active
             AND o.status<>'withdrawn'
             AND ($4::boolean OR c.status NOT IN ('closed','cancelled'))""",
        offer_id, case_id, owner_user_id, allow_closed,
    )
    if offer is None:
        raise TechnicalReviewConflict("assessment offer is absent or no longer authorized")
    requirements = await connection.fetch(
        """SELECT id,requirement_key,operator,expected_value,is_hard,source_pointer
           FROM item_requirements WHERE request_item_id=$1
           ORDER BY is_hard DESC,requirement_key,id""",
        offer["request_item_id"],
    )
    if not requirements:
        specification_text = str(offer["specification_text"] or "").strip()
        if not specification_text:
            raise TechnicalReviewConflict("tender item has no specification to assess")
        requirements = [
            _whole_spec_requirement(offer["request_item_id"], specification_text)
        ]
    evidence_rows = await connection.fetch(
        """SELECT source.id,source.source_type,source.source_locator,
                  source.title,source.raw_payload
           FROM source_evidence source
           WHERE EXISTS (
             SELECT 1 FROM offer_observations observation
             WHERE observation.offer_id=$1
               AND observation.source_evidence_id=source.id
           )
             AND source.source_type=ANY($2::text[])
           ORDER BY source.captured_at DESC,source.id""",
        offer_id,
        ["supplier_message", "supplier_site", "marketplace", "web", "manual", "import"],
    )
    evidence: list[dict[str, object]] = []
    for row in evidence_rows:
        raw_payload = row["raw_payload"]
        if isinstance(raw_payload, str):
            raw_payload = json.loads(raw_payload)
        if not isinstance(raw_payload, Mapping):
            continue
        # Only captured message text or a verbatim page excerpt is accepted as
        # quoteable evidence; labels, extracted numbers, and generated summaries
        # are never promoted to supplier proof.
        text = raw_payload.get("text")
        if not isinstance(text, str) or not text.strip():
            text = raw_payload.get("excerpt")
        if not isinstance(text, str) or not text.strip():
            continue
        evidence.append(
            {
                "id": str(row["id"]),
                "source_type": str(row["source_type"]),
                "source_locator": str(row["source_locator"]),
                "title": str(row["title"]),
                "text": text,
            }
        )
    return TechnicalAssessmentSnapshot(
        case_id=case_id,
        request_item_id=offer["request_item_id"],
        offer_id=offer_id,
        owner_user_id=owner_user_id,
        requirements=tuple(dict(row) for row in requirements),
        evidence=tuple(evidence),
    )


async def persist_technical_assessment_proposal(
    connection: asyncpg.Connection,
    *,
    proposal_id: UUID,
    case_id: UUID,
    owner_user_id: UUID,
    proposal: TechnicalAssessmentProposal,
    stale: bool,
) -> bool:
    try:
        proposal_offer_id = UUID(proposal.offer_id)
    except ValueError as exc:
        raise ValueError("proposal offer id is invalid") from exc
    status = "stale" if stale else "proposed"
    result = await connection.execute(
        """UPDATE offer_technical_assessment_proposals
           SET status=$2,spec_sha256=$3,context_sha256=$4,claims=$5::jsonb,
               completed_at=now()
           WHERE id=$1 AND offer_id=$6 AND case_id=$7 AND requested_by=$8
             AND status='queued'""",
        proposal_id,
        status,
        proposal.spec_sha256,
        proposal.context_sha256,
        [claim.model_dump(mode="json") for claim in proposal.claims],
        proposal_offer_id,
        case_id,
        owner_user_id,
    )
    return result == "UPDATE 1"


async def fail_technical_assessment_proposal(
    connection: asyncpg.Connection,
    *,
    proposal_id: UUID,
    case_id: UUID,
    offer_id: UUID,
    owner_user_id: UUID,
    error_code: str,
) -> bool:
    result = await connection.execute(
        """UPDATE offer_technical_assessment_proposals
           SET status='failed',error_code=$2,completed_at=now()
           WHERE id=$1 AND case_id=$3 AND offer_id=$4 AND requested_by=$5
             AND status='queued'""",
        proposal_id,
        error_code[:120],
        case_id,
        offer_id,
        owner_user_id,
    )
    return result == "UPDATE 1"


async def read_latest_technical_assessment(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    offer_id: UUID,
    owner_user_id: UUID,
) -> dict[str, Any] | None:
    """Read the latest owner-visible advisory proposal and mark changed inputs stale."""

    snapshot = await load_technical_assessment_snapshot(
        connection,
        case_id=case_id,
        offer_id=offer_id,
        owner_user_id=owner_user_id,
        allow_closed=True,
    )
    row = await connection.fetchrow(
        """SELECT id,case_id,request_item_id,offer_id,requested_by,idempotency_key,
                  status,spec_sha256,context_sha256,claims,error_code,created_at,completed_at
           FROM offer_technical_assessment_proposals
           WHERE case_id=$1 AND offer_id=$2
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        case_id, offer_id,
    )
    if row is None:
        return None
    value = dict(row)
    if value["status"] == "proposed":
        current_hash = (
            technical_assessment_context_hash(
                str(offer_id), snapshot.requirements, snapshot.evidence
            )
            if snapshot.requirements
            else None
        )
        if current_hash != value["context_sha256"]:
            value["status"] = "stale"
    claims = value.get("claims")
    value["claims"] = json.loads(claims) if isinstance(claims, str) else claims
    value["status"] = (
        "operator_review_required" if value["status"] == "proposed" else value["status"]
    )
    return value


class TechnicalReviewConflict(ValueError):
    """A review does not belong to the owner, offer, or requirement."""


def technical_states(
    offers: Sequence[Mapping[str, object]],
    requirements: Sequence[Mapping[str, object]],
    reviews: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    """Require a whole-spec review and every hard atomic requirement.

    A seller's generic statement and the legacy ``offers.status='exact'`` are
    deliberately insufficient to mark an offer verified.
    """

    hard_by_item: dict[str, set[str]] = {}
    for requirement in requirements:
        if requirement["is_hard"]:
            hard_by_item.setdefault(str(requirement["request_item_id"]), set()).add(
                str(requirement["id"])
            )
    latest: dict[tuple[str, str | None], str] = {}
    for review in sorted(
        reviews,
        key=lambda row: (str(row["created_at"]), str(row["id"])),
        reverse=True,
    ):
        key = (
            str(review["offer_id"]),
            str(review["requirement_id"]) if review["requirement_id"] else None,
        )
        latest.setdefault(key, str(review["verdict"]))
    result: dict[str, str] = {}
    for offer in offers:
        data = dict(offer)
        offer_id = str(data.get("offer_id", data.get("id")))
        status = str(data.get("offer_status", data.get("status")))
        hard = hard_by_item.get(str(offer["request_item_id"]), set())
        whole = latest.get((offer_id, None))
        checks = [latest.get((offer_id, requirement_id)) for requirement_id in hard]
        if whole == "mismatch" or "mismatch" in checks:
            result[offer_id] = "mismatch"
        elif whole == "match" and all(check == "match" for check in checks):
            result[offer_id] = "verified"
        elif status == "mismatch" and whole is None:
            result[offer_id] = "mismatch"
        else:
            result[offer_id] = "needs_confirmation"
    return result


async def load_case_technical_reviews(
    connection: asyncpg.Connection, case_id: UUID,
) -> tuple[list[asyncpg.Record], list[asyncpg.Record]]:
    requirements = await connection.fetch(
        """SELECT r.id,r.request_item_id,r.requirement_key,r.operator,
                  r.expected_value,r.is_hard,r.source_pointer
           FROM item_requirements r
           JOIN request_items i ON i.id=r.request_item_id
           WHERE i.case_id=$1 ORDER BY i.line_number,r.is_hard DESC,r.id""",
        case_id,
    )
    reviews = await connection.fetch(
        """SELECT review.id,review.offer_id,review.request_item_id,
                  review.requirement_id,review.verdict,review.note,
                  review.source_evidence_id,review.created_at
           FROM offer_technical_reviews review
           JOIN request_items i ON i.id=review.request_item_id
           WHERE i.case_id=$1 ORDER BY review.created_at DESC,review.id DESC""",
        case_id,
    )
    return list(requirements), list(reviews)


async def record_technical_review(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    offer_id: UUID,
    requirement_id: UUID | None,
    owner_user_id: UUID,
    verdict: str,
    note: str,
    source_evidence_id: UUID | None,
    idempotency_key: str,
) -> UUID:
    if verdict not in {"match", "mismatch"}:
        raise TechnicalReviewConflict("unsupported technical verdict")
    note = note.strip()
    if not 3 <= len(note) <= 2000 or not idempotency_key.strip():
        raise TechnicalReviewConflict("technical review requires a reason")
    async with connection.transaction():
        offer = await connection.fetchrow(
            """SELECT o.request_item_id,i.specification_text
               FROM offers o JOIN request_items i ON i.id=o.request_item_id
               JOIN procurement_cases c ON c.id=i.case_id
               WHERE o.id=$1 AND i.case_id=$2 AND c.owner_user_id=$3
                 AND c.status NOT IN ('closed','cancelled')
               FOR SHARE OF o,i,c""",
            offer_id, case_id, owner_user_id,
        )
        if offer is None:
            raise TechnicalReviewConflict("offer is absent or case is not editable")
        if requirement_id is None:
            if not str(offer["specification_text"] or "").strip():
                raise TechnicalReviewConflict("whole specification is absent")
        elif not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM item_requirements WHERE id=$1 AND request_item_id=$2)",
            requirement_id, offer["request_item_id"],
        ):
            raise TechnicalReviewConflict("requirement belongs to another item")
        if source_evidence_id is not None and not await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM offer_observations
                              WHERE offer_id=$1 AND source_evidence_id=$2)""",
            offer_id, source_evidence_id,
        ):
            raise TechnicalReviewConflict("evidence does not belong to this offer")
        existing = await connection.fetchrow(
            "SELECT * FROM offer_technical_reviews WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            expected = (
                offer_id, offer["request_item_id"], requirement_id, verdict,
                note, source_evidence_id, owner_user_id,
            )
            actual = tuple(existing[key] for key in (
                "offer_id", "request_item_id", "requirement_id", "verdict",
                "note", "source_evidence_id", "reviewed_by",
            ))
            if actual != expected:
                raise TechnicalReviewConflict("technical review key has different facts")
            return existing["id"]
        return await connection.fetchval(
            """INSERT INTO offer_technical_reviews(
                 offer_id,request_item_id,requirement_id,verdict,note,
                 source_evidence_id,reviewed_by,idempotency_key
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            offer_id, offer["request_item_id"], requirement_id, verdict,
            note, source_evidence_id, owner_user_id, idempotency_key,
        )
