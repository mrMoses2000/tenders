import unittest

from tender_agent.dedupe import dedupe_suppliers, supplier_key
from tender_agent.models import SupplierIdentity


class DedupeTests(unittest.TestCase):
    def test_same_domain_groups_aliases(self) -> None:
        suppliers = [
            SupplierIdentity("Аквилон", site_url="https://www.akvilon.kz/catalog/item"),
            SupplierIdentity("Akvilon.kz Караганда", site_url="https://akvilon.kz/contacts"),
            SupplierIdentity("Другой", site_url="https://other.example"),
        ]
        groups = dedupe_suppliers(suppliers)
        self.assertEqual(sorted(map(len, groups)), [1, 2])

    def test_key_is_stable(self) -> None:
        supplier = SupplierIdentity("Цемент Караганда", phone="+7 777 142 74 32")
        self.assertEqual(supplier_key(supplier), supplier_key(supplier))

    def test_key_survives_contact_enrichment(self) -> None:
        before = SupplierIdentity("Магазин", city="Караганда")
        after = SupplierIdentity(
            "Магазин", city="Караганда", phone="+7 777 000 00 00", site_url="https://shop.kz"
        )
        self.assertEqual(supplier_key(before), supplier_key(after))

    def test_shared_marketplace_domain_does_not_merge_sellers(self) -> None:
        suppliers = [
            SupplierIdentity("Продавец А", city="Караганда", site_url="https://kaspi.kz/shop/p/x"),
            SupplierIdentity("Продавец Б", city="Караганда", site_url="https://kaspi.kz/shop/p/x"),
        ]
        self.assertEqual(sorted(map(len, dedupe_suppliers(suppliers))), [1, 1])


if __name__ == "__main__":
    unittest.main()
