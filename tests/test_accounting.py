from decimal import Decimal
from uuid import uuid4

import pytest

from procurement_bot.accounting import allocate_equal_delivery


def test_equal_delivery_allocation_preserves_exact_total_and_order() -> None:
    items = [uuid4(), uuid4(), uuid4()]

    result = allocate_equal_delivery(Decimal("100.00"), items)

    assert [value.request_item_id for value in result] == items
    assert [value.amount for value in result] == [
        Decimal("33.34"),
        Decimal("33.33"),
        Decimal("33.33"),
    ]
    assert sum((value.amount for value in result), Decimal(0)) == Decimal("100.00")


def test_delivery_allocation_rejects_duplicates_and_subcent_guessing() -> None:
    item = uuid4()
    with pytest.raises(ValueError, match="unique"):
        allocate_equal_delivery(Decimal("10"), [item, item])
    with pytest.raises(ValueError, match="decimal places"):
        allocate_equal_delivery(Decimal("10.001"), [item])
