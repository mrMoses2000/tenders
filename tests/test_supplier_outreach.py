from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from types import TracebackType
from uuid import uuid4

import pytest

from procurement_bot.supplier_outreach import (
    _quantity,
    prepare_supplier_outreach,
    product_label,
)


def test_supplier_quantity_is_readable() -> None:
    assert _quantity(Decimal("40.000000"), "Штука") == "40 шт."
    assert _quantity(Decimal("1.000000"), "кг") == "1 кг"


def test_generic_catalog_name_uses_short_product_identity_from_spec() -> None:
    spec = (
        "Плита для потолка АРМСТРОНГ. Потолочная панель декоративная "
        "тепло- и звукоизоляционная, 60 x 60 см. По согласованию с заказчиком"
    )
    assert product_label("Плитка", spec) == "потолочная панель Armstrong"


def test_pilot_messages_name_actual_products_from_spec() -> None:
    assert product_label("Пакля", "Лён сантехнический, коса 1кг.") == "Лён сантехнический"
    assert product_label("Доводчик дверной", "Петля с доводчиком для ДСП, серебряный") == (
        "Петля с доводчиком для ДСП"
    )
    assert product_label(
        "Машина стационарная для сварки", "Аппарат для сварки пластиковых труб 2000 Вт."
    ) == "Аппарат для сварки пластиковых труб 2000 Вт"


def test_product_label_never_copies_a_long_specification() -> None:
    label = product_label(
        "Кабель",
        "Кабель силовой медный для внутренней прокладки, сечение 3х2.5 мм2, "
        "изоляция ПВХ, цвет белый, напряжение 220В, сертификат соответствия",
    )
    assert len(label) <= 72
    assert "сечение" not in label
    assert "сертификат" not in label


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _NoWhatsAppConnection:
    def __init__(self) -> None:
        self.owner_id = uuid4()
        self.events: list[tuple[object, ...]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, sql: str, *_args: object):
        if "FROM procurement_cases" in sql:
            return {
                "owner_user_id": self.owner_id,
                "telegram_id": 123,
                "status": "evaluating",
            }
        raise AssertionError(sql)

    async def fetch(self, _sql: str, *_args: object):
        return []

        async def fetch(self, sql: str, *_args: object):
            assert "contact.contact_type='whatsapp'" in sql
            assert "source_evidence" in sql
            assert "research_runs" in sql
            assert "offer_observations" in sql
            assert "contact_evidence_ids" in sql
            return []

    async def fetchval(self, sql: str, *_args: object):
        assert "count(DISTINCT offer.supplier_id)" in sql
        return 3

    async def execute(self, sql: str, *args: object) -> str:
        assert "supplier_outreach_prepared" in sql
        self.events.append(args)
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_outreach_does_not_treat_plain_phone_as_whatsapp() -> None:
    connection = _NoWhatsAppConnection()
    case_id = uuid4()

    result = await prepare_supplier_outreach(
        connection,  # type: ignore[arg-type]
        case_id=case_id,
        session_name="default",
    )

    assert result.proposals_created == 0
    assert result.suppliers_without_whatsapp == 3
    assert len(connection.events) == 1


@pytest.mark.asyncio
async def test_outreach_preserves_existing_manual_conversation() -> None:
    class _ManualConversationConnection(_NoWhatsAppConnection):
        def __init__(self) -> None:
            super().__init__()
            self.supplier_id = uuid4()
            self.contact_id = uuid4()
            self.offer_id = uuid4()

        async def fetch(self, sql: str, *_args: object):
            assert "contact.contact_type='whatsapp'" in sql
            return [{
                "supplier_id": self.supplier_id,
                "contact_id": self.contact_id,
                "offer_id": self.offer_id,
                "normalized_value": "+77085766841",
            }]

        async def fetchrow(self, sql: str, *_args: object):
            if "FROM supplier_conversations" in sql:
                return {"idempotency_key": "owner-test-dialogue"}
            return await super().fetchrow(sql, *_args)

        async def fetchval(self, sql: str, *_args: object):
            assert "count(DISTINCT offer.supplier_id)" in sql
            return 1

    connection = _ManualConversationConnection()
    result = await prepare_supplier_outreach(
        connection,  # type: ignore[arg-type]
        case_id=uuid4(),
        session_name="default",
    )

    assert result.proposals_created == 0
    assert result.skipped_existing == 1
