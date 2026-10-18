from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from procurement_bot.db import run_migrations
from procurement_bot.manual_quotes import (
    ManualQuoteConflict,
    ManualQuoteInput,
    ManualQuoteNotFound,
    record_manual_quote,
)
from procurement_bot.offer_comparison import compare_offer, rank_goods_offers
from procurement_bot.quote_snapshots import get_current_quote_snapshot
from procurement_bot.suppliers import (
    OfferInput,
    SourceEvidenceInput,
    SupplierInput,
    upsert_offer,
    upsert_source_evidence,
    upsert_supplier,
)


def quote_input(*, nonce: UUID | None = None, **changes: object) -> ManualQuoteInput:
    values: dict[str, object] = {
        "idempotency_key": nonce or uuid4(),
        "variant_confirmed": True,
        "price_amount": Decimal("1250.50"),
        "currency": "KZT",
        "price_unit": "1 pcs",
        "available_qty": Decimal("4"),
        "availability_status": "in_stock",
        "observed_at": datetime.now(UTC),
        "source_text": "Supplier confirmed the price of 1250.50 KZT per piece.",
        "source_url": "https://supplier.example/quote/1",
        "delivery_status": "delivery",
        "vat_status": "included",
        "document_status": "available",
    }
    values.update(changes)
    return ManualQuoteInput.model_validate(values)


