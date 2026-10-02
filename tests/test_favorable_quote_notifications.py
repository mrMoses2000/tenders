from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.offer_technical import record_technical_review
from procurement_bot.queue import stable_key
from procurement_bot.supplier_reply_processing import enqueue_favorable_quote_notification
from procurement_bot.suppliers import (
    OfferInput,
    OfferObservationInput,
    SourceEvidenceInput,
    SupplierInput,
    upsert_offer,
    upsert_offer_observation,
    upsert_source_evidence,
    upsert_supplier,
)


@pytest.mark.asyncio
async def test_favorable_notification_uses_current_verified_supplier_quote_once() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_favorable_quote_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init_connection(connection: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
                format="text",
            )

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=2,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        async with pool.acquire() as connection, connection.transaction():
            owner_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id",
                int(uuid4().int % 2_000_000_000) + 1,
            )
            case_id = await connection.fetchval(
                "INSERT INTO procurement_cases(owner_user_id,status) "
                "VALUES ($1,'evaluating') RETURNING id",
                owner_id,
            )
            item_id = await connection.fetchval(
                """INSERT INTO request_items(
                       case_id,line_number,name,specification_text,quantity,unit
                   ) VALUES ($1,1,'Перчатки','нитриловые, размер M',10,'шт') RETURNING id""",
                case_id,
            )
            await connection.execute(
                "INSERT INTO request_item_financials(request_item_id,tender_unit_price_private) "
                "VALUES ($1,1000)",
                item_id,
            )
            now = datetime.now(UTC)
            identity_evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="manual",
                    source_locator="manual:test-supplier",
                    captured_at=now,
                    content_sha256="a" * 64,
                    raw_payload={},
                    idempotency_key=f"favorable:evidence:identity:{case_id}",
                ),
            )
            supplier = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Поставщик тестовых перчаток",
                    identity_type="source_record",
                    identity_value=f"favorable-{case_id}",
                    source_evidence_id=identity_evidence.id,
                ),
            )
            offer = await upsert_offer(
                connection,
                OfferInput(
                    request_item_id=item_id,
                    supplier_id=supplier.id,
                    idempotency_key=f"favorable:offer:{case_id}",
                    supplier_product_name="Перчатки M",
                ),
            )
            supplier_message = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="supplier_message",
                    source_locator=f"whatsapp:test:{case_id}",
                    captured_at=now,
                    content_sha256="b" * 64,
                    raw_payload={"text": "Цена 900 тенге, есть 20 штук"},
                    idempotency_key=f"favorable:evidence:reply:{case_id}",
                ),
            )
            observation = await upsert_offer_observation(
                connection,
                OfferObservationInput(
                    offer_id=offer.id,
                    source_evidence_id=supplier_message.id,
                    observed_at=now,
                    observed_by="supplier:test",
                    source_type="supplier_message",
                    price_amount=Decimal("900"),
                    currency="KZT",
                    price_unit="шт",
                    available_qty=Decimal("20"),
                    availability_status="in_stock",
                    evidence_level=4,
                    idempotency_key=f"favorable:observation:{case_id}",
                ),
            )
            await record_technical_review(
                connection,
                case_id=case_id,
                offer_id=offer.id,
                requirement_id=None,
                owner_user_id=owner_id,
                verdict="match",
                note="Whole specification reviewed against supplier evidence.",
                source_evidence_id=supplier_message.id,
                idempotency_key=f"favorable:technical-review:{case_id}",
            )

            assert await enqueue_favorable_quote_notification(connection, offer_id=offer.id)
            queued = await connection.fetchrow(
                "SELECT id,event_type,payload,idempotency_key FROM outbox_events "
                "WHERE idempotency_key LIKE 'favorable-supplier-quote:%'"
            )
            assert queued is not None
            assert queued["event_type"] == "telegram.send_message"
            assert queued["payload"]["chat_id"] is not None
            assert "ниже тендерного ориентира на 1000 KZT" in queued["payload"]["text"]
            snapshot_id = await connection.fetchval(
                "SELECT snapshot_id FROM quote_snapshot_current WHERE offer_id=$1", offer.id
            )
            assert queued["idempotency_key"] == stable_key("favorable-supplier-quote", snapshot_id)
            assert not await enqueue_favorable_quote_notification(connection, offer_id=offer.id)
            assert observation.created
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
