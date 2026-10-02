"""Versioned, evidence-backed projections of supplier offer observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

_DECIMAL_FACTS = frozenset(
    {"price_amount", "pack_qty", "moq", "order_multiple", "available_qty"}
)


@dataclass(frozen=True, slots=True)
class QuoteFact:
    value: Decimal | str | date
    source_evidence_id: UUID
    observation_id: UUID
    observed_at: datetime
    source_type: str


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    id: UUID
    offer_id: UUID
    request_item_id: UUID
    version: int
    variant_fingerprint: str
    snapshot_hash: str
    created_at: datetime
    facts: dict[str, QuoteFact]


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _current_variant(
    connection: asyncpg.Connection, offer_id: UUID
) -> tuple[asyncpg.Record, str]:
    offer = await connection.fetchrow(
        """
        SELECT o.id AS offer_id,o.request_item_id,o.supplier_product_name,o.model_sku,
               i.name AS item_name,i.specification_text,i.unit AS item_unit,
               i.quantity AS item_quantity
        FROM offers o JOIN request_items i ON i.id=o.request_item_id
        WHERE o.id=$1
        FOR UPDATE OF o
        """,
        offer_id,
    )
    if offer is None:
        raise ValueError(f"offer does not exist: {offer_id}")
    requirements = await connection.fetch(
        """
        SELECT requirement_key,operator,expected_value,is_hard
        FROM item_requirements WHERE request_item_id=$1
        ORDER BY requirement_key,operator,expected_value::text,is_hard
        """,
        offer["request_item_id"],
    )
    fingerprint = _sha256(
        {
            "request_item": {
                "name": offer["item_name"],
                "specification_text": offer["specification_text"],
                "unit": offer["item_unit"],
                "quantity": offer["item_quantity"],
                "requirements": [dict(row) for row in requirements],
            },
            "supplier_variant": {
                "supplier_product_name": offer["supplier_product_name"],
                "model_sku": offer["model_sku"],
            },
        }
    )
    return offer, fingerprint


def _fact(row: asyncpg.Record, field: str, value: Any | None = None) -> dict[str, Any] | None:
    if value is None:
        value = row[field]
    if value is None:
        return None
    return {
        "value": _json_value(value),
        "source_evidence_id": str(row["source_evidence_id"]),
        "observation_id": str(row["id"]),
        "observed_at": row["observed_at"].isoformat(),
        "source_type": row["source_type"],
    }


async def refresh_quote_snapshot(
    connection: asyncpg.Connection,
    offer_id: UUID,
    observation_id: UUID,
    *,
    bind_observation: bool,
) -> UUID:
    """Refresh an offer projection in the caller's transaction and return its row ID."""
    offer, fingerprint = await _current_variant(connection, offer_id)
    item_id = offer["request_item_id"]

    if bind_observation:
        await connection.execute(
            """
            INSERT INTO offer_observation_variants(
                observation_id,offer_id,request_item_id,variant_fingerprint
            ) VALUES ($1,$2,$3,$4)
            ON CONFLICT (observation_id) DO NOTHING
            """,
            observation_id,
            offer_id,
            item_id,
            fingerprint,
        )

    observations = await connection.fetch(
        """
        SELECT obs.* FROM offer_observations obs
        JOIN offer_observation_variants variant ON variant.observation_id=obs.id
        WHERE obs.offer_id=$1 AND variant.offer_id=$1
          AND variant.request_item_id=$2 AND variant.variant_fingerprint=$3
        ORDER BY obs.observed_at DESC,obs.created_at DESC,obs.id DESC
        """,
        offer_id,
        item_id,
        fingerprint,
    )

    price = next((row for row in observations if row["price_amount"] is not None), None)
    facts: dict[str, dict[str, Any]] = {}
    if price is not None:
        facts["price_amount"] = _fact(price, "price_amount")  # type: ignore[assignment]
        facts["currency"] = _fact(price, "currency")  # type: ignore[assignment]

    terms = [
        row for row in observations
        if row["source_type"] in {"supplier_message", "manual"}
        and (price is None or row["observed_at"] >= price["observed_at"])
    ]
    for name in ("price_unit", "pack_qty", "moq", "order_multiple"):
        row = next((entry for entry in terms if entry[name] is not None), None)
        if row is None and name in {"price_unit", "pack_qty"}:
            row = price if price is not None and price[name] is not None else None
        if row is not None:
            facts[name] = _fact(row, name)  # type: ignore[assignment]

    stock_row = next(
        (
            row for row in observations
            if row["availability_status"] != "unknown"
            or row["available_qty"] is not None
            or row["availability_date"] is not None
        ),
        None,
    )
    if stock_row is not None:
        for name in ("availability_status", "available_qty", "availability_date"):
            fact = _fact(stock_row, name)
            if fact is not None:
                facts[name] = fact

    snapshot = {"facts": facts}
    snapshot_hash = _sha256(
        {"offer_id": str(offer_id), "request_item_id": str(item_id),
         "variant_fingerprint": fingerprint, "snapshot": snapshot}
    )
    existing = await connection.fetchrow(
        "SELECT id FROM quote_snapshot_versions WHERE offer_id=$1 AND snapshot_hash=$2",
        offer_id,
        snapshot_hash,
    )
    if existing is None:
        version = await connection.fetchval(
            "SELECT coalesce(max(version),0)+1 FROM quote_snapshot_versions WHERE offer_id=$1",
            offer_id,
        )
        existing = await connection.fetchrow(
            """
            INSERT INTO quote_snapshot_versions(
                offer_id,request_item_id,version,variant_fingerprint,snapshot_hash,snapshot
            ) VALUES ($1,$2,$3,$4,$5,$6::jsonb)
            ON CONFLICT (offer_id,snapshot_hash) DO NOTHING
            RETURNING id
            """,
            offer_id,
            item_id,
            version,
            fingerprint,
            snapshot_hash,
            snapshot,
        )
        if existing is None:
            existing = await connection.fetchrow(
                "SELECT id FROM quote_snapshot_versions WHERE offer_id=$1 AND snapshot_hash=$2",
                offer_id,
                snapshot_hash,
            )
    if existing is None:
        raise RuntimeError("quote snapshot conflict vanished")
    snapshot_id = existing["id"]
    await connection.execute(
        """
        INSERT INTO quote_snapshot_current(offer_id,request_item_id,snapshot_id)
        VALUES ($1,$2,$3)
        ON CONFLICT (offer_id) DO UPDATE SET
            request_item_id=EXCLUDED.request_item_id,
            snapshot_id=EXCLUDED.snapshot_id,
            updated_at=now()
        """,
        offer_id,
        item_id,
        snapshot_id,
    )
    return snapshot_id