@pytest.fixture
async def manual_quotes_db():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_manual_quote_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init(connection: asyncpg.Connection) -> None:
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
        max_size=3,
        server_settings={"search_path": schema},
        init=init,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner_id, case_id = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,'manual quote test')",
            owner_id,
            int(uuid4().int % 2_000_000_000) + 1,
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES($1,$2,'Manual quote test','Almaty','researching')",
            case_id,
            owner_id,
        )
        item_id = await pool.fetchval(
            "INSERT INTO request_items(case_id,line_number,name,quantity,unit,status) "
            "VALUES($1,1,'Gauze',10,'pcs','ready') RETURNING id",
            case_id,
        )
        yield pool, case_id, owner_id, item_id
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def test_manual_quote_input_enforces_operator_fact_boundaries() -> None:
    with pytest.raises(ValidationError):
        quote_input(variant_confirmed=False)
    with pytest.raises(ValidationError):
        quote_input(variant_confirmed=1)
    with pytest.raises(ValidationError):
        quote_input(variant_confirmed="true")
    with pytest.raises(ValidationError):
        quote_input(availability_status="in_stock", available_qty=None)
    with pytest.raises(ValidationError):
        quote_input(source_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        quote_input(observed_at=datetime.now(UTC) + timedelta(minutes=6))
    with pytest.raises(ValidationError):
        quote_input(unexpected_field="must be rejected")


def test_manual_quote_is_operator_recorded_and_needs_technical_verification_to_rank() -> None:
    row = {
        "offer_id": "offer-1",
        "request_item_id": "item-1",
        "supplier_id": "supplier-1",
        "quantity": Decimal("10"),
        "unit": "шт",
        "price_amount": Decimal("1250.50"),
        "currency": "KZT",
        "price_unit": "шт",
        "source_type": "manual",
        "available_qty": Decimal("12"),
        "stock_source_type": "manual",
        "availability_status": "in_stock",
        "technical_state": "needs_confirmation",
    }
    comparison = compare_offer(row)

    assert comparison.price_state == "operator_recorded"
    assert comparison.technical_state == "needs_confirmation"
    assert "technical_not_verified" in comparison.blockers
    assert not comparison.rankable
    assert rank_goods_offers([row])[0].rank is None


@pytest.mark.asyncio
async def test_manual_quote_is_owned_append_only_and_idempotent(
    manual_quotes_db,
) -> None:
    pool, case_id, owner_id, item_id = manual_quotes_db
    async with pool.acquire() as connection:
        identity_source = await upsert_source_evidence(
            connection,
            SourceEvidenceInput(
                source_type="manual",
                source_locator=f"manual_quote_test_supplier:{uuid4()}",
                captured_at=datetime.now(UTC),
                content_sha256="a" * 64,
                raw_payload={"test": "supplier identity"},
                idempotency_key=f"manual_quote_test_supplier:{uuid4()}",
            ),
        )
        supplier = await upsert_supplier(
            connection,
            SupplierInput(
                display_name="Manual quote test supplier",
                identity_type="source_record",
                identity_value=f"manual-quote-test:{uuid4()}",
                source_evidence_id=identity_source.id,
            ),
        )
        offer = await upsert_offer(
            connection,
            OfferInput(
                request_item_id=item_id,
                supplier_id=supplier.id,
                idempotency_key=f"manual-quote-test-offer:{uuid4()}",
                supplier_product_name="Gauze medical",
                model_sku="M-10",
            ),
        )

        nonce = uuid4()
        quote_received_at = datetime.now(UTC) - timedelta(days=1)
        quote = quote_input(nonce=nonce, observed_at=quote_received_at)
        operation_started_at = datetime.now(UTC)
        created = await record_manual_quote(
            connection,
            case_id=case_id,
            offer_id=offer.id,
            owner_user_id=owner_id,
            quote=quote,
        )
        retry = await record_manual_quote(
            connection,
            case_id=case_id,
            offer_id=offer.id,
            owner_user_id=owner_id,
            quote=quote,
        )
        assert created.created is True
        assert retry.created is False
        assert retry == type(created)(
            observation_id=created.observation_id,
            source_evidence_id=created.source_evidence_id,
            quote_snapshot_id=created.quote_snapshot_id,
            created=False,
        )

        counts_before_variant_change = await connection.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM source_evidence WHERE idempotency_key LIKE $1) AS evidence,
              (SELECT count(*) FROM offer_observations WHERE offer_id=$2) AS observations,
              (SELECT count(*) FROM workflow_events
               WHERE case_id=$3 AND event_type='manual_quote_recorded') AS events,
              (SELECT count(*) FROM research_runs WHERE case_id=$3) AS research_runs,
              (SELECT count(*) FROM jobs) AS jobs,
              (SELECT count(*) FROM outbox_events) AS outbox,
              (SELECT count(*) FROM purchase_items WHERE case_id=$3) AS purchases,
              (SELECT count(*) FROM customer_acceptance_events WHERE case_id=$3) AS acceptance,
              (SELECT count(*) FROM whatsapp_send_acceptance_journal) AS wa_acceptance
            """,
            f"manual-quote:{case_id}:{offer.id}:{owner_id}:{nonce}%",
            offer.id,
            case_id,
        )
        assert counts_before_variant_change["evidence"] == 1
        assert counts_before_variant_change["observations"] == 1
        assert counts_before_variant_change["events"] == 1
        assert counts_before_variant_change["research_runs"] == 0
        assert counts_before_variant_change["jobs"] == 0
        assert counts_before_variant_change["outbox"] == 0
        assert counts_before_variant_change["purchases"] == 0
        assert counts_before_variant_change["acceptance"] == 0
        assert counts_before_variant_change["wa_acceptance"] == 0
        audit_event = await connection.fetchrow(
            """SELECT actor_type,actor_id,data FROM workflow_events
               WHERE case_id=$1 AND event_type='manual_quote_recorded'""",
            case_id,
        )
        assert audit_event["actor_type"] == "user"
        assert audit_event["actor_id"] == str(owner_id)
        assert audit_event["data"]["payload_hash"]
        assert "source_text" not in audit_event["data"]
        persisted_observation = await connection.fetchrow(
            "SELECT source_type,observed_by,observed_at,price_amount,price_unit "
            "FROM offer_observations WHERE id=$1",
            created.observation_id,
        )
        assert persisted_observation["source_type"] == "manual"
        assert persisted_observation["observed_by"] == str(owner_id)
        assert persisted_observation["price_amount"] == Decimal("1250.50")
        assert persisted_observation["price_unit"] == "1 pcs"
        assert persisted_observation["observed_at"] == quote_received_at
        source_row = await connection.fetchrow(
            "SELECT captured_at FROM source_evidence WHERE id=$1",
            created.source_evidence_id,
        )
        assert source_row["captured_at"] >= operation_started_at

        snapshot = await get_current_quote_snapshot(connection, offer.id)
        assert snapshot is not None
        assert snapshot.id == created.quote_snapshot_id
        assert snapshot.facts["price_amount"].value == Decimal("1250.50")
        assert snapshot.facts["price_amount"].source_type == "manual"
        assert snapshot.facts["price_amount"].source_evidence_id == created.source_evidence_id
        manual_comparison = compare_offer(
            {
                "offer_id": str(offer.id),
                "request_item_id": str(item_id),
                "supplier_id": str(supplier.id),
                "quantity": Decimal("10"),
                "unit": "шт",
                "price_amount": Decimal("1250.50"),
                "currency": "KZT",
                "price_unit": "шт",
                "source_type": "manual",
                "available_qty": Decimal("4"),
                "stock_source_type": "manual",
                "availability_status": "in_stock",
                "technical_state": "needs_confirmation",
            }
        )
        assert manual_comparison.price_state == "operator_recorded"
        assert manual_comparison.technical_state == "needs_confirmation"
        assert "technical_not_verified" in manual_comparison.blockers
        assert not manual_comparison.rankable
        assert rank_goods_offers(
            [
                {
                    "offer_id": str(offer.id),
                    "request_item_id": str(item_id),
                    "supplier_id": str(supplier.id),
                    "quantity": Decimal("10"),
                    "unit": "шт",
                    "price_amount": Decimal("1250.50"),
                    "currency": "KZT",
                    "price_unit": "шт",
                    "source_type": "manual",
                    "available_qty": Decimal("4"),
                    "stock_source_type": "manual",
                    "availability_status": "in_stock",
                    "technical_state": "needs_confirmation",
                }
            ]
        )[0].rank is None
        prior_binding = await connection.fetchrow(
            "SELECT variant_fingerprint FROM offer_observation_variants WHERE observation_id=$1",
            created.observation_id,
        )

        with pytest.raises(ManualQuoteConflict):
            await record_manual_quote(
                connection,
                case_id=case_id,
                offer_id=offer.id,
                owner_user_id=owner_id,
                quote=quote_input(nonce=nonce, source_text="Исправленное содержание ответа."),
            )

        with pytest.raises(ManualQuoteNotFound):
            await record_manual_quote(
                connection,
                case_id=case_id,
                offer_id=offer.id,
                owner_user_id=uuid4(),
                quote=quote_input(),
            )
        with pytest.raises(ManualQuoteNotFound):
            await record_manual_quote(
                connection,
                case_id=uuid4(),
                offer_id=offer.id,
                owner_user_id=owner_id,
                quote=quote_input(),
            )

        await connection.execute(
            "UPDATE offers SET supplier_product_name='Gauze updated',"
            "model_sku='M-11' WHERE id=$1",
            offer.id,
        )
        retry_after_variant_change = await record_manual_quote(
            connection,
            case_id=case_id,
            offer_id=offer.id,
            owner_user_id=owner_id,
            quote=quote,
        )
        current_binding = await connection.fetchrow(
            "SELECT variant_fingerprint FROM offer_observation_variants WHERE observation_id=$1",
            created.observation_id,
        )
        assert retry_after_variant_change.quote_snapshot_id == created.quote_snapshot_id
        assert current_binding["variant_fingerprint"] == prior_binding["variant_fingerprint"]
        assert await connection.fetchval(
            "SELECT supplier_product_name FROM offers WHERE id=$1", offer.id
        ) == "Gauze updated"

        await connection.execute(
            "UPDATE procurement_cases SET status='closed' WHERE id=$1", case_id
        )
        closed_retry = await record_manual_quote(
            connection,
            case_id=case_id,
            offer_id=offer.id,
            owner_user_id=owner_id,
            quote=quote,
        )
        assert closed_retry.created is False
        assert closed_retry.observation_id == created.observation_id
        with pytest.raises(ManualQuoteNotFound):
            await record_manual_quote(
                connection,
                case_id=case_id,
                offer_id=offer.id,
                owner_user_id=owner_id,
                quote=quote_input(),
            )
