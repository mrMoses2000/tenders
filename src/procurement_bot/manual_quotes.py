"""Owner-entered supplier quotes for an existing offer variant."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from procurement_bot.suppliers import (
    OfferObservationInput,
    PersistenceConflictError,
    SourceEvidenceInput,
    upsert_offer_observation,
    upsert_source_evidence,
)

AvailabilityStatus = Literal[
    "unknown", "in_stock_unquantified", "in_stock", "preorder", "out_of_stock"
]
DeliveryStatus = Literal["unknown", "pickup", "delivery", "both", "unavailable"]
VatStatus = Literal["unknown", "included", "excluded", "not_applicable"]
DocumentStatus = Literal["unknown", "available", "partial", "unavailable"]


class ManualQuoteInput(BaseModel):
    """Validated facts entered by an owner for the currently selected variant."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: UUID
    variant_confirmed: Literal[True]
    price_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="KZT", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    price_unit: str = Field(min_length=1, max_length=50)
    pack_qty: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    moq: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    order_multiple: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    available_qty: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)
    availability_status: AvailabilityStatus = "unknown"
    observed_at: datetime
    source_text: str = Field(min_length=3, max_length=4000)
    source_url: str | None = Field(default=None, max_length=2000)
    delivery_status: DeliveryStatus = "unknown"
    vat_status: VatStatus = "unknown"
    document_status: DocumentStatus = "unknown"

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("observed_at cannot be more than five minutes in the future")
        return value

    @field_validator("variant_confirmed", mode="before")
    @classmethod
    def _require_boolean_confirmation(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("variant_confirmed must be the boolean true")
        return value

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def _validate_stock_quantity(self) -> ManualQuoteInput:
        if self.availability_status == "in_stock" and self.available_qty is None:
            raise ValueError("available_qty is required when availability_status is in_stock")
        return self


@dataclass(frozen=True, slots=True)
class ManualQuoteResult:
    observation_id: UUID
    source_evidence_id: UUID
    quote_snapshot_id: UUID
    created: bool


class ManualQuoteConflict(RuntimeError):
    """A manual-quote idempotency key was reused for different facts."""


class ManualQuoteNotFound(LookupError):
    """The caller cannot access the requested case/offer tuple."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _idempotency_scope(case_id: UUID, offer_id: UUID, owner_user_id: UUID, nonce: UUID) -> str:
    return f"manual-quote:{case_id}:{offer_id}:{owner_user_id}:{nonce}"


async def record_manual_quote(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    offer_id: UUID,
    owner_user_id: UUID,
    quote: ManualQuoteInput,
) -> ManualQuoteResult:
    """Persist one owner-entered quote atomically without changing the offer.

    Existing evidence, observation, and snapshot contracts are reused. The
    workflow event is the idempotency receipt and records only IDs plus a hash
    of the validated input; the source text remains in evidence storage.
    """

    payload = quote.model_dump(mode="json")
    payload_hash = _payload_hash(payload)
    scope_key = _idempotency_scope(case_id, offer_id, owner_user_id, quote.idempotency_key)
    captured_at = datetime.now(UTC)

    async with connection.transaction():
        target = await connection.fetchrow(
            """
            SELECT c.id AS case_id,c.owner_user_id,c.status,
                   o.id AS offer_id,o.request_item_id
            FROM procurement_cases c
            JOIN request_items i ON i.case_id=c.id
            JOIN offers o ON o.request_item_id=i.id
            WHERE c.id=$1 AND o.id=$2
            FOR UPDATE OF c,o
            """,
            case_id,
            offer_id,
        )
        if (
            target is None
            or target["owner_user_id"] != owner_user_id
        ):
            raise ManualQuoteNotFound("case or offer is unavailable")

        prior = await connection.fetchrow(
            """
            SELECT data FROM workflow_events
            WHERE idempotency_key=$1 AND case_id=$2 AND event_type='manual_quote_recorded'
            """,
            scope_key,
            case_id,
        )
        if prior is not None:
            data = prior["data"]
            if isinstance(data, str):
                data = json.loads(data)
            if data.get("payload_hash") != payload_hash:
                raise ManualQuoteConflict("idempotency key was used for different quote facts")
            return ManualQuoteResult(
                observation_id=UUID(data["observation_id"]),
                source_evidence_id=UUID(data["source_evidence_id"]),
                quote_snapshot_id=UUID(data["quote_snapshot_id"]),
                created=False,
            )
        if target["status"] in {"closed", "cancelled"}:
            raise ManualQuoteNotFound("case or offer is unavailable")

        try:
            evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="manual",
                    source_locator=f"manual_quote:{offer_id}:{quote.idempotency_key}",
                    source_url=quote.source_url,
                    captured_at=captured_at,
                    content_sha256=hashlib.sha256(quote.source_text.encode("utf-8")).hexdigest(),
                    raw_payload={
                        "source_text": quote.source_text,
                        "source_url": quote.source_url,
                        "quote_payload_sha256": payload_hash,
                    },
                    idempotency_key=f"{scope_key}:evidence",
                ),
            )
            observation = await upsert_offer_observation(
                connection,
                OfferObservationInput(
                    offer_id=offer_id,
                    source_evidence_id=evidence.id,
                    observed_at=quote.observed_at,
                    observed_by=str(owner_user_id),
                    source_type="manual",
                    idempotency_key=f"{scope_key}:observation",
                    price_amount=quote.price_amount,
                    currency=quote.currency,
                    price_unit=quote.price_unit,
                    pack_qty=quote.pack_qty,
                    moq=quote.moq,
                    order_multiple=quote.order_multiple,
                    available_qty=quote.available_qty,
                    availability_status=quote.availability_status,
                    vat_status=quote.vat_status,
                    document_status=quote.document_status,
                    delivery_status=quote.delivery_status,
                    raw_payload={"source_text_sha256": hashlib.sha256(
                        quote.source_text.encode("utf-8")
                    ).hexdigest()},
                ),
            )
            snapshot_id = await connection.fetchval(
                "SELECT snapshot_id FROM quote_snapshot_current WHERE offer_id=$1",
                offer_id,
            )
            if snapshot_id is None:
                raise RuntimeError("manual quote snapshot was not refreshed")
        except PersistenceConflictError as exc:
            raise ManualQuoteConflict(
                "idempotency key was used for different quote facts"
            ) from exc

        event = await connection.fetchrow(
            """
            INSERT INTO workflow_events(
                case_id,request_item_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES($1,$2,'manual_quote_recorded','user',$3,$4,$5::jsonb)
            ON CONFLICT(idempotency_key) DO NOTHING
            RETURNING id
            """,
            case_id,
            target["request_item_id"],
            str(owner_user_id),
            scope_key,
            {
                "offer_id": str(offer_id),
                "observation_id": str(observation.id),
                "source_evidence_id": str(evidence.id),
                "quote_snapshot_id": str(snapshot_id),
                "payload_hash": payload_hash,
            },
        )
        if event is None:
            existing = await connection.fetchrow(
                "SELECT data FROM workflow_events WHERE idempotency_key=$1", scope_key
            )
            data = existing["data"] if existing is not None else {}
            if isinstance(data, str):
                data = json.loads(data)
            if data.get("payload_hash") != payload_hash:
                raise ManualQuoteConflict("idempotency key was used for different quote facts")
            return ManualQuoteResult(
                observation_id=UUID(data["observation_id"]),
                source_evidence_id=UUID(data["source_evidence_id"]),
                quote_snapshot_id=UUID(data["quote_snapshot_id"]),
                created=False,
            )
        return ManualQuoteResult(
            observation_id=observation.id,
            source_evidence_id=evidence.id,
            quote_snapshot_id=snapshot_id,
            created=True,
        )
