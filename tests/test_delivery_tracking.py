from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from procurement_bot.accounting import load_financial_snapshot
from procurement_bot.delivery_tracking import (
    DeliveryConflict,
    DeliveryLine,
    _fingerprint,
    create_delivery_run,
    transition_delivery_run,
)


@pytest.mark.asyncio
async def test_delivery_rejects_duplicate_or_unknown_cargo_without_database() -> None:
    item = uuid4()
    arguments = {"case_id": uuid4(), "owner_user_id": uuid4(),
                 "idempotency_key": "test"}
    with pytest.raises(ValueError, match="unique cargo"):
        await create_delivery_run(None, lines=(DeliveryLine(item, Decimal(1)),
                                               DeliveryLine(item, Decimal(2))),
                                  **arguments)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        await create_delivery_run(None, lines=(DeliveryLine(item, Decimal(0)),),
                                  **arguments)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancellation_requires_reason_before_database_write() -> None:
    with pytest.raises(ValueError, match="reason"):
        await transition_delivery_run(
            None, case_id=uuid4(), delivery_run_id=uuid4(),
            owner_user_id=uuid4(), to_status="cancelled", idempotency_key="test",
        )  # type: ignore[arg-type]


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

    async def fetchrow(self, sql: str, *_args: object):
        if "FROM delivery_runs WHERE idempotency_key" in sql:
            return None
        if "FROM delivery_status_events" in sql:
            return None
        if "FROM procurement_cases c" in sql:
            return {"status": "closed", "currency": "KZT"}
        raise AssertionError(f"unexpected query: {sql}")

    async def fetchval(self, sql: str, *_args: object):
        if "FROM procurement_cases WHERE" in sql:
            return "closed"
        raise AssertionError(f"unexpected query: {sql}")


@pytest.mark.asyncio
async def test_closed_case_rejects_new_delivery_and_status_events() -> None:
    case_id, owner_id, item_id = uuid4(), uuid4(), uuid4()
    connection = _ClosedCaseConnection()
    with pytest.raises(DeliveryConflict, match="case is closed"):
        await create_delivery_run(
            connection, case_id=case_id, owner_user_id=owner_id,
            lines=(DeliveryLine(item_id, Decimal(1)),), idempotency_key="new-run",
        )  # type: ignore[arg-type]
    with pytest.raises(DeliveryConflict, match="case is closed"):
        await transition_delivery_run(
            connection, case_id=case_id, delivery_run_id=uuid4(),
            owner_user_id=owner_id, to_status="in_transit", idempotency_key="new-status",
        )  # type: ignore[arg-type]


def test_delivery_fingerprint_is_independent_of_cargo_order() -> None:
    case, owner, first, second = (uuid4() for _ in range(4))
    lines = (DeliveryLine(first, Decimal(4)), DeliveryLine(second, Decimal(12)))
    values = (case, owner, "Carrier", "", "Run 1", "", None)
    assert _fingerprint(case, owner, lines, *values[2:]) == _fingerprint(
        case, owner, tuple(reversed(lines)), *values[2:]
    )


@pytest.mark.asyncio
async def test_unknown_delivery_cost_blocks_provisional_margin() -> None:
    class Connection:
        async def fetchrow(self, _sql: str, *_args: object):
            return {
                "tender_revenue_private": Decimal(10000), "currency": "KZT",
                "product_cost": Decimal(4000), "delivery_cost": Decimal(0),
                "additional_cost": Decimal(0), "unknown_lines": 0,
                "unknown_delivery_runs": 1,
            }

    result = await load_financial_snapshot(
        Connection(), case_id=uuid4(), owner_user_id=uuid4(),
    )  # type: ignore[arg-type]
    assert result.unknown_delivery_runs == 1
    assert result.provisional_margin is None


def test_delivery_schema_keeps_case_and_item_links() -> None:
    sql = (Path(__file__).parents[1] / "migrations/018_delivery_tracking.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS delivery_run_items" in sql
    assert "FOREIGN KEY (delivery_run_id,case_id)" in sql
    assert "FOREIGN KEY (request_item_id,case_id)" in sql
    assert "ADD COLUMN delivery_run_id UUID" in sql
