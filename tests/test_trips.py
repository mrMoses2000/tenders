from decimal import Decimal
import unittest

from tender_agent.trips import MarketItemHypothesis, evaluate_market_trip


class TripTests(unittest.TestCase):
    def test_profitable_group_trip(self) -> None:
        result = evaluate_market_trip(
            market_name="Алматы-1",
            city="Алматы",
            trip_cost="5000",
            minimum_net_savings="5000",
            items=[
                MarketItemHypothesis("№4", "Товар", "20", "2500", "1500", "1900", "0.7"),
                MarketItemHypothesis("№7", "Материал", "10", "1800", "1200", "1500", "0.5"),
            ],
        )
        self.assertGreater(result.expected_net_savings, Decimal("5000"))
        self.assertIn("оправдана", result.recommendation)
        self.assertEqual(len(result.route_sheet), 3)

    def test_hypothesis_does_not_claim_confirmation(self) -> None:
        item = MarketItemHypothesis("№1", "Товар", "1", "1000", "900", "950", "0.1")
        result = evaluate_market_trip(market_name="Рынок", city="Город", trip_cost="500", items=[item])
        self.assertIn("не окупается", result.recommendation)
        self.assertIn("проверить ТЗ, остаток, документы и контакт", result.route_sheet[1])


if __name__ == "__main__":
    unittest.main()
