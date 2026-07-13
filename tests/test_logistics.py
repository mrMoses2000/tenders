from datetime import date
from decimal import Decimal
import unittest

from tender_agent.logistics import (
    ProcurementScenario,
    RouteObservation,
    compare_procurement_scenarios,
    summarize_route,
)


class LogisticsTests(unittest.TestCase):
    def test_route_history_confidence_and_median(self) -> None:
        observations = [
            RouteObservation("Алматы", "Караганда", "малый груз", cost, date(2026, 7, day))
            for day, cost in enumerate((5000, 6000, 7000, 8000), start=1)
        ]
        summary = summarize_route(observations)
        self.assertEqual(summary.count, 4)
        self.assertEqual(summary.median, Decimal("6500.00"))
        self.assertEqual(summary.confidence, "Высокая")

    def test_full_cost_can_make_local_option_better(self) -> None:
        ranked = compare_procurement_scenarios([
            ProcurementScenario("Локально", "2700", "5", logistics="1000", risk_reserve_percent="2", evidence_level=4),
            ProcurementScenario("Межгород", "2200", "5", logistics="5000", risk_reserve_percent="10", evidence_level=2),
        ])
        self.assertEqual(ranked[0].name, "Локально")
        self.assertEqual(ranked[0].rank, 1)
        self.assertLess(ranked[0].risk_adjusted_cost, ranked[1].risk_adjusted_cost)

    def test_wrong_spec_scenario_is_not_ranked(self) -> None:
        ranked = compare_procurement_scenarios([
            ProcurementScenario("Дешевый аналог", "100", "1", technical_valid=False),
            ProcurementScenario("Точный", "200", "1", technical_valid=True),
        ])
        rejected = next(item for item in ranked if item.name == "Дешевый аналог")
        self.assertIsNone(rejected.rank)

    def test_mixed_routes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            summarize_route([
                RouteObservation("Алматы", "Караганда", "малый груз", "5000", date(2026, 7, 1)),
                RouteObservation("Астана", "Караганда", "малый груз", "5000", date(2026, 7, 2)),
            ])

    def test_duplicate_scenario_names_get_independent_ranks(self) -> None:
        ranked = compare_procurement_scenarios([
            ProcurementScenario("Одинаковое имя", "100", "1"),
            ProcurementScenario("Одинаковое имя", "200", "1"),
        ])
        self.assertEqual([item.rank for item in ranked], [1, 2])


if __name__ == "__main__":
    unittest.main()
