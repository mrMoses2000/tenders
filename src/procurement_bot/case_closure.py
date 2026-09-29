"""Owner-controlled closure after quantity and delivery facts reconcile."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg


class CaseClosureConflict(RuntimeError):
    """A case cannot move to the requested final state."""


@dataclass(frozen=True, slots=True)
class ClosureIssue:
    code: str
    message: str
    request_item_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ClosureReadiness:
    case_id: UUID
    status: str
    issues: tuple[ClosureIssue, ...]
    item_count: int

    @property
    def ready(self) -> bool:
        return not self.issues


async def ensure_case_editable(
    connection: asyncpg.Connection, *, case_id: UUID, owner_user_id: UUID,
) -> None:
    """Call under an outer transaction before any Mini App fulfilment write."""
    status = await connection.fetchval(
        "SELECT status FROM procurement_cases WHERE id=$1 AND owner_user_id=$2 FOR UPDATE",
        case_id, owner_user_id,
    )
    if status is None:
        raise CaseClosureConflict("Заявка не найдена")
    if status in {"closed", "cancelled"}:
        raise CaseClosureConflict("Заявка закрыта. Сначала откройте её повторно")


async def load_case_closure_readiness(
    connection: asyncpg.Connection, *, case_id: UUID, owner_user_id: UUID,
) -> ClosureReadiness:
    case = await connection.fetchrow(
        "SELECT status FROM procurement_cases WHERE id=$1 AND owner_user_id=$2",
        case_id, owner_user_id,
    )
    if case is None:
        raise CaseClosureConflict("Заявка не найдена")
    issues: list[ClosureIssue] = []
    items = await connection.fetch(
        """
        SELECT i.id,i.line_number,i.quantity,
               COALESCE(p.purchased,0) AS purchased,
               COALESCE(p.unknown_cost,0) AS unknown_cost,
               COALESCE(e.delivered,0) AS delivered,
               COALESCE(e.rejected,0) AS rejected,
               COALESCE(e.returned,0) AS returned,
               COALESCE(e.cancelled,0) AS cancelled,
               COALESCE(a.accepted,0) AS accepted
        FROM request_items i
        LEFT JOIN LATERAL (
            SELECT sum(purchased_quantity) AS purchased,
                   count(*) FILTER (WHERE unit_cost IS NULL) AS unknown_cost
            FROM purchase_items WHERE request_item_id=i.id
        ) p ON TRUE
        LEFT JOIN LATERAL (
            SELECT sum(quantity_delta) FILTER (
                       WHERE event_kind='customer_delivered') AS delivered,
                   sum(quantity_delta) FILTER (
                       WHERE event_kind='customer_rejected') AS rejected,
                   sum(quantity_delta) FILTER (
                       WHERE event_kind='returned') AS returned,
                   sum(quantity_delta) FILTER (
                       WHERE event_kind='cancelled') AS cancelled
            FROM request_item_execution_events WHERE request_item_id=i.id
        ) e ON TRUE
        LEFT JOIN LATERAL (
            SELECT sum(CASE WHEN event_type='accepted' THEN accepted_quantity
                            ELSE -accepted_quantity END) AS accepted
            FROM customer_acceptance_events WHERE request_item_id=i.id
        ) a ON TRUE
        WHERE i.case_id=$1 ORDER BY i.line_number
        """,
        case_id,
    )
    if not items:
        issues.append(ClosureIssue("empty_case", "В заявке нет позиций"))
    for row in items:
        prefix = f"Позиция №{row['line_number']}: "
        item_id = row["id"]
        required = row["quantity"]
        accepted = Decimal(row["accepted"])
        cancelled = Decimal(row["cancelled"])
        purchased = Decimal(row["purchased"])
        delivered = Decimal(row["delivered"])
        rejected = Decimal(row["rejected"])
        returned = Decimal(row["returned"])
        if required is None:
            issues.append(ClosureIssue(
                "unknown_quantity", prefix + "не указано требуемое количество", item_id,
            ))
        elif accepted + cancelled != Decimal(required):
            remaining = Decimal(required) - accepted - cancelled
            issues.append(ClosureIssue(
                "unresolved_quantity",
                prefix + f"не распределено {remaining} между приёмкой и отменой",
                item_id,
            ))
        if accepted > purchased:
            issues.append(ClosureIssue(
                "purchase_shortfall", prefix + "принято больше, чем закуплено", item_id,
            ))
        if accepted > delivered - rejected - returned:
            issues.append(ClosureIssue(
                "delivery_shortfall",
                prefix + "принято больше, чем передано без отказов и возвратов",
                item_id,
            ))
        if row["unknown_cost"]:
            issues.append(ClosureIssue(
                "unknown_purchase_cost", prefix + "не указана цена закупки", item_id,
            ))

    runs = await connection.fetch(
        """
        SELECT r.id,r.status,r.total_cost,
               EXISTS(SELECT 1 FROM case_expenses e
                      WHERE e.delivery_run_id=r.id AND e.category='delivery'
                        AND e.cost_kind='actual') AS has_actual_cost
        FROM delivery_runs r WHERE r.case_id=$1 ORDER BY r.created_at,r.id
        """,
        case_id,
    )
    for run in runs:
        label = f"Доставка {str(run['id'])[:8]}: "
        if run["status"] in {"planned", "in_transit", "arrived"}:
            issues.append(ClosureIssue("active_delivery", label + "рейс ещё не завершён"))
        elif run["status"] == "completed":
            if run["total_cost"] is None and not run["has_actual_cost"]:
                issues.append(ClosureIssue(
                    "unknown_delivery_cost", label + "нет фактической стоимости",
                ))
            cargo = await connection.fetch(
                """
                SELECT di.request_item_id,di.quantity,i.line_number,
                       COALESCE(sum(e.quantity_delta) FILTER (
                           WHERE e.event_kind='warehouse_received'),0) AS received,
                       COALESCE(sum(e.quantity_delta) FILTER (
                           WHERE e.event_kind='customer_delivered'),0) AS delivered
                FROM delivery_run_items di
                JOIN request_items i ON i.id=di.request_item_id
                LEFT JOIN request_item_execution_events e
                  ON e.delivery_run_id=di.delivery_run_id
                 AND e.request_item_id=di.request_item_id
                WHERE di.delivery_run_id=$1
                GROUP BY di.request_item_id,di.quantity,i.line_number
                """,
                run["id"],
            )
            for line in cargo:
                if line["quantity"] is not None and max(
                    Decimal(line["received"]), Decimal(line["delivered"])
                ) < Decimal(line["quantity"]):
                    issues.append(ClosureIssue(
                        "unaccounted_cargo",
                        label + "нет события поступления или передачи "
                        + f"для позиции №{line['line_number']}",
                        line["request_item_id"],
                    ))
    return ClosureReadiness(case_id, case["status"], tuple(issues), len(items))


async def change_case_closure(
    connection: asyncpg.Connection, *, case_id: UUID, owner_user_id: UUID,
    action: str, idempotency_key: str, reason: str = "",
) -> UUID:
    if action not in {"closed", "reopened"} or not idempotency_key.strip():
        raise ValueError("Неверное действие или ключ повтора")
    reason = " ".join(reason.split())
    if len(reason) > 1000 or (action == "reopened" and not reason):
        raise ValueError("Для повторного открытия укажите причину до 1000 символов")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"case-closure:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT * FROM case_closure_events WHERE idempotency_key=$1", idempotency_key,
        )
        if existing is not None:
            if (existing["case_id"] != case_id or existing["recorded_by"] != owner_user_id
                    or existing["event_kind"] != action or existing["reason"] != reason):
                raise CaseClosureConflict("Ключ повтора уже использован для другого действия")
            return existing["id"]
        case = await connection.fetchrow(
            "SELECT status FROM procurement_cases WHERE id=$1 AND owner_user_id=$2 FOR UPDATE",
            case_id, owner_user_id,
        )
        if case is None:
            raise CaseClosureConflict("Заявка не найдена")
        from_status = case["status"]
        if action == "closed":
            if from_status in {"closed", "cancelled"}:
                raise CaseClosureConflict("Заявка уже закрыта или отменена")
            await connection.fetch(
                "SELECT id FROM request_items WHERE case_id=$1 FOR UPDATE", case_id,
            )
            readiness = await load_case_closure_readiness(
                connection, case_id=case_id, owner_user_id=owner_user_id,
            )
            if not readiness.ready:
                raise CaseClosureConflict(readiness.issues[0].message)
            to_status = "closed"
            await connection.execute(
                "UPDATE request_items SET status='closed',updated_at=now() WHERE case_id=$1",
                case_id,
            )
        else:
            if from_status != "closed":
                raise CaseClosureConflict("Повторно открыть можно только закрытую заявку")
            to_status = "selected"
            await connection.execute(
                "UPDATE request_items SET status='selected',updated_at=now() "
                "WHERE case_id=$1 AND status='closed'",
                case_id,
            )
        event_id = uuid4()
        await connection.execute(
            "UPDATE procurement_cases SET status=$2,updated_at=now() WHERE id=$1",
            case_id, to_status,
        )
        await connection.execute(
            """INSERT INTO case_closure_events(
                   id,case_id,event_kind,from_status,to_status,reason,idempotency_key,recorded_by
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            event_id, case_id, action, from_status, to_status, reason,
            idempotency_key, owner_user_id,
        )
        return event_id
