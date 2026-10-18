from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.phone import normalize_phone
from procurement_bot.queue import stable_key

_CENT = Decimal("0.01")


class AccountingConflict(RuntimeError):
    """Evidence, ownership, or an idempotent accounting fact is inconsistent."""


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    request_item_id: UUID
    purchased_quantity: Decimal
    unit_cost: Decimal | None
    offer_id: UUID | None = None

    def __post_init__(self) -> None:
        if (not self.purchased_quantity.is_finite() or self.purchased_quantity <= 0
                or self.purchased_quantity.as_tuple().exponent < -6):
            raise ValueError("purchased_quantity must be positive")
        if self.unit_cost is not None and (
            not self.unit_cost.is_finite() or self.unit_cost < 0
            or self.unit_cost.as_tuple().exponent < -4
        ):
            raise ValueError("unit_cost must be nonnegative")


@dataclass(frozen=True, slots=True)
class PurchaseRecordResult:
    document_id: UUID
    recorded_lines: int
    closed_item_ids: tuple[UUID, ...]
    needs_quantity_review: tuple[UUID, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class DeliveryAllocation:
    request_item_id: UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class FinancialSnapshot:
    case_id: UUID
    currency: str
    tender_revenue_private: Decimal | None
    product_cost_known: Decimal
    delivery_cost_known: Decimal
    unknown_purchase_lines: int
    provisional_margin: Decimal | None
    additional_cost_known: Decimal = Decimal(0)
    unknown_delivery_runs: int = 0


@dataclass(frozen=True, slots=True)
class CustomerAcceptanceResult:
    event_id: UUID
    customer_id: UUID
    request_item_id: UUID
    net_accepted_quantity: Decimal
    created: bool


def allocate_equal_delivery(
    total_cost: Decimal,
    request_item_ids: Sequence[UUID],
) -> tuple[DeliveryAllocation, ...]:
    """Split a delivery exactly to cents; the remainder is deterministic."""

    if total_cost < 0:
        raise ValueError("total_cost must be nonnegative")
    unique = tuple(dict.fromkeys(request_item_ids))
    if not unique or len(unique) != len(request_item_ids):
        raise ValueError("request_item_ids must be a non-empty unique sequence")
    quantized = total_cost.quantize(_CENT)
    if quantized != total_cost:
        raise ValueError("total_cost must have at most two decimal places")
    base = (quantized / len(unique)).quantize(_CENT, rounding=ROUND_DOWN)
    remainder_cents = int((quantized - base * len(unique)) / _CENT)
    allocations = tuple(
        DeliveryAllocation(item_id, base + (_CENT if index < remainder_cents else Decimal(0)))
        for index, item_id in enumerate(unique)
    )
    if sum((item.amount for item in allocations), Decimal(0)) != quantized:
        raise AssertionError("delivery allocation lost money")
    return allocations


def allocate_weighted_cost(
    total_cost: Decimal,
    item_weights: Sequence[tuple[UUID, Decimal]],
) -> tuple[DeliveryAllocation, ...]:
    """Allocate cents by positive weights, assigning remainder deterministically."""

    if not total_cost.is_finite() or total_cost < 0 or total_cost.quantize(_CENT) != total_cost:
        raise ValueError("total_cost must be nonnegative with at most two decimals")
    if not item_weights or len({item_id for item_id, _ in item_weights}) != len(item_weights):
        raise ValueError("item_weights must be a non-empty unique sequence")
    if any(not weight.is_finite() or weight <= 0 for _, weight in item_weights):
        raise ValueError("allocation weights must be positive")
    total_weight = sum((weight for _, weight in item_weights), Decimal(0))
    exact = [total_cost * weight / total_weight for _, weight in item_weights]
    floored = [value.quantize(_CENT, rounding=ROUND_DOWN) for value in exact]
    remaining = int((total_cost - sum(floored, Decimal(0))) / _CENT)
    order = sorted(range(len(item_weights)), key=lambda i: (exact[i] - floored[i], -i),
                   reverse=True)
    for index in order[:remaining]:
        floored[index] += _CENT
    return tuple(DeliveryAllocation(item_id, floored[index])
                 for index, (item_id, _) in enumerate(item_weights))


async def record_case_expense(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    category: Literal["delivery", "handling", "packaging", "commission", "travel",
                      "storage", "other"],
    cost_kind: Literal["planned", "actual"],
    total_cost: Decimal,
    request_item_ids: Sequence[UUID],
    allocation_method: Literal["equal", "quantity", "manual"],
    idempotency_key: str,
    delivery_run_id: UUID | None = None,
    manual_amounts: Mapping[UUID, Decimal] | None = None,
    source_artifact_id: UUID | None = None,
    note: str = "",
    incurred_at: datetime | None = None,
    currency: str = "KZT",
) -> UUID:
    """Record an expense and exact item shares in one transaction."""

    if category not in {"delivery", "handling", "packaging", "commission", "travel",
                        "storage", "other"}:
        raise ValueError("unsupported expense category")
    if cost_kind not in {"planned", "actual"}:
        raise ValueError("unsupported cost_kind")
    if allocation_method not in {"equal", "quantity", "manual"}:
        raise ValueError("unsupported allocation_method")
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must be non-empty")
    if not total_cost.is_finite() or total_cost < 0 or total_cost.quantize(_CENT) != total_cost:
        raise ValueError("total_cost must be nonnegative with at most two decimals")
    unique_ids = tuple(sorted(set(request_item_ids)))
    if not unique_ids or len(unique_ids) != len(request_item_ids):
        raise ValueError("request_item_ids must be non-empty and unique")
    normalized_currency = _currency(currency)
    if allocation_method == "manual":
        if manual_amounts is None or set(manual_amounts) != set(unique_ids):
            raise ValueError("manual_amounts must cover exactly the selected items")
        if any(not amount.is_finite() or amount < 0 or amount.quantize(_CENT) != amount
               for amount in manual_amounts.values()):
            raise ValueError("manual amounts must be nonnegative cents")
        if sum(manual_amounts.values(), Decimal(0)) != total_cost:
            raise ValueError("manual amounts do not sum to total_cost")
    elif manual_amounts is not None:
        raise ValueError("manual_amounts requires manual allocation")

    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"case-expense:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT * FROM case_expenses WHERE idempotency_key=$1", idempotency_key
        )
        if existing is not None:
            expected = {
                "case_id": case_id, "category": category, "cost_kind": cost_kind,
                "amount": total_cost, "allocation_method": allocation_method,
                "currency": normalized_currency, "source_artifact_id": source_artifact_id,
                "note": note.strip(), "recorded_by": owner_user_id,
                "incurred_at": incurred_at,
                "delivery_run_id": delivery_run_id,
            }
            if any(existing[key] != value for key, value in expected.items()):
                raise AccountingConflict("expense idempotency key has different facts")
            old_allocations = await connection.fetch(
                "SELECT request_item_id,allocated_amount FROM case_expense_allocations "
                "WHERE expense_id=$1", existing["id"],
            )
            if {row["request_item_id"] for row in old_allocations} != set(unique_ids):
                raise AccountingConflict("expense idempotency key has different items")
            if allocation_method == "manual" and any(
                row["allocated_amount"] != manual_amounts[row["request_item_id"]]
                for row in old_allocations
            ):
                raise AccountingConflict("expense idempotency key has different allocations")
            return existing["id"]
        case_currency = await connection.fetchval(
            """
            SELECT COALESCE(f.currency,'KZT') FROM procurement_cases c
            LEFT JOIN case_financials f ON f.case_id=c.id
            WHERE c.id=$1 AND c.owner_user_id=$2 FOR UPDATE OF c
            """,
            case_id, owner_user_id,
        )
        if case_currency is None:
            raise AccountingConflict("case is absent or belongs to another owner")
        if normalized_currency != case_currency:
            raise AccountingConflict("expense currency differs from case currency")
        rows = await connection.fetch(
            "SELECT id,quantity FROM request_items "
            "WHERE case_id=$1 AND id=ANY($2::uuid[]) FOR SHARE",
            case_id, list(unique_ids),
        )
        by_id = {row["id"]: row for row in rows}
        if set(by_id) != set(unique_ids):
            raise AccountingConflict("expense contains an item from another case")
        delivery_quantities: dict[UUID, Decimal | None] = {}
        if delivery_run_id is not None:
            run = await connection.fetchrow(
                """SELECT r.total_cost FROM delivery_runs r
                   JOIN procurement_cases c ON c.id=r.case_id
                   WHERE r.id=$1 AND r.case_id=$2 AND c.owner_user_id=$3
                   FOR SHARE OF r""",
                delivery_run_id, case_id, owner_user_id,
            )
            if run is None:
                raise AccountingConflict("delivery belongs to another case or owner")
            if category == "delivery" and cost_kind == "actual" and run["total_cost"] is not None:
                raise AccountingConflict("delivery transport cost is already recorded")
            if category == "handling" and cost_kind == "actual":
                latest_loading_mode = await connection.fetchval(
                    """SELECT loading_mode FROM delivery_run_terms
                       WHERE delivery_run_id=$1
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    delivery_run_id,
                )
                if latest_loading_mode == "included":
                    raise AccountingConflict(
                        "loading is already included in this delivery tariff"
                    )
            cargo = await connection.fetch(
                """SELECT request_item_id,quantity FROM delivery_run_items
                   WHERE delivery_run_id=$1""",
                delivery_run_id,
            )
            all_delivery_quantities = {
                row["request_item_id"]: row["quantity"] for row in cargo
            }
            if not set(unique_ids) <= set(all_delivery_quantities):
                raise AccountingConflict("expense includes an item outside this delivery")
            if category == "delivery" and set(unique_ids) != set(all_delivery_quantities):
                raise AccountingConflict("delivery transport cost must cover every cargo item")
            delivery_quantities = {
                item_id: all_delivery_quantities[item_id] for item_id in unique_ids
            }
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
                raise AccountingConflict("expense evidence belongs to another user")
        if allocation_method == "manual":
            assert manual_amounts is not None
            allocations = tuple(DeliveryAllocation(item_id, manual_amounts[item_id])
                                for item_id in unique_ids)
            weights = {item_id: Decimal(1) for item_id in unique_ids}
        else:
            weights = {
                item_id: (Decimal(1) if allocation_method == "equal"
                          else (delivery_quantities[item_id] if delivery_run_id is not None
                                else by_id[item_id]["quantity"]))
                for item_id in unique_ids
            }
            if any(value is None or value <= 0 for value in weights.values()):
                raise AccountingConflict("quantity allocation requires known quantities")
            allocations = allocate_weighted_cost(
                total_cost, [(item_id, weight) for item_id, weight in weights.items()]
            )
        expense_id = uuid4()
        await connection.execute(
            """
            INSERT INTO case_expenses(
                id,case_id,category,cost_kind,amount,currency,allocation_method,
                provenance,source_artifact_id,note,incurred_at,idempotency_key,recorded_by,
                delivery_run_id
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """,
            expense_id, case_id, category, cost_kind, total_cost, normalized_currency,
            allocation_method, "documented" if source_artifact_id else "user_reported",
            source_artifact_id, note.strip(), incurred_at, idempotency_key, owner_user_id,
            delivery_run_id,
        )
        for allocation in allocations:
            await connection.execute(
                """
                INSERT INTO case_expense_allocations(
                    expense_id,case_id,request_item_id,allocated_amount,allocation_weight
                ) VALUES ($1,$2,$3,$4,$5)
                """,
                expense_id, case_id, allocation.request_item_id, allocation.amount,
                weights[allocation.request_item_id],
            )
        return expense_id


async def record_purchase(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    source_artifact_id: UUID | None,
    document_kind: Literal["receipt", "invoice", "waybill", "cash_receipt", "other"],
    lines: Sequence[PurchaseLine],
    idempotency_key: str,
    document_number: str = "",
    seller_name: str = "",
    issued_at: datetime | None = None,
    stated_total: Decimal | None = None,
    currency: str = "KZT",
    note: str = "",
) -> PurchaseRecordResult:
    """Record a documented or explicitly user-reported purchase."""

    if not lines:
        raise ValueError("at least one purchase line is required")
    if len({line.request_item_id for line in lines}) != len(lines):
        raise ValueError("purchase lines must target unique request items")
    if document_kind not in {"receipt", "invoice", "waybill", "cash_receipt", "other"}:
        raise ValueError("unsupported purchase document kind")
    normalized_currency = _currency(currency)
    if stated_total is not None and (not stated_total.is_finite() or stated_total < 0):
        raise ValueError("stated_total must be nonnegative")
    note = " ".join(note.split())
    seller_name = " ".join(seller_name.split())
    if source_artifact_id is None and (not note or not seller_name):
        raise ValueError("reported purchase needs seller and note")
    if len(note) > 1000 or len(seller_name) > 200 or len(document_number) > 120:
        raise ValueError("purchase details are too long")
    request_hash = hashlib.sha256(json.dumps({
        "case": str(case_id), "owner": str(owner_user_id),
        "artifact": str(source_artifact_id) if source_artifact_id else None,
        "kind": document_kind, "number": document_number.strip(),
        "seller": seller_name, "note": note,
        "issued_at": issued_at.isoformat() if issued_at else None,
        "stated_total": str(stated_total) if stated_total is not None else None,
        "currency": normalized_currency,
        "lines": sorted((str(line.request_item_id), str(line.purchased_quantity),
                         str(line.unit_cost) if line.unit_cost is not None else None,
                         str(line.offer_id) if line.offer_id else None)
                        for line in lines),
    }, sort_keys=True).encode()).hexdigest()

    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"purchase:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT id,case_id,source_artifact_id,request_hash FROM purchase_documents "
            "WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            if (
                existing["case_id"] != case_id
                or existing["source_artifact_id"] != source_artifact_id
                or (existing["request_hash"] is not None
                    and existing["request_hash"] != request_hash)
            ):
                raise AccountingConflict("purchase idempotency key has different provenance")
            return await _existing_purchase_result(connection, existing["id"])

        owner_ok = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM procurement_cases "
            "WHERE id=$1 AND owner_user_id=$2)",
            case_id,
            owner_user_id,
        )
        if owner_ok is not True:
            raise AccountingConflict("case does not belong to the acting owner")
        if source_artifact_id is not None:
            artifact_ok = await connection.fetchval(
            """
            SELECT EXISTS(
                SELECT 1
                FROM source_artifacts AS artifact
                JOIN incoming_attachments AS attachment
                  ON attachment.id=artifact.attachment_id
                JOIN messages AS message ON message.id=attachment.message_id
                WHERE artifact.id=$1 AND message.user_id=$2
            )
            """,
                source_artifact_id,
                owner_user_id,
            )
            if artifact_ok is not True:
                raise AccountingConflict("receipt artifact is absent or owned by another user")

        item_rows = await connection.fetch(
            """
            SELECT id,quantity,status
            FROM request_items
            WHERE case_id=$1 AND id=ANY($2::uuid[])
            FOR UPDATE
            """,
            case_id,
            [line.request_item_id for line in lines],
        )
        if {row["id"] for row in item_rows} != {line.request_item_id for line in lines}:
            raise AccountingConflict("a purchase line belongs to another case or is absent")
        offer_ids = [line.offer_id for line in lines if line.offer_id is not None]
        if offer_ids:
            offers = await connection.fetch(
                "SELECT id,request_item_id FROM offers WHERE id=ANY($1::uuid[])", offer_ids,
            )
            offer_items = {row["id"]: row["request_item_id"] for row in offers}
            if any(line.offer_id is not None and offer_items.get(line.offer_id)
                   != line.request_item_id for line in lines):
                raise AccountingConflict("selected supplier offer does not match purchase item")

        document_id = uuid4()
        await connection.execute(
            """
            INSERT INTO purchase_documents(
                id,case_id,source_artifact_id,document_kind,document_number,
                seller_name,issued_at,stated_total,currency,idempotency_key,created_by,
                provenance,note,request_hash
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """,
            document_id,
            case_id,
            source_artifact_id,
            document_kind,
            document_number.strip(),
            seller_name,
            issued_at,
            stated_total,
            normalized_currency,
            idempotency_key,
            owner_user_id,
            "documented" if source_artifact_id else "user_reported",
            note,
            request_hash,
        )
        for line in lines:
            await connection.execute(
                """
                INSERT INTO purchase_items(
                    id,case_id,purchase_document_id,request_item_id,offer_id,
                    purchased_quantity,unit_cost,currency,idempotency_key
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                uuid4(),
                case_id,
                document_id,
                line.request_item_id,
                line.offer_id,
                line.purchased_quantity,
                line.unit_cost,
                normalized_currency,
                stable_key("purchase-line", idempotency_key, line.request_item_id),
            )

        needs_review: list[UUID] = []
        for item in item_rows:
            if item["quantity"] is None:
                needs_review.append(item["id"])
        return PurchaseRecordResult(
            document_id=document_id,
            recorded_lines=len(lines),
            closed_item_ids=(),
            needs_quantity_review=tuple(needs_review),
            created=True,
        )


# Existing callers keep the evidenced name and still pass a source artifact.
record_evidenced_purchase = record_purchase


async def record_equal_delivery(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    request_item_ids: Sequence[UUID],
    total_cost: Decimal,
    provider_kind: str,
    phone: str,
    idempotency_key: str,
    company_name: str = "",
    driver_name: str = "",
    source_url: str | None = None,
    occurred_at: datetime | None = None,
    currency: str = "KZT",
) -> UUID:
    """Persist a reusable driver and split one shared trip across exact positions."""

    if provider_kind not in {"yandex", "indriver", "private", "olx", "company", "other"}:
        raise ValueError("unsupported provider_kind")
    normalized = normalize_phone(phone)
    allocations = allocate_equal_delivery(total_cost, request_item_ids)
    normalized_currency = _currency(currency)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"delivery:{idempotency_key}",
        )
        existing = await connection.fetchval(
            "SELECT id FROM delivery_runs WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            return existing
        rows = await connection.fetch(
            "SELECT id FROM request_items WHERE case_id=$1 AND id=ANY($2::uuid[]) FOR SHARE",
            case_id,
            list(request_item_ids),
        )
        if {row["id"] for row in rows} != set(request_item_ids):
            raise AccountingConflict("delivery contains an item from another case")
        carrier_id = await connection.fetchval(
            """
            INSERT INTO delivery_carriers(
                owner_user_id,provider_kind,company_name,driver_name,
                normalized_phone,source_url
            ) VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (owner_user_id,normalized_phone) DO UPDATE SET
                provider_kind=EXCLUDED.provider_kind,
                company_name=CASE WHEN EXCLUDED.company_name<>''
                                  THEN EXCLUDED.company_name
                                  ELSE delivery_carriers.company_name END,
                driver_name=CASE WHEN EXCLUDED.driver_name<>''
                                 THEN EXCLUDED.driver_name
                                 ELSE delivery_carriers.driver_name END,
                source_url=COALESCE(EXCLUDED.source_url,delivery_carriers.source_url),
                updated_at=now()
            RETURNING id
            """,
            owner_user_id,
            provider_kind,
            company_name.strip(),
            driver_name.strip(),
            normalized,
            source_url,
        )
        run_id = uuid4()
        await connection.execute(
            """
            INSERT INTO delivery_runs(
                id,case_id,carrier_id,total_cost,currency,allocation_method,
                occurred_at,idempotency_key,recorded_by
            ) VALUES ($1,$2,$3,$4,$5,'equal_per_item',COALESCE($6,now()),$7,$8)
            """,
            run_id,
            case_id,
            carrier_id,
            total_cost,
            normalized_currency,
            occurred_at,
            idempotency_key,
            owner_user_id,
        )
        for allocation in allocations:
            await connection.execute(
                """
                INSERT INTO delivery_allocations(
                    case_id,delivery_run_id,request_item_id,allocated_amount,allocation_weight
                ) VALUES ($1,$2,$3,$4,1)
                """,
                case_id,
                run_id,
                allocation.request_item_id,
                allocation.amount,
            )
            await connection.execute(
                """INSERT INTO delivery_run_items(
                       delivery_run_id,case_id,request_item_id
                   ) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
                run_id, case_id, allocation.request_item_id,
            )
        return run_id


async def record_customer_acceptance(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    request_item_id: UUID,
    customer_name: str,
    accepted_quantity: Decimal,
    event_type: Literal["accepted", "revoked"],
    idempotency_key: str,
    source_artifact_id: UUID | None = None,
    delivery_run_id: UUID | None = None,
    reason: str = "",
) -> CustomerAcceptanceResult:
    """Append one recipient acceptance fact without rewriting its history."""

    normalized_name = " ".join(customer_name.split())
    if not normalized_name:
        raise ValueError("customer_name must be non-empty")
    if event_type not in {"accepted", "revoked"}:
        raise ValueError("unsupported acceptance event type")
    if (not accepted_quantity.is_finite() or accepted_quantity <= 0
            or accepted_quantity.as_tuple().exponent < -6):
        raise ValueError("accepted_quantity must be positive")
    reason = " ".join(reason.split())
    if event_type == "revoked" and not reason:
        raise ValueError("acceptance revocation needs a reason")
    if len(reason) > 1000:
        raise ValueError("acceptance reason is too long")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"customer-acceptance:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT * FROM customer_acceptance_events WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            expected = {
                "case_id": case_id,
                "request_item_id": request_item_id,
                "event_type": event_type,
                "accepted_quantity": accepted_quantity,
                "source_artifact_id": source_artifact_id,
                "delivery_run_id": delivery_run_id,
                "reason": reason,
                "recorded_by": owner_user_id,
            }
            if any(existing[key] != value for key, value in expected.items()):
                raise AccountingConflict("acceptance idempotency key has different facts")
            net = await _net_customer_acceptance(
                connection,
                case_id,
                existing["customer_id"],
                request_item_id,
            )
            return CustomerAcceptanceResult(
                existing["id"], existing["customer_id"], request_item_id, net, False
            )

        case = await connection.fetchrow(
            "SELECT customer_id FROM procurement_cases "
            "WHERE id=$1 AND owner_user_id=$2 FOR UPDATE",
            case_id,
            owner_user_id,
        )
        if case is None:
            raise AccountingConflict("case is absent or belongs to another owner")
        item = await connection.fetchrow(
            "SELECT quantity FROM request_items WHERE id=$1 AND case_id=$2 FOR UPDATE",
            request_item_id,
            case_id,
        )
        if item is None:
            raise AccountingConflict("accepted item is absent or belongs to another case")
        customer_id = await connection.fetchval(
            """
            INSERT INTO customers(owner_user_id,display_name)
            VALUES ($1,$2)
            ON CONFLICT (owner_user_id,display_name) DO UPDATE SET
                active=TRUE,updated_at=now()
            RETURNING id
            """,
            owner_user_id,
            normalized_name,
        )
        if case["customer_id"] is None:
            await connection.execute(
                "UPDATE procurement_cases SET customer_id=$2,updated_at=now() WHERE id=$1",
                case_id,
                customer_id,
            )
        elif case["customer_id"] != customer_id:
            raise AccountingConflict("case is already assigned to another customer")
        if source_artifact_id is not None:
            artifact_ok = await connection.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM source_artifacts AS artifact
                    JOIN incoming_attachments AS attachment
                      ON attachment.id=artifact.attachment_id
                    JOIN messages AS message ON message.id=attachment.message_id
                    WHERE artifact.id=$1 AND message.user_id=$2
                )
                """,
                source_artifact_id,
                owner_user_id,
            )
            if artifact_ok is not True:
                raise AccountingConflict("acceptance evidence belongs to another user")
        if delivery_run_id is not None:
            linked = await connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM delivery_run_items di
                   JOIN delivery_runs r ON r.id=di.delivery_run_id
                   WHERE di.delivery_run_id=$1 AND di.request_item_id=$2
                     AND di.case_id=$3 AND r.status NOT IN ('planned','cancelled'))""",
                delivery_run_id, request_item_id, case_id,
            )
            if not linked:
                raise AccountingConflict("delivery does not contain this item or has not departed")
        event_id = uuid4()
        await connection.execute(
            """
            INSERT INTO customer_acceptance_events(
                id,case_id,customer_id,request_item_id,event_type,accepted_quantity,
                source_artifact_id,idempotency_key,recorded_by,delivery_run_id,reason
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            event_id,
            case_id,
            customer_id,
            request_item_id,
            event_type,
            accepted_quantity,
            source_artifact_id,
            idempotency_key,
            owner_user_id,
            delivery_run_id,
            reason,
        )
        net = await _net_customer_acceptance(
            connection, case_id, customer_id, request_item_id
        )
        if net < 0:
            raise AccountingConflict("acceptance revocation exceeds accepted quantity")
        if item["quantity"] is not None and net > Decimal(item["quantity"]):
            raise AccountingConflict("accepted quantity exceeds the requested quantity")
        cancelled = await connection.fetchval(
            "SELECT COALESCE(cancelled_quantity,0) FROM request_item_execution_totals "
            "WHERE request_item_id=$1", request_item_id,
        )
        if (item["quantity"] is not None
                and net + Decimal(cancelled or 0) > Decimal(item["quantity"])):
            raise AccountingConflict("accepted and cancelled exceed the requested quantity")
        return CustomerAcceptanceResult(
            event_id,
            customer_id,
            request_item_id,
            net,
            True,
        )


async def set_private_tender_revenue(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    amount: Decimal,
    currency: str = "KZT",
) -> None:
    """Store private revenue outside every supplier-facing search/dialogue payload."""

    if amount < 0:
        raise ValueError("tender revenue must be nonnegative")
    normalized_currency = _currency(currency)
    result = await connection.execute(
        """
        INSERT INTO case_financials(case_id,tender_revenue_private,currency,updated_by)
        SELECT procurement_case.id,$3,$4,$2
        FROM procurement_cases AS procurement_case
        WHERE procurement_case.id=$1 AND procurement_case.owner_user_id=$2
        ON CONFLICT (case_id) DO UPDATE SET
            tender_revenue_private=EXCLUDED.tender_revenue_private,
            currency=EXCLUDED.currency,updated_by=EXCLUDED.updated_by,updated_at=now()
        """,
        case_id,
        owner_user_id,
        amount,
        normalized_currency,
    )
    if result not in {"INSERT 0 1", "UPDATE 1"}:
        raise AccountingConflict("case is absent or belongs to another owner")


async def load_financial_snapshot(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
) -> FinancialSnapshot:
    row = await connection.fetchrow(
        """
        SELECT financial.tender_revenue_private,
               COALESCE(financial.currency,'KZT') AS currency,
               COALESCE(sum(purchase.purchased_quantity*purchase.unit_cost)
                        FILTER (WHERE purchase.unit_cost IS NOT NULL),0) AS product_cost,
               count(*) FILTER (WHERE purchase.id IS NOT NULL AND purchase.unit_cost IS NULL)
                   AS unknown_lines,
               COALESCE((SELECT sum(run.total_cost) FROM delivery_runs AS run
                         WHERE run.case_id=procurement_case.id),0)
               + COALESCE((SELECT sum(expense.amount) FROM case_expenses AS expense
                           WHERE expense.case_id=procurement_case.id
                             AND expense.cost_kind='actual'
                             AND expense.category='delivery'),0) AS delivery_cost,
               (SELECT count(*) FROM delivery_runs AS run
                WHERE run.case_id=procurement_case.id AND run.status<>'cancelled'
                  AND run.total_cost IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM case_expenses AS expense
                      WHERE expense.delivery_run_id=run.id
                        AND expense.cost_kind='actual'
                        AND expense.category='delivery'
                  )) AS unknown_delivery_runs,
               COALESCE((SELECT sum(expense.amount) FROM case_expenses AS expense
                         WHERE expense.case_id=procurement_case.id
                           AND expense.cost_kind='actual'
                           AND expense.category<>'delivery'),0) AS additional_cost
        FROM procurement_cases AS procurement_case
        LEFT JOIN case_financials AS financial ON financial.case_id=procurement_case.id
        LEFT JOIN request_items AS item ON item.case_id=procurement_case.id
        LEFT JOIN purchase_items AS purchase ON purchase.request_item_id=item.id
        WHERE procurement_case.id=$1 AND procurement_case.owner_user_id=$2
        GROUP BY procurement_case.id,financial.tender_revenue_private,financial.currency
        """,
        case_id,
        owner_user_id,
    )
    if row is None:
        raise AccountingConflict("case is absent or belongs to another owner")
    revenue = row["tender_revenue_private"]
    product = Decimal(row["product_cost"])
    delivery = Decimal(row["delivery_cost"])
    additional = Decimal(row["additional_cost"])
    unknown = int(row["unknown_lines"])
    unknown_delivery_runs = int(row["unknown_delivery_runs"])
    margin = (None if revenue is None or unknown or unknown_delivery_runs
              else Decimal(revenue) - product - delivery - additional)
    return FinancialSnapshot(
        case_id=case_id,
        currency=row["currency"],
        tender_revenue_private=Decimal(revenue) if revenue is not None else None,
        product_cost_known=product,
        delivery_cost_known=delivery,
        unknown_purchase_lines=unknown,
        provisional_margin=margin,
        additional_cost_known=additional,
        unknown_delivery_runs=unknown_delivery_runs,
    )


async def _existing_purchase_result(
    connection: asyncpg.Connection,
    document_id: UUID,
) -> PurchaseRecordResult:
    rows = await connection.fetch(
        """
        SELECT purchase.request_item_id,item.status,item.quantity
        FROM purchase_items AS purchase
        JOIN request_items AS item ON item.id=purchase.request_item_id
        WHERE purchase.purchase_document_id=$1
        ORDER BY item.line_number,item.id
        """,
        document_id,
    )
    return PurchaseRecordResult(
        document_id=document_id,
        recorded_lines=len(rows),
        closed_item_ids=tuple(
            row["request_item_id"] for row in rows if row["status"] == "closed"
        ),
        needs_quantity_review=tuple(
            row["request_item_id"] for row in rows if row["quantity"] is None
        ),
        created=False,
    )


async def _net_customer_acceptance(
    connection: asyncpg.Connection,
    case_id: UUID,
    customer_id: UUID,
    request_item_id: UUID,
) -> Decimal:
    value = await connection.fetchval(
        """
        SELECT COALESCE(sum(CASE WHEN event_type='accepted' THEN accepted_quantity
                                 ELSE -accepted_quantity END),0)
        FROM customer_acceptance_events
        WHERE case_id=$1 AND customer_id=$2 AND request_item_id=$3
        """,
        case_id,
        customer_id,
        request_item_id,
    )
    return Decimal(value)


def _currency(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("currency must be a three-letter ASCII code")
    return normalized
