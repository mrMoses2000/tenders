"""Case-owned transport runs with exact cargo lines and auditable state changes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.accounting import AccountingConflict
from procurement_bot.phone import normalize_phone


class DeliveryConflict(AccountingConflict):
    """A delivery fact conflicts with ownership, cargo, or existing history."""


@dataclass(frozen=True, slots=True)
class DeliveryLine:
    request_item_id: UUID
    quantity: Decimal
    purchase_item_id: UUID | None = None


_TRANSITIONS = {
    "planned": frozenset({"in_transit", "cancelled"}),
    "in_transit": frozenset({"arrived", "completed", "cancelled"}),
    "arrived": frozenset({"completed", "cancelled"}),
}


def _clean(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) > limit:
        raise ValueError("delivery field is too long")
    return text


def _fingerprint(
    case_id: UUID, owner_user_id: UUID, lines: tuple[DeliveryLine, ...],
    carrier_name: str, carrier_phone: str, tracking_ref: str, note: str,
    expected_at: datetime | None,
) -> str:
    payload = {
        "case_id": str(case_id), "owner": str(owner_user_id),
        "items": sorted((str(line.request_item_id), str(line.quantity),
                         str(line.purchase_item_id) if line.purchase_item_id else "")
                        for line in lines),
        "carrier_name": carrier_name, "carrier_phone": carrier_phone,
        "tracking_ref": tracking_ref, "note": note,
        "expected_at": expected_at.isoformat() if expected_at else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def create_delivery_run(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    lines: tuple[DeliveryLine, ...],
    idempotency_key: str,
    carrier_name: str = "",
    carrier_phone: str = "",
    tracking_ref: str = "",
    note: str = "",
    expected_at: datetime | None = None,
) -> UUID:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must be non-empty")
    if not lines or len(lines) > 200 or len({line.request_item_id for line in lines}) != len(lines):
        raise ValueError("delivery needs unique cargo positions")
    if any(not line.quantity.is_finite() or line.quantity <= 0
           or line.quantity.as_tuple().exponent < -6 for line in lines):
        raise ValueError("delivery quantities must be positive with at most six decimals")
    carrier_name = _clean(carrier_name, 200)
    carrier_phone = normalize_phone(carrier_phone) if carrier_phone.strip() else ""
    tracking_ref = _clean(tracking_ref, 120)
    note = _clean(note, 1000)
    if expected_at is not None and expected_at.tzinfo is None:
        raise ValueError("expected_at must include a timezone")
    request_hash = _fingerprint(case_id, owner_user_id, lines, carrier_name,
                                carrier_phone, tracking_ref, note, expected_at)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"delivery:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT id,request_hash FROM delivery_runs WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise DeliveryConflict("delivery idempotency key has different facts")
            return existing["id"]
        case = await connection.fetchrow(
            """SELECT c.status,COALESCE(f.currency,'KZT') AS currency
               FROM procurement_cases c
               LEFT JOIN case_financials f ON f.case_id=c.id
               WHERE c.id=$1 AND c.owner_user_id=$2 FOR UPDATE OF c""",
            case_id, owner_user_id,
        )
        if case is None:
            raise DeliveryConflict("case is absent or belongs to another owner")
        if case["status"] in {"closed", "cancelled"}:
            raise DeliveryConflict("case is closed; reopen it before recording delivery")
        currency = case["currency"]
        item_ids = [line.request_item_id for line in lines]
        rows = await connection.fetch(
            "SELECT id FROM request_items WHERE case_id=$1 AND id=ANY($2::uuid[]) FOR SHARE",
            case_id, item_ids,
        )
        if {row["id"] for row in rows} != set(item_ids):
            raise DeliveryConflict("cargo includes an item from another case")
        for line in lines:
            if line.purchase_item_id is not None:
                purchased = await connection.fetchval(
                    """SELECT purchased_quantity FROM purchase_items WHERE id=$1
                       AND case_id=$2 AND request_item_id=$3 FOR SHARE""",
                    line.purchase_item_id, case_id, line.request_item_id,
                )
                if purchased is None:
                    raise DeliveryConflict("purchase line does not match cargo position")
                already_shipped = await connection.fetchval(
                    """SELECT COALESCE(sum(di.quantity),0)
                       FROM delivery_run_items di
                       JOIN delivery_runs r ON r.id=di.delivery_run_id
                       WHERE di.purchase_item_id=$1 AND r.status<>'cancelled'""",
                    line.purchase_item_id,
                )
                if Decimal(already_shipped) + line.quantity > Decimal(purchased):
                    raise DeliveryConflict("cargo exceeds the linked purchase quantity")
        carrier_id = None
        if carrier_phone:
            carrier_id = await connection.fetchval(
                """INSERT INTO delivery_carriers(
                       owner_user_id,provider_kind,company_name,normalized_phone
                   ) VALUES ($1,'other',$2,$3)
                   ON CONFLICT (owner_user_id,normalized_phone) DO UPDATE SET
                       company_name=CASE WHEN EXCLUDED.company_name<>''
                           THEN EXCLUDED.company_name ELSE delivery_carriers.company_name END,
                       updated_at=now()
                   RETURNING id""",
                owner_user_id, carrier_name, carrier_phone,
            )
        run_id = uuid4()
        await connection.execute(
            """INSERT INTO delivery_runs(
                   id,case_id,carrier_id,total_cost,currency,status,
                   carrier_name,tracking_ref,expected_at,note,request_hash,
                   idempotency_key,recorded_by
               ) VALUES ($1,$2,$3,NULL,$4,'planned',$5,$6,$7,$8,$9,$10,$11)""",
            run_id, case_id, carrier_id, currency, carrier_name, tracking_ref,
            expected_at, note, request_hash, idempotency_key, owner_user_id,
        )
        for line in lines:
            await connection.execute(
                """INSERT INTO delivery_run_items(
                       delivery_run_id,case_id,request_item_id,purchase_item_id,quantity
                   ) VALUES ($1,$2,$3,$4,$5)""",
                run_id, case_id, line.request_item_id, line.purchase_item_id,
                line.quantity,
            )
        return run_id


async def transition_delivery_run(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    delivery_run_id: UUID,
    owner_user_id: UUID,
    to_status: str,
    idempotency_key: str,
    reason: str = "",
) -> UUID:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must be non-empty")
    reason = _clean(reason, 1000)
    if to_status == "cancelled" and not reason:
        raise ValueError("cancellation needs a reason")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"delivery-status:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT * FROM delivery_status_events WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            if (existing["case_id"] != case_id or existing["delivery_run_id"] != delivery_run_id
                    or existing["recorded_by"] != owner_user_id
                    or existing["to_status"] != to_status or existing["reason"] != reason):
                raise DeliveryConflict("status idempotency key has different facts")
            return existing["id"]
        case_status = await connection.fetchval(
            "SELECT status FROM procurement_cases WHERE id=$1 AND owner_user_id=$2 FOR UPDATE",
            case_id, owner_user_id,
        )
        if case_status is None:
            raise DeliveryConflict("case is absent or belongs to another owner")
        if case_status in {"closed", "cancelled"}:
            raise DeliveryConflict("case is closed; reopen it before recording delivery")
        run = await connection.fetchrow(
            """SELECT r.status FROM delivery_runs r
               JOIN procurement_cases c ON c.id=r.case_id
               WHERE r.id=$1 AND r.case_id=$2 AND c.owner_user_id=$3
               FOR UPDATE OF r""",
            delivery_run_id, case_id, owner_user_id,
        )
        if run is None:
            raise DeliveryConflict("delivery is absent or belongs to another owner")
        from_status = run["status"]
        if to_status not in _TRANSITIONS.get(from_status, frozenset()):
            raise DeliveryConflict(f"invalid delivery transition: {from_status} -> {to_status}")
        if to_status == "cancelled":
            has_movement = await connection.fetchval(
                """SELECT EXISTS(
                       SELECT 1 FROM request_item_execution_events
                       WHERE delivery_run_id=$1
                       UNION ALL
                       SELECT 1 FROM customer_acceptance_events
                       WHERE delivery_run_id=$1
                   )""",
                delivery_run_id,
            )
            if has_movement:
                raise DeliveryConflict("delivery with recorded cargo movement cannot be cancelled")
        event_id = uuid4()
        await connection.execute(
            """INSERT INTO delivery_status_events(
                   id,case_id,delivery_run_id,from_status,to_status,reason,
                   idempotency_key,recorded_by
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            event_id, case_id, delivery_run_id, from_status, to_status,
            reason, idempotency_key, owner_user_id,
        )
        await connection.execute(
            "UPDATE delivery_runs SET status=$2 WHERE id=$1",
            delivery_run_id, to_status,
        )
        return event_id
