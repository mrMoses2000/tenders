import unittest

from tender_agent.adapters import plan_source_escalation, strategy_for_url
from tender_agent.extractor import ExtractedProduct


class AdapterTests(unittest.TestCase):
    def test_known_domains_are_classified(self) -> None:
        self.assertEqual(strategy_for_url("https://kaspi.kz/shop/p/x").source_type, "Kaspi")
        self.assertEqual(strategy_for_url("https://www.olx.kz/d/item").source_type, "OLX")
        self.assertEqual(strategy_for_url("https://2gis.kz/karaganda/firm/x").source_type, "2ГИС")
        self.assertEqual(strategy_for_url("https://store.example/item").source_type, "Сайт")

    def test_http_extraction_precedes_browser(self) -> None:
        plan = plan_source_escalation("https://kaspi.kz/shop/p/x")
        self.assertEqual(plan.first_step, "http-extractor")
        self.assertFalse(plan.computer_use_required)

    def test_missing_dynamic_price_escalates_to_chrome(self) -> None:
        extracted = ExtractedProduct("https://kaspi.kz/shop/p/x", product_name="Товар")
        plan = plan_source_escalation("https://kaspi.kz/shop/p/x", extracted)
        self.assertEqual(plan.first_step, "chrome")
        self.assertTrue(plan.browser_required)

    def test_stock_confirmation_requires_contact_not_browser(self) -> None:
        extracted = ExtractedProduct("https://store.example/item", product_name="Товар", price="100")
        plan = plan_source_escalation(
            "https://store.example/item", extracted, required_fields=("confirmed_stock",)
        )
        self.assertEqual(plan.first_step, "draft-contact")
        self.assertTrue(plan.supplier_contact_required)


if __name__ == "__main__":
    unittest.main()
