from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from procurement_bot.delivery_settlement import (
    DeliverySettlementConflict,
    record_delivery_payment,
    record_delivery_terms,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ClosedCaseConnection:
    def transaction(self):
        return _Transaction()

    async def execute(self, *_args: object):
        return "OK"

    async def fetchval(self, sql: str, *_args: object):
        if "FROM procurement_cases WHERE" in sql:
            return "closed"
        raise AssertionError(f"unexpected query: {sql}")

    async def fetchrow(self, sql: str, *_args: object):
        if "FROM delivery_run_terms" in sql or "FROM delivery_payments" in sql:
            return None
        raise AssertionError(f"unexpected query: {sql}")


@pytest.mark.asyncio
async def test_closed_case_rejects_new_delivery_terms_and_payments() -> None:
    connection = _ClosedCaseConnection()
    case_id, run_id, owner_id = uuid4(), uuid4(), uuid4()

    with pytest.raises(DeliverySettlementConflict, match="case is closed"):
        await record_delivery_terms(
            connection, case_id=case_id, delivery_run_id=run_id,
            owner_user_id=owner_id, loading_mode="unknown",
            included_loading_value=None, note="", source_artifact_id=None,
            idempotency_key="new-terms",
        )  # type: ignore[arg-type]

    with pytest.raises(DeliverySettlementConflict, match="case is closed"):
        await record_delivery_payment(
            connection, case_id=case_id, delivery_run_id=run_id,
            owner_user_id=owner_id, kind="payment", amount=Decimal("1.00"),
            paid_at=datetime.now(UTC), payee="", note="",
            source_artifact_id=None, idempotency_key="new-payment",
        )  # type: ignore[arg-type]
