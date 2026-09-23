from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RequirementStatus(StrEnum):
    CONFIRMED_MATCH = "confirmed_match"
    CONFIRMED_MISMATCH = "confirmed_mismatch"
    UNKNOWN = "unknown"


class TechnicalStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NEEDS_CONFIRMATION = "needs_confirmation"


class RequirementCheck(StrictModel):
    requirement: str = Field(min_length=1, max_length=500)
    hard: bool = True
    status: RequirementStatus
    evidence: str | None = Field(default=None, max_length=1000)


class OfferAssessment(StrictModel):
    offer_id: str = Field(min_length=1, max_length=100)
    supplier_id: str = Field(min_length=1, max_length=100)
    supplier_name: str = Field(min_length=1, max_length=500)
    product_name: str = Field(min_length=1, max_length=1000)
    availability_confirmed: bool | None = None
    total_cost: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)
    requirement_checks: list[RequirementCheck] = Field(default_factory=list)
    evidence_quality: float = Field(default=0, ge=0, le=1)


class EvaluatedOffer(StrictModel):
    offer: OfferAssessment
    technical_status: TechnicalStatus
    eligible: bool
    blockers: list[str]
    rank: int | None = None


def technical_status(offer: OfferAssessment) -> TechnicalStatus:
    hard = [check for check in offer.requirement_checks if check.hard]
    if any(check.status == RequirementStatus.CONFIRMED_MISMATCH for check in hard):
        return TechnicalStatus.MISMATCH
    if not hard or any(check.status == RequirementStatus.UNKNOWN for check in hard):
        return TechnicalStatus.NEEDS_CONFIRMATION
    return TechnicalStatus.MATCH


def evaluate_and_rank(
    offers: list[OfferAssessment],
    *,
    comparison_currency: str = "KZT",
) -> list[EvaluatedOffer]:
    """Technical conformity is resolved before price can affect ranking."""

    evaluated: list[EvaluatedOffer] = []
    for offer in offers:
        status = technical_status(offer)
        blockers: list[str] = []
        if status != TechnicalStatus.MATCH:
            reason = (
                "technical_mismatch"
                if status == TechnicalStatus.MISMATCH
                else "spec_unknown"
            )
            blockers.append(reason)
        if offer.availability_confirmed is not True:
            blockers.append("availability_unknown")
        if offer.total_cost is None:
            blockers.append("price_unknown")
        if offer.currency != comparison_currency:
            blockers.append("currency_not_comparable")
        evaluated.append(
            EvaluatedOffer(
                offer=offer,
                technical_status=status,
                eligible=not blockers,
                blockers=blockers,
            )
        )

    eligible = sorted(
        (value for value in evaluated if value.eligible),
        key=lambda value: (
            value.offer.total_cost,
            -value.offer.evidence_quality,
            value.offer.supplier_name.casefold(),
            value.offer.offer_id,
        ),
    )
    ranks = {value.offer.offer_id: index for index, value in enumerate(eligible, start=1)}
    return [
        value.model_copy(update={"rank": ranks.get(value.offer.offer_id)})
        for value in evaluated
    ]
