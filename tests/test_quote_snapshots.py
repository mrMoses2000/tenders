from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.offer_comparison import compare_offer
from procurement_bot.quote_snapshots import get_current_quote_snapshot
from procurement_bot.report_pipeline import build_case_report
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


def test_quote_snapshot_migration_is_additive_and_separates_current_pointer() -> None:
    sql = (Path(__file__).parents[1] / "migrations/031_quote_snapshots.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS quote_snapshot_versions" in sql
    assert "CREATE TABLE IF NOT EXISTS quote_snapshot_current" in sql
    assert "CREATE TABLE IF NOT EXISTS offer_observation_variants" in sql
    assert "UNIQUE (offer_id, snapshot_hash)" in sql


@pytest.mark.asyncio
async def test_quote_snapshots_track_price_terms_variant_provenance_and_offer_scope(
    tmp_path: Path,
) -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_quote_snapshots_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init_connection(connection: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name, schema="pg_catalog", encoder=json.dumps,
                decoder=json.loads, format="text",
            )

    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2,
        server_settings={"search_path": schema}, init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        async with pool.acquire() as connection:
            async with connection.transaction():
                user_id = await connection.fetchval(
                    "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id",
                    int(uuid4().int % 2_000_000_000) + 1,
                )
                case_id = await connection.fetchval(
                    "INSERT INTO procurement_cases(owner_user_id,status) "
                    "VALUES ($1,'evaluating') RETURNING id",
                    user_id,
                )
                item_id = await connection.fetchval(
                    """INSERT INTO request_items(case_id,line_number,name,specification_text,unit)
                    VALUES ($1,1,'Провод','медный 2.5 мм','м') RETURNING id""",
                    case_id,
                )
                other_item_id = await connection.fetchval(
                    """INSERT INTO request_items(case_id,line_number,name,specification_text,unit)
                    VALUES ($1,2,'Изолента','черная','шт') RETURNING id""",
                    case_id,
                )
                evidence0 = SourceEvidenceInput(
                    source_type="manual", source_locator="manual:test-supplier",
                    captured_at=datetime.now(UTC), content_sha256="a" * 64,
                    raw_payload={}, idempotency_key="quote:evidence:supplier",
                )
                source = await upsert_source_evidence(connection, evidence0)
                supplier = await upsert_supplier(
                    connection,
                    SupplierInput(
                        display_name="Тестовый поставщик", identity_type="source_record",
                        identity_value="quote-snapshot-supplier", source_evidence_id=source.id,
                    ),
                )
                offer = await upsert_offer(
                    connection,
                    OfferInput(
                        request_item_id=item_id, supplier_id=supplier.id,
                        idempotency_key="quote:offer:wire", supplier_product_name="Провод",
                        model_sku="WIRE-25",
                    ),
                )
                other_offer = await upsert_offer(
                    connection,
                    OfferInput(
                        request_item_id=other_item_id, supplier_id=supplier.id,
                        idempotency_key="quote:offer:tape", supplier_product_name="Изолента",
                        model_sku="TAPE-BLK",
                    ),
                )

                now = datetime.now(UTC)

                async def observe(
                    *, key: str, source_type: str, offset: int,
                    price: str | None = None, unit: str | None = None,
                    pack: str | None = None, qty: str | None = None,
                    target_offer=offer,
                ):
                    evidence_value = SourceEvidenceInput(
                        source_type=source_type,
                        source_locator=f"manual:{key}",
                        captured_at=now + timedelta(minutes=offset),
                        content_sha256=(str(offset % 10) * 64),
                        raw_payload={"case": key},
                        idempotency_key=f"quote:evidence:{key}",
                    )
                    evidence = await upsert_source_evidence(connection, evidence_value)
                    observation = OfferObservationInput(
                        offer_id=target_offer.id, source_evidence_id=evidence.id,
                        observed_at=now + timedelta(minutes=offset), observed_by="test",
                        source_type=source_type, idempotency_key=f"quote:observation:{key}",
                        price_amount=Decimal(price) if price is not None else None,
                        currency="KZT" if price is not None else None,
                        price_unit=unit,
                        pack_qty=Decimal(pack) if pack is not None else None,
                        available_qty=Decimal(qty) if qty is not None else None,
                        availability_status="in_stock" if qty is not None else "unknown",
                    )
                    return observation, await upsert_offer_observation(connection, observation)

                first, first_result = await observe(
                    key="wire-first", source_type="marketplace", offset=1,
                    price="100", unit="л", pack="1", qty="40",
                )
                first_snapshot = await get_current_quote_snapshot(connection, offer.id)
                assert first_snapshot is not None
                assert first_snapshot.facts["price_amount"].value == Decimal("100.00")
                assert first_snapshot.facts["price_unit"].value == "л"
                assert first_snapshot.facts["price_unit"].observation_id == first_result.id
                assert first_snapshot.facts["price_unit"].source_type == "marketplace"
                assert (
                    first_snapshot.facts["price_unit"].source_evidence_id
                    == first.source_evidence_id
                )
                assert first_snapshot.facts["pack_qty"].value == Decimal("1")
                assert first_snapshot.facts["pack_qty"].source_type == "marketplace"
                assert first_snapshot.facts["available_qty"].value == Decimal("40")
                advertised = compare_offer(
                    {
                        "offer_id": offer.id,
                        "request_item_id": item_id,
                        "supplier_id": supplier.id,
                        "quantity": Decimal("10"),
                        "unit": "л",
                        "price_amount": first_snapshot.facts["price_amount"].value,
                        "currency": "KZT",
                        "price_unit": first_snapshot.facts["price_unit"].value,
                        "pack_qty": first_snapshot.facts["pack_qty"].value,
                        "available_qty": Decimal("40"),
                        "availability_status": "in_stock",
                        "stock_source_type": "marketplace",
                        "source_type": "marketplace",
                        "technical_state": "verified",
                        "tender_currency": "KZT",
                    }
                )
                assert advertised.price_state == "published"
                assert advertised.rank is None
                assert "price_not_confirmed" in advertised.blockers

                # A new price without quoted terms clears the older advertised unit.
                await observe(
                    key="wire-price-update", source_type="manual", offset=2,
                    price="125",
                )
                price_update = await get_current_quote_snapshot(connection, offer.id)
                assert price_update is not None
                assert price_update.facts["price_amount"].value == Decimal("125.00")
                assert "price_unit" not in price_update.facts
                assert "pack_qty" not in price_update.facts

                # The report query reads the current normalized projection and
                # must not resurrect terms from the pre-price observation.
                report_root = tmp_path / "quote-report"
                initial_report = await build_case_report(
                    connection,
                    results_root=report_root,
                    case_id=case_id,
                )
                initial_html = (report_root / initial_report.storage_path).read_text()
                assert "125.00 KZT" in initial_html
                assert "коробка" not in initial_html

                # A later supplier reply may establish terms for that price.
                later_unit, later_result = await observe(
                    key="wire-unit-later", source_type="supplier_message", offset=3,
                    unit="м", pack="1",
                )
                unit_snapshot = await get_current_quote_snapshot(connection, offer.id)
                assert unit_snapshot is not None
                assert unit_snapshot.facts["price_unit"].value == "м"
                assert unit_snapshot.facts["price_unit"].observation_id == later_result.id
                assert unit_snapshot.facts["pack_qty"].value == Decimal("1")
                current_count = await connection.fetchval(
                    "SELECT count(*) FROM quote_snapshot_versions WHERE offer_id=$1",
                    offer.id,
                )
                retry = await upsert_offer_observation(connection, later_unit)
                repeated_snapshot = await get_current_quote_snapshot(connection, offer.id)
                assert not retry.created and retry.id == later_result.id
                assert repeated_snapshot is not None
                assert repeated_snapshot.id == unit_snapshot.id
                assert await connection.fetchval(
                    "SELECT count(*) FROM quote_snapshot_versions WHERE offer_id=$1",
                    offer.id,
                ) == current_count

                other_obs, _ = await observe(
                    key="tape-price", source_type="supplier_message", offset=4,
                    price="55", unit="рулон", target_offer=other_offer,
                )
                other_snapshot = await get_current_quote_snapshot(connection, other_offer.id)
                assert other_snapshot is not None
                assert other_snapshot.request_item_id == other_item_id
                assert other_snapshot.facts["price_amount"].value == Decimal("55.00")
                assert other_snapshot.facts["price_amount"].observation_id != first_result.id
                assert other_obs.offer_id == other_offer.id

                # Changing the offer SKU changes the fingerprint. Replaying an
                # old observation must not bind it to the new variant.
                await connection.execute(
                    "UPDATE offers SET model_sku='WIRE-25B' WHERE id=$1", offer.id
                )
                await upsert_offer_observation(connection, first)
                changed_variant = await get_current_quote_snapshot(connection, offer.id)
                assert changed_variant is not None
                assert changed_variant.variant_fingerprint != unit_snapshot.variant_fingerprint
                assert changed_variant.facts == {}

                variant_report = await build_case_report(
                    connection,
                    results_root=report_root,
                    case_id=case_id,
                    refresh_key="variant-changed",
                )
                variant_html = (report_root / variant_report.storage_path).read_text()
                assert "Цена не подтверждена" in variant_html
                assert "125.00 KZT" not in variant_html
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
