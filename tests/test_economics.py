from decimal import Decimal
import unittest

from tender_agent.economics import allocate_delivery_cost, calculate_landed_cost


class EconomicsTests(unittest.TestCase):
    def test_karaganda_basket_forecast(self) -> None:
        result = calculate_landed_cost(
            tender_unit_price="850",
            required_quantity="3",
            offer_unit_price="800",
            logistics="1000",
        )
        self.assertEqual(result.tender_revenue, Decimal("2550.00"))
        self.assertEqual(result.total_cost, Decimal("3400.00"))
        self.assertEqual(result.profit, Decimal("-850.00"))
        self.assertEqual(result.margin_percent, Decimal("-33.33"))
        self.assertEqual(result.economy, "Невыгодно")

    def test_missing_price_stays_unknown(self) -> None:
        result = calculate_landed_cost(
            tender_unit_price="2100", required_quantity="5", offer_unit_price=None, logistics="5000"
        )
        self.assertIsNone(result.total_cost)
        self.assertEqual(result.economy, "Не рассчитано")

    def test_delivery_allocation_preserves_total_to_tiyn(self) -> None:
        result = allocate_delivery_cost("5000", {"№8": 1, "№12": 1, "№15": 1})
        self.assertEqual(sum(result.values(), Decimal("0")), Decimal("5000.00"))
        self.assertEqual(result["№8"], Decimal("1666.67"))
        self.assertEqual(result["№12"], Decimal("1666.67"))
        self.assertEqual(result["№15"], Decimal("1666.66"))

    def test_zero_weights_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            allocate_delivery_cost("5000", {"№8": 0, "№12": 0})


if __name__ == "__main__":
    unittest.main()
