import unittest
from datetime import date
from decimal import Decimal

from tender_agent.models import CandidateOffer, RequestLine, SupplierIdentity
from tender_agent.notion_payload import offer_observation_key, pack_notion_offer
from tender_agent.validation import validate_candidate


class NotionPayloadTests(unittest.TestCase):
    def test_payload_and_key_are_idempotent(self) -> None:
        request = RequestLine(
            "№23", "Корзина", "9 л пластик", Decimal("3"), "шт", Decimal("850"),
            notion_page_url="https://notion.so/request-23",
        )
        supplier = SupplierIdentity(
            "МЕГАСТРОЙ", site_url="https://example.com",
            notion_page_url="https://notion.so/supplier-mega",
        )
        offer = CandidateOffer(
            "Корзина P139 9 л",
            "https://example.com/p139",
            date(2026, 7, 11),
            Decimal("800"),
            "шт",
            model_article="P139",
            supplier_city="Усть-Каменогорск",
            source_photo_url="https://example.com/p139.jpg",
        )
        validation = validate_candidate(request, offer)
        first = pack_notion_offer(request, supplier, offer, validation)
        second = pack_notion_offer(request, supplier, offer, validation)
        self.assertEqual(first, second)
        self.assertEqual(first["idempotency_key"], offer_observation_key(request, supplier, offer))
        self.assertIsNone(first["properties"]["Количество доступно"])
        self.assertEqual(first["properties"]["Город поставщика"], "Усть-Каменогорск")
        self.assertEqual(first["properties"]["Источник фото"], "https://example.com/p139.jpg")
        self.assertEqual(first["properties"]["Ключ наблюдения"], first["idempotency_key"])
        self.assertRegex(first["idempotency_key"], r"^offer:v3:[0-9a-f]{64}$")
        self.assertNotIn("https://", first["idempotency_key"])
        self.assertEqual(first["properties"]["Заявка"], ["https://notion.so/request-23"])
        self.assertEqual(first["properties"]["Поставщик"], ["https://notion.so/supplier-mega"])

    def test_payload_blocks_false_profit_from_moq(self) -> None:
        request = RequestLine(
            "№11", "Головка", "Huter", Decimal("5"), "шт", Decimal("3800"),
            hard_requirements={"бренд": "Huter"},
        )
        supplier = SupplierIdentity("Отвертка.kz", city="Караганда")
        offer = CandidateOffer(
            "Головка Huter", "https://example.com/huter", date(2026, 7, 13),
            price=Decimal("1232"), price_unit="шт", minimum_order=Decimal("36"),
            forecast_logistics=Decimal("1000"), attributes={"бренд": "Huter"},
        )
        payload = pack_notion_offer(request, supplier, offer, validate_candidate(request, offer))
        self.assertEqual(payload["properties"]["Расчётное количество покупки"], 36.0)
        self.assertEqual(payload["properties"]["Безопасная себестоимость прогноз"], 45352.0)
        self.assertEqual(payload["properties"]["Безопасная экономика"], "Невыгодно")

    def test_unknown_partial_balance_never_uses_full_tender_quantity(self) -> None:
        request = RequestLine(
            "№50", "Светильник", "50 Вт", Decimal("75"), "шт", Decimal("3999"),
            procurement_quantity_known=False,
        )
        supplier = SupplierIdentity("Магазин")
        offer = CandidateOffer(
            "Светильник 50 Вт", "https://example.com/light", date(2026, 7, 13),
            price=Decimal("3000"), price_unit="шт", technical_match_proven=True,
        )
        payload = pack_notion_offer(request, supplier, offer, validate_candidate(request, offer))
        self.assertIsNone(payload["properties"]["Количество к закупке"])
        self.assertIsNone(payload["properties"]["Расчётное количество покупки"])
        self.assertEqual(payload["properties"]["Безопасная экономика"], "Не рассчитано")

    def test_closed_request_keeps_zero_balance_without_cost_forecast(self) -> None:
        request = RequestLine(
            "№15", "Известь", "200 кг", Decimal("200"), "кг", Decimal("140"),
            procurement_quantity=Decimal("0"),
        )
        offer = CandidateOffer(
            "Известь", "https://example.com/lime", date(2026, 7, 13),
            price=Decimal("100"), price_unit="кг", technical_match_proven=True,
        )
        payload = pack_notion_offer(request, SupplierIdentity("Поставщик"), offer, validate_candidate(request, offer))
        self.assertEqual(payload["properties"]["Количество к закупке"], 0.0)
        self.assertIsNone(payload["properties"]["Расчётное количество покупки"])
        self.assertEqual(payload["properties"]["Безопасная экономика"], "Не рассчитано")


if __name__ == "__main__":
    unittest.main()
