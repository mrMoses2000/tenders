import unittest
from decimal import Decimal

from tender_agent.normalization import (
    conversion,
    effective_unit_price,
    normalize_phone,
    normalize_unit,
)


class NormalizationTests(unittest.TestCase):
    def test_unit_aliases_and_metric_conversion(self) -> None:
        self.assertEqual(normalize_unit("ШТ."), "шт")
        result = conversion("г", "кг")
        self.assertTrue(result.compatible)
        self.assertEqual(result.multiplier, Decimal("0.001"))

    def test_package_price_requires_packaging(self) -> None:
        self.assertIsNone(effective_unit_price(Decimal("2500"), "мешок", "кг"))
        self.assertEqual(
            effective_unit_price(Decimal("2500"), "мешок", "кг", Decimal("50"), "кг"),
            Decimal("50"),
        )

    def test_phone_normalization(self) -> None:
        self.assertEqual(normalize_phone("8 (700) 000-00-07"), "+77000000007")

    def test_procurement_units_from_notion_are_normalized(self) -> None:
        self.assertEqual(normalize_unit("Одна пачка"), "упаковка")
        self.assertEqual(normalize_unit("Пара"), "пара")
        self.assertEqual(normalize_unit("Метр погонный"), "м")


if __name__ == "__main__":
    unittest.main()
