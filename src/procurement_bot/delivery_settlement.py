"""Auditable loader terms and payments for a case-owned transport run."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from procurement_bot.accounting import AccountingConflict


class DeliverySettlementConflict(AccountingConflict):
    """A settlement fact conflicts with the run, owner, or earlier facts."""


def _money(value: Decimal, *, positive: bool = False) -> None:
    if not value.is_finite() or value.as_tuple().exponent < -2 or value < 0:
        raise ValueError("amount must be nonnegative with at most two decimals")
    if positive and value == 0:
        raise ValueError("payment amount must be positive")


async def _lock_case(
    connection: asyncpg.Connection, case_id: UUID, owner_id: UUID,
) -> str:
    status = await connection.fetchval(
        "SELECT status FROM procurement_cases WHERE id=$1 AND owner_user_id=$2 FOR UPDATE",
        case_id, owner_id,
    )
    if status is None:
        raise DeliverySettlementConflict("case is absent or belongs to another owner")
    return status


async def _owned_run(
    connection: asyncpg.Connection, case_id: UUID, run_id: UUID, owner_id: UUID,
) -> asyncpg.Record:
    row = await connection.fetchrow(
        """SELECT r.id,r.currency FROM delivery_runs r
           JOIN procurement_cases c ON c.id=r.case_id
           WHERE r.id=$1 AND r.case_id=$2 AND c.owner_user_id=$3
           FOR UPDATE OF r""",
        run_id, case_id, owner_id,
    )
    if row is None:
        raise DeliverySettlementConflict("delivery is absent or belongs to another owner")
    return row


async def _check_artifact(
    connection: asyncpg.Connection, artifact_id: UUID | None, owner_id: UUID,
) -> None:
    if artifact_id is None:
        return
    exists = await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM source_artifacts a
           JOIN incoming_attachments ia ON ia.id=a.attachment_id
           JOIN messages m ON m.id=ia.message_id
           WHERE a.id=$1 AND m.user_id=$2)""",
        artifact_id, owner_id,
    )
    if not exists:
        raise DeliverySettlementConflict("document belongs to another user")


async def record_delivery_terms(
    connection: asyncpg.Connection, *, case_id: UUID, delivery_run_id: UUID,
    owner_user_id: UUID, loading_mode: str, included_loading_value: Decimal | None,
    note: str, source_artifact_id: UUID | None, idempotency_key: str,
) -> UUID:
    if loading_mode not in {"unknown", "included", "separate", "not_required"}:
        raise ValueError("unsupported loader terms")
    if not idempotency_key.strip() or len(note) > 1000:
        raise ValueError("invalid terms key or note")
    if included_loading_value is not None:
        _money(included_loading_value)
    if loading_mode != "included" and included_loading_value is not None:
        raise ValueError("included loader value requires included mode")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"delivery-terms:{idempotency_key}",
        )
        case_status = await _lock_case(connection, case_id, owner_user_id)
        existing = await connection.fetchrow(
            "SELECT * FROM delivery_run_terms WHERE idempotency_key=$1", idempotency_key,
        )
        if existing is not None:
            expected = (case_id, delivery_run_id, owner_user_id, loading_mode,
                        included_loading_value, note.strip(), source_artifact_id)
            actual = tuple(existing[key] for key in (
                "case_id", "delivery_run_id", "recorded_by", "loading_mode",
                "included_loading_value", "note", "source_artifact_id"
            ))
            if actual != expected:
                raise DeliverySettlementConflict("terms idempotency key has different facts")
            return existing["id"]
        if case_status in {"closed", "cancelled"}:
            raise DeliverySettlementConflict(
                "case is closed; reopen it before recording delivery terms"
            )
        await _owned_run(connection, case_id, delivery_run_id, owner_user_id)
        await _check_artifact(connection, source_artifact_id, owner_user_id)
        if loading_mode == "included":
            separate_handling = await connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM case_expenses
                   WHERE delivery_run_id=$1 AND category='handling' AND cost_kind='actual')""",
                delivery_run_id,
            )
            if separate_handling:
                raise DeliverySettlementConflict(
                    "included loading conflicts with a separate actual handling expense"
                )
        return await connection.fetchval(
            """INSERT INTO delivery_run_terms(
                   case_id,delivery_run_id,loading_mode,included_loading_value,
                   note,source_artifact_id,recorded_by,idempotency_key
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            case_id, delivery_run_id, loading_mode, included_loading_value,
            note.strip(), source_artifact_id, owner_user_id, idempotency_key,
        )


async def record_delivery_payment(
    connection: asyncpg.Connection, *, case_id: UUID, delivery_run_id: UUID,
    owner_user_id: UUID, kind: str, amount: Decimal, paid_at: datetime,
    payee: str, note: str, source_artifact_id: UUID | None, idempotency_key: str,
) -> UUID:
    if kind not in {"payment", "refund"}:
        raise ValueError("unsupported payment kind")
    _money(amount, positive=True)
    if paid_at.tzinfo is None or paid_at.utcoffset() is None:
        raise ValueError("payment time must include a timezone")
    if not idempotency_key.strip() or len(payee) > 200 or len(note) > 1000:
        raise ValueError("invalid payment key, payee, or note")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"delivery-payment:{idempotency_key}",
        )
        case_status = await _lock_case(connection, case_id, owner_user_id)
        existing = await connection.fetchrow(
            "SELECT * FROM delivery_payments WHERE idempotency_key=$1", idempotency_key,
        )
        if existing is not None:
            expected = (case_id, delivery_run_id, owner_user_id, kind, amount,
                        paid_at, payee.strip(), note.strip(), source_artifact_id)
            actual = tuple(existing[key] for key in (
                "case_id", "delivery_run_id", "recorded_by", "kind", "amount",
                "paid_at", "payee", "note", "source_artifact_id"
            ))
            if actual != expected:
                raise DeliverySettlementConflict("payment idempotency key has different facts")
            return existing["id"]
        if case_status in {"closed", "cancelled"}:
            raise DeliverySettlementConflict(
                "case is closed; reopen it before recording delivery payment"
            )
        run = await _owned_run(connection, case_id, delivery_run_id, owner_user_id)
        await _check_artifact(connection, source_artifact_id, owner_user_id)
        if kind == "refund":
            net_paid = await connection.fetchval(
                """SELECT COALESCE(sum(CASE WHEN kind='payment' THEN amount ELSE -amount END),0)
                   FROM delivery_payments WHERE delivery_run_id=$1""",
                delivery_run_id,
            )
            if amount > net_paid:
                raise DeliverySettlementConflict("refund exceeds recorded payments")
        return await connection.fetchval(
            """INSERT INTO delivery_payments(
                   case_id,delivery_run_id,kind,amount,currency,paid_at,payee,note,
                   source_artifact_id,recorded_by,idempotency_key
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id""",
            case_id, delivery_run_id, kind, amount, run["currency"], paid_at,
            payee.strip(), note.strip(), source_artifact_id, owner_user_id,
            idempotency_key,
        )
