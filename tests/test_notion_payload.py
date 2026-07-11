from datetime import date
from decimal import Decimal
import unittest

from tender_agent.models import CandidateOffer, RequestLine, SupplierIdentity
from tender_agent.notion_payload import offer_observation_key, pack_notion_offer
from tender_agent.validation import validate_candidate


class NotionPayloadTests(unittest.TestCase):
    def test_payload_and_key_are_idempotent(self) -> None:
        request = RequestLine("№23", "Корзина", "9 л пластик", Decimal("3"), "шт", Decimal("850"))
        supplier = SupplierIdentity("МЕГАСТРОЙ", site_url="https://example.com")
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


if __name__ == "__main__":
    unittest.main()
