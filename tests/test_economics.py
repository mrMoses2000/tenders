import unittest
from decimal import Decimal

from tender_agent.economics import (
    allocate_delivery_cost,
    calculate_landed_cost,
    calculate_purchase_quantity,
)


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

    def test_minimum_order_is_included_in_real_cost(self) -> None:
        result = calculate_landed_cost(
            tender_unit_price="3800",
            required_quantity="5",
            offer_unit_price="1232",
            logistics="1000",
            minimum_order="36",
        )
        self.assertEqual(result.purchase_quantity, Decimal("36"))
        self.assertEqual(result.total_cost, Decimal("45352.00"))
        self.assertEqual(result.profit, Decimal("-26352.00"))
        self.assertEqual(result.economy, "Невыгодно")

    def test_order_multiple_rounds_up_after_moq(self) -> None:
        self.assertEqual(
            calculate_purchase_quantity("20", minimum_order="24", order_multiple="10"),
            Decimal("30"),
        )

    def test_partial_delivery_uses_remaining_quantity_for_revenue(self) -> None:
        result = calculate_landed_cost(
            tender_unit_price="400",
            required_quantity="20",
            offer_unit_price="300",
            logistics="1000",
        )
        self.assertEqual(result.tender_revenue, Decimal("8000.00"))
        self.assertEqual(result.goods_cost, Decimal("6000.00"))


if __name__ == "__main__":
    unittest.main()
