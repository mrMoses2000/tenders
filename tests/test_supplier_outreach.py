from contextlib import AbstractAsyncContextManager
from types import TracebackType
from uuid import uuid4

import pytest

from procurement_bot.supplier_outreach import prepare_supplier_outreach


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

    async def fetch(self, sql: str, *_args: object):
        assert "contact.contact_type='whatsapp'" in sql
        assert "source_evidence" in sql
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
