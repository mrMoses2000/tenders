from datetime import date
from decimal import Decimal
import unittest

from tender_agent.models import CandidateOffer, RequestLine
from tender_agent.validation import TECH_EXACT, TECH_PENDING, TECH_REJECT, validate_candidate


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = RequestLine(
            request_id="Караганда_1 №61",
            name="Цемент",
            technical_spec="Цемент ПЦ-400 50 кг",
            required_quantity=Decimal("5"),
            required_unit="мешок",
            tender_unit_price=Decimal("2100"),
            hard_requirements={"марка": "ПЦ-400", "фасовка": "50 кг"},
        )

    def test_missing_hard_parameter_is_pending(self) -> None:
        offer = CandidateOffer(
            supplier_item="Цемент ПЦ-400",
            source_url="https://example.com/cement",
            checked_at=date(2026, 7, 11),
            price=Decimal("2290"),
            price_unit="мешок",
            attributes={"марка": "ПЦ-400"},
        )
        result = validate_candidate(self.request, offer)
        self.assertEqual(result.technical_status, TECH_PENDING)
        self.assertIn("Подтвердить фасовка: требуется 50 кг", result.confirmations)

    def test_wrong_hard_parameter_is_rejected(self) -> None:
        offer = CandidateOffer(
            supplier_item="Цемент М500 50 кг",
            source_url="https://example.com/cement",
            checked_at=date(2026, 7, 11),
            price=Decimal("2290"),
            price_unit="мешок",
            attributes={"марка": "М500", "фасовка": "50 кг"},
        )
        self.assertEqual(validate_candidate(self.request, offer).technical_status, TECH_REJECT)

    def test_technical_exact_does_not_imply_commercial_readiness(self) -> None:
        offer = CandidateOffer(
            supplier_item="Цемент ПЦ-400 50 кг",
            source_url="https://example.com/cement",
            checked_at=date(2026, 7, 11),
            price=Decimal("2290"),
            price_unit="мешок",
            attributes={"марка": "ПЦ-400", "фасовка": "50 кг"},
        )
        result = validate_candidate(self.request, offer)
        self.assertEqual(result.technical_status, TECH_EXACT)
        self.assertEqual(result.readiness, "Нужно связаться")

    def test_missing_package_is_pending_not_rejected(self) -> None:
        request = RequestLine("№1", "Клей", "25 кг", Decimal("10"), "кг", Decimal("1000"))
        offer = CandidateOffer(
            supplier_item="Клей, мешок",
            source_url="https://example.com/glue",
            checked_at=date(2026, 7, 11),
            price=Decimal("5000"),
            price_unit="мешок",
            technical_match_proven=True,
        )
        result = validate_candidate(request, offer)
        self.assertEqual(result.technical_status, TECH_EXACT)
        self.assertIsNone(result.effective_unit_price)
        self.assertTrue(any("фасовк" in item for item in result.confirmations))

    def test_no_structured_technical_evidence_is_pending(self) -> None:
        request = RequestLine("№2", "Товар", "Спецификация", Decimal("1"), "шт", Decimal("100"))
        offer = CandidateOffer(
            supplier_item="Похожий товар",
            source_url="https://example.com/item",
            checked_at=date(2026, 7, 11),
            price=Decimal("90"),
            price_unit="шт",
        )
        self.assertEqual(validate_candidate(request, offer).technical_status, TECH_PENDING)


if __name__ == "__main__":
    unittest.main()