async def get_current_quote_snapshot(
    connection: asyncpg.Connection, offer_id: UUID
) -> QuoteSnapshot | None:
    row = await connection.fetchrow(
        """
        SELECT version_row.* FROM quote_snapshot_current current
        JOIN quote_snapshot_versions version_row
          ON version_row.id=current.snapshot_id
         AND version_row.offer_id=current.offer_id
         AND version_row.request_item_id=current.request_item_id
        WHERE current.offer_id=$1
        """,
        offer_id,
    )
    if row is None:
        return None
    raw_snapshot = row["snapshot"]
    if isinstance(raw_snapshot, str):
        raw_snapshot = json.loads(raw_snapshot)
    facts: dict[str, QuoteFact] = {}
    for name, raw_fact in raw_snapshot.get("facts", {}).items():
        value = raw_fact["value"]
        if name in _DECIMAL_FACTS:
            value = Decimal(value)
        elif name == "availability_date":
            value = date.fromisoformat(value)
        facts[name] = QuoteFact(
            value=value,
            source_evidence_id=UUID(raw_fact["source_evidence_id"]),
            observation_id=UUID(raw_fact["observation_id"]),
            observed_at=datetime.fromisoformat(raw_fact["observed_at"]),
            source_type=raw_fact["source_type"],
        )
    return QuoteSnapshot(
        id=row["id"],
        offer_id=row["offer_id"],
        request_item_id=row["request_item_id"],
        version=row["version"],
        variant_fingerprint=row["variant_fingerprint"].strip(),
        snapshot_hash=row["snapshot_hash"].strip(),
        created_at=row["created_at"],
        facts=facts,
    )
