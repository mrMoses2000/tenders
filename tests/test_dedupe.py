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


if __name__ == "__main__":
    unittest.main()
