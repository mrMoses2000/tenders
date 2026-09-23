from __future__ import annotations

from collections.abc import Sequence
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
        if self.purchased_quantity <= 0:
            raise ValueError("purchased_quantity must be positive")
        if self.unit_cost is not None and self.unit_cost < 0:
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


async def record_evidenced_purchase(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    source_artifact_id: UUID,
    document_kind: Literal["receipt", "invoice", "waybill", "cash_receipt", "other"],
    lines: Sequence[PurchaseLine],
    idempotency_key: str,
    document_number: str = "",
    seller_name: str = "",
    issued_at: datetime | None = None,
    stated_total: Decimal | None = None,
    currency: str = "KZT",
) -> PurchaseRecordResult:
    """Record a purchase only when an owner-supplied receipt/waybill artifact exists."""

    if not lines:
        raise ValueError("at least one purchase line is required")
    if len({line.request_item_id for line in lines}) != len(lines):
        raise ValueError("purchase lines must target unique request items")
    normalized_currency = _currency(currency)
    if stated_total is not None and stated_total < 0:
        raise ValueError("stated_total must be nonnegative")

    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"purchase:{idempotency_key}",
        )
        existing = await connection.fetchrow(
            "SELECT id,case_id,source_artifact_id FROM purchase_documents "
            "WHERE idempotency_key=$1",
            idempotency_key,
        )
        if existing is not None:
            if (
                existing["case_id"] != case_id
                or existing["source_artifact_id"] != source_artifact_id
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

        document_id = uuid4()
        await connection.execute(
            """
            INSERT INTO purchase_documents(
                id,case_id,source_artifact_id,document_kind,document_number,
                seller_name,issued_at,stated_total,currency,idempotency_key,created_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            document_id,
            case_id,
            source_artifact_id,
            document_kind,
            document_number.strip(),
            seller_name.strip(),
            issued_at,
            stated_total,
            normalized_currency,
            idempotency_key,
            owner_user_id,
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

        closed: list[UUID] = []
        needs_review: list[UUID] = []
        for item in item_rows:
            if item["quantity"] is None:
                needs_review.append(item["id"])
                continue
            purchased = await connection.fetchval(
                "SELECT COALESCE(sum(purchased_quantity),0) FROM purchase_items "
                "WHERE request_item_id=$1",
                item["id"],
            )
            if Decimal(purchased) >= Decimal(item["quantity"]):
                await connection.execute(
                    "UPDATE request_items SET status='closed',updated_at=now() "
                    "WHERE id=$1 AND status<>'closed'",
                    item["id"],
                )
                closed.append(item["id"])
        return PurchaseRecordResult(
            document_id=document_id,
            recorded_lines=len(lines),
            closed_item_ids=tuple(closed),
            needs_quantity_review=tuple(needs_review),
            created=True,
        )


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
) -> CustomerAcceptanceResult:
    """Append one recipient acceptance fact without rewriting its history."""

    normalized_name = " ".join(customer_name.split())
    if not normalized_name:
        raise ValueError("customer_name must be non-empty")
    if accepted_quantity <= 0:
        raise ValueError("accepted_quantity must be positive")
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
        event_id = uuid4()
        await connection.execute(
            """
            INSERT INTO customer_acceptance_events(
                id,case_id,customer_id,request_item_id,event_type,accepted_quantity,
                source_artifact_id,idempotency_key,recorded_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
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
        )
        net = await _net_customer_acceptance(
            connection, case_id, customer_id, request_item_id
        )
        if net < 0:
            raise AccountingConflict("acceptance revocation exceeds accepted quantity")
        if item["quantity"] is not None and net > Decimal(item["quantity"]):
            raise AccountingConflict("accepted quantity exceeds the requested quantity")
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
                         WHERE run.case_id=procurement_case.id),0) AS delivery_cost
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
    unknown = int(row["unknown_lines"])
    margin = None if revenue is None or unknown else Decimal(revenue) - product - delivery
    return FinancialSnapshot(
        case_id=case_id,
        currency=row["currency"],
        tender_revenue_private=Decimal(revenue) if revenue is not None else None,
        product_cost_known=product,
        delivery_cost_known=delivery,
        unknown_purchase_lines=unknown,
        provisional_margin=margin,
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
