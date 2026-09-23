from decimal import Decimal

from procurement_bot.evaluation import (
    OfferAssessment,
    RequirementCheck,
    RequirementStatus,
    TechnicalStatus,
    evaluate_and_rank,
)


def _offer(identifier: str, price: str, status: RequirementStatus) -> OfferAssessment:
    return OfferAssessment(
        offer_id=identifier,
        supplier_id=f"supplier-{identifier}",
        supplier_name=f"Поставщик {identifier}",
        product_name="Марля",
        availability_confirmed=True,
        total_cost=Decimal(price),
        currency="KZT",
        requirement_checks=[RequirementCheck(requirement="плотность", status=status)],
        evidence_quality=0.8,
    )


def test_price_never_ranks_technical_mismatch_above_match() -> None:
    cheap_wrong = _offer("wrong", "1", RequirementStatus.CONFIRMED_MISMATCH)
    costly_match = _offer("match", "100", RequirementStatus.CONFIRMED_MATCH)
    results = evaluate_and_rank([cheap_wrong, costly_match])
    by_id = {value.offer.offer_id: value for value in results}
    assert by_id["wrong"].technical_status == TechnicalStatus.MISMATCH
    assert by_id["wrong"].rank is None
    assert by_id["match"].rank == 1


def test_unknown_availability_or_price_blocks_ranking() -> None:
    offer = _offer("unknown", "20", RequirementStatus.CONFIRMED_MATCH).model_copy(
        update={"availability_confirmed": None, "total_cost": None}
    )
    result = evaluate_and_rank([offer])[0]
    assert result.eligible is False
    assert {"availability_unknown", "price_unknown"} <= set(result.blockers)
