"""Append-only quantity events for the parts of fulfilment outside purchases/acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import asyncpg

EventKind = Literal[
    "warehouse_received", "customer_delivered", "customer_rejected", "returned", "cancelled"
]


class ItemExecutionConflict(RuntimeError):
    """An event would violate case ownership, quantity or idempotency rules."""


@dataclass(frozen=True, slots=True)
class ItemExecutionTotals:
    warehouse_received: Decimal
    delivered: Decimal
    rejected: Decimal
    returned: Decimal
    cancelled: Decimal
    accepted: Decimal


async def record_item_execution_event(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    request_item_id: UUID,
    owner_user_id: UUID,
    event_kind: EventKind,
    quantity_delta: Decimal,
    idempotency_key: str,
    reason: str = "",
    source_artifact_id: UUID | None = None,
    replaces_event_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> UUID:
    """Record one independently auditable movement, rejection or cancellation."""

    if event_kind not in {
        "warehouse_received", "customer_delivered", "customer_rejected",
        "returned", "cancelled",
    }:
        raise ValueError("unsupported event_kind")
    if (not quantity_delta.is_finite() or quantity_delta == 0
            or quantity_delta.as_tuple().exponent < -6):
        raise ValueError("quantity_delta must be nonzero with at most six decimals")
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must be non-empty")
    normalized_reason = " ".join(reason.split())
    if (quantity_delta < 0 or event_kind in {"customer_rejected", "returned", "cancelled"}) \
            and not normalized_reason:
        raise ValueError("a correction, rejection, return or cancellation needs a reason")
    if replaces_event_id is not None and event_kind != "customer_delivered":
        raise ValueError("only a customer delivery can replace a rejected delivery")

    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"item-execution:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT * FROM request_item_execution_events WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            expected = {
                "case_id": case_id,
                "request_item_id": request_item_id,
                "event_kind": event_kind,
                "quantity_delta": quantity_delta,
                "reason": normalized_reason,
                "source_artifact_id": source_artifact_id,
                "replaces_event_id": replaces_event_id,
                "recorded_by": owner_user_id,
            }
            if any(existing[key] != value for key, value in expected.items()):
                raise ItemExecutionConflict("idempotency key represents a different event")
            if occurred_at is not None and existing["occurred_at"] != occurred_at:
                raise ItemExecutionConflict("idempotency key represents a different event")
            return existing["id"]
        item = await connection.fetchrow(
            """
            SELECT i.quantity FROM request_items i
            JOIN procurement_cases c ON c.id=i.case_id
            WHERE i.id=$1 AND i.case_id=$2 AND c.owner_user_id=$3
            FOR UPDATE OF i
            """,
            request_item_id, case_id, owner_user_id,
        )
        if item is None:
            raise ItemExecutionConflict("item is absent or belongs to another owner")
        if source_artifact_id is not None:
            valid_artifact = await connection.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM source_artifacts a
                    JOIN incoming_attachments ia ON ia.id=a.attachment_id
                    JOIN messages m ON m.id=ia.message_id
                    WHERE a.id=$1 AND m.user_id=$2
                )
                """,
                source_artifact_id, owner_user_id,
            )
            if valid_artifact is not True:
                raise ItemExecutionConflict("event evidence belongs to another user")
        if replaces_event_id is not None:
            replaced = await connection.fetchval(
                "SELECT event_kind FROM request_item_execution_events "
                "WHERE id=$1 AND request_item_id=$2",
                replaces_event_id, request_item_id,
            )
            if replaced != "customer_rejected":
                raise ItemExecutionConflict("replacement must refer to a rejection")
        event_id = uuid4()
        await connection.execute(
            """
            INSERT INTO request_item_execution_events(
                id,case_id,request_item_id,event_kind,quantity_delta,reason,
                replaces_event_id,source_artifact_id,provenance,occurred_at,
                idempotency_key,recorded_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE($10,now()),$11,$12)
            """,
            event_id, case_id, request_item_id, event_kind, quantity_delta,
            normalized_reason, replaces_event_id, source_artifact_id,
            "documented" if source_artifact_id else "user_reported",
            occurred_at, idempotency_key, owner_user_id,
        )
        totals = await load_item_execution_totals(connection, request_item_id)
        if min(totals.warehouse_received, totals.delivered, totals.rejected,
               totals.returned, totals.cancelled) < 0:
            raise ItemExecutionConflict("a correction exceeds the recorded quantity")
        if totals.returned > totals.delivered:
            raise ItemExecutionConflict("returned quantity exceeds delivered quantity")
        if totals.rejected > totals.delivered:
            raise ItemExecutionConflict("rejected quantity exceeds delivered quantity")
        if (event_kind == "cancelled" and item["quantity"] is not None
                and totals.cancelled + totals.accepted > Decimal(item["quantity"])):
            raise ItemExecutionConflict("accepted and cancelled exceed requested quantity")
        return event_id


async def load_item_execution_totals(
    connection: asyncpg.Connection, request_item_id: UUID
) -> ItemExecutionTotals:
    row = await connection.fetchrow(
        """
        SELECT t.warehouse_received_quantity,t.delivered_quantity,
               t.rejected_quantity,t.returned_quantity,t.cancelled_quantity,
               COALESCE((
                   SELECT sum(CASE WHEN e.event_type='accepted' THEN e.accepted_quantity
                                   ELSE -e.accepted_quantity END)
                   FROM customer_acceptance_events e
                   WHERE e.request_item_id=$1
               ),0) AS accepted_quantity
        FROM request_item_execution_totals t WHERE t.request_item_id=$1
        """,
        request_item_id,
    )
    if row is None:
        accepted = await connection.fetchval(
            """
            SELECT COALESCE(sum(CASE WHEN event_type='accepted' THEN accepted_quantity
                                     ELSE -accepted_quantity END),0)
            FROM customer_acceptance_events WHERE request_item_id=$1
            """,
            request_item_id,
        )
        return ItemExecutionTotals(*(Decimal(0) for _ in range(5)), Decimal(accepted))
    return ItemExecutionTotals(
        Decimal(row["warehouse_received_quantity"]), Decimal(row["delivered_quantity"]),
        Decimal(row["rejected_quantity"]), Decimal(row["returned_quantity"]),
        Decimal(row["cancelled_quantity"]), Decimal(row["accepted_quantity"]),
    )
