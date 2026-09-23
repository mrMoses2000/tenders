from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.suppliers import (
    OfferInput,
    OfferObservationInput,
    PersistenceConflictError,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierIdentityConflictError,
    SupplierInput,
    SupplierLocationInput,
    location_key,
    normalize_contact,
    normalize_supplier_identity,
    upsert_offer,
    upsert_offer_observation,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
    upsert_supplier_location,
)


def test_normalization_is_deterministic_for_kazakhstan_supplier_identities() -> None:
    assert normalize_contact("phone", "8 (777) 123-45-67") == "+77771234567"
    assert normalize_contact("whatsapp", "+7 777 123 45 67") == "+77771234567"
    assert normalize_supplier_identity("domain", "https://WWW.Example.KZ/shop") == "example.kz"
    assert normalize_supplier_identity("instagram", "https://instagram.com/Med_Snab/") == "med_snab"
    assert normalize_supplier_identity("bin_iin", "123 456 789 012") == "123456789012"


def test_location_key_uses_all_identity_fields_but_not_evidence() -> None:
    first = SupplierLocationInput(
        source_evidence_id=UUID(int=1),
        city=" Алматы ",
        market_name="Базар Алматы-1",
        pavilion="12",
    )
    same_place_new_evidence = SupplierLocationInput(
        source_evidence_id=UUID(int=2),
        city="алматы",
        market_name=" базар   Алматы-1 ",
        pavilion="12",
    )
    another_pavilion = SupplierLocationInput(
        source_evidence_id=UUID(int=1),
        city="Алматы",
        market_name="Базар Алматы-1",
        pavilion="13",
    )

    assert location_key(first) == location_key(same_place_new_evidence)
    assert location_key(first) != location_key(another_pavilion)


def test_observation_requires_currency_and_exact_stock_quantity() -> None:
    base = {
        "offer_id": UUID(int=1),
        "source_evidence_id": UUID(int=2),
        "observed_at": datetime.now(UTC),
        "observed_by": "research-worker",
        "source_type": "web",
        "idempotency_key": "observation:1",
    }
    with pytest.raises(ValueError, match="currency"):
        OfferObservationInput(**base, price_amount=Decimal("100"))
    with pytest.raises(ValueError, match="available_qty"):
        OfferObservationInput(**base, availability_status="in_stock")


class _ConflictConnection:
    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        if "INSERT INTO source_evidence" in sql:
            return None
        return {
            "id": UUID(int=9),
            "research_query_id": None,
            "source_type": "web",
            "source_locator": "https://example.kz/a",
            "source_url": "https://example.kz/a",
            "external_id": "",
            "title": "",
            "captured_at": datetime(2026, 9, 22, tzinfo=UTC),
            "content_sha256": "b" * 64,
            "snapshot_storage_path": "",
            "raw_payload": {"name": "changed"},
        }


@pytest.mark.asyncio
async def test_idempotency_key_cannot_hide_changed_evidence() -> None:
    evidence = SourceEvidenceInput(
        source_type="web",
        source_locator="https://example.kz/a",
        source_url="https://example.kz/a",
        captured_at=datetime(2026, 9, 22, tzinfo=UTC),
        content_sha256="a" * 64,
        raw_payload={"name": "original"},
        idempotency_key="web:capture:1",
    )

    with pytest.raises(PersistenceConflictError, match="content_sha256"):
        await upsert_source_evidence(_ConflictConnection(), evidence)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_supplier_repository_roundtrip_and_conflict() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_suppliers_{uuid4().hex}"
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
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES (9001) RETURNING id"
            )
            case_id = await connection.fetchval(
                "INSERT INTO procurement_cases(owner_user_id) VALUES ($1) RETURNING id",
                user_id,
            )
            item_id = await connection.fetchval(
                """
                INSERT INTO request_items(case_id,line_number,name)
                VALUES ($1,1,'Марля') RETURNING id
                """,
                case_id,
            )
            evidence_value = SourceEvidenceInput(
                source_type="2gis",
                source_locator="2gis:firm:42",
                source_url="https://2gis.kz/almaty/firm/42",
                external_id="42",
                captured_at=datetime(2026, 9, 22, tzinfo=UTC),
                content_sha256="a" * 64,
                raw_payload={"name": "Медснаб"},
                idempotency_key="evidence:2gis:42:v1",
            )
            evidence = await upsert_source_evidence(connection, evidence_value)
            evidence_retry = await upsert_source_evidence(connection, evidence_value)
            assert evidence.created and not evidence_retry.created
            assert evidence.id == evidence_retry.id

            supplier_value = SupplierInput(
                display_name="Медснаб",
                identity_type="2gis_firm_id",
                identity_value="42",
                source_evidence_id=evidence.id,
            )
            supplier = await upsert_supplier(connection, supplier_value)
            supplier_retry = await upsert_supplier(connection, supplier_value)
            assert supplier.created and not supplier_retry.created
            assert supplier.id == supplier_retry.id

            contact_value = SupplierContactInput(
                contact_type="whatsapp",
                display_value="8 777 123 45 67",
                is_whatsapp_confirmed=True,
                source_evidence_id=evidence.id,
            )
            contact = await upsert_supplier_contact(connection, supplier.id, contact_value)
            contact_retry = await upsert_supplier_contact(connection, supplier.id, contact_value)
            assert contact.created and not contact_retry.created

            another_supplier = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Другой",
                    identity_type="2gis_firm_id",
                    identity_value="43",
                    source_evidence_id=evidence.id,
                ),
            )
            with pytest.raises(SupplierIdentityConflictError):
                await upsert_supplier_contact(connection, another_supplier.id, contact_value)

            location_value = SupplierLocationInput(
                source_evidence_id=evidence.id,
                city="Алматы",
                address_text="ул. Сейфуллина, 1",
                market_name="Алматы-1",
                pavilion="12",
                source_provider="2gis",
                provider_place_id="place-42",
            )
            location = await upsert_supplier_location(
                connection, supplier.id, location_value
            )
            location_retry = await upsert_supplier_location(
                connection, supplier.id, location_value
            )
            assert location.created and not location_retry.created

            offer_value = OfferInput(
                request_item_id=item_id,
                supplier_id=supplier.id,
                supplier_location_id=location.id,
                supplier_product_name="Марля медицинская",
                idempotency_key="offer:item1:supplier42",
            )
            offer = await upsert_offer(connection, offer_value)
            offer_retry = await upsert_offer(connection, offer_value)
            assert offer.created and not offer_retry.created

            observation_value = OfferObservationInput(
                offer_id=offer.id,
                source_evidence_id=evidence.id,
                observed_at=datetime(2026, 9, 22, tzinfo=UTC),
                observed_by="research-worker",
                source_type="2gis",
                availability_status="unknown",
                raw_payload={"listing": "supplier only"},
                idempotency_key="observation:offer1:v1",
            )
            observation = await upsert_offer_observation(connection, observation_value)
            observation_retry = await upsert_offer_observation(connection, observation_value)
            assert observation.created and not observation_retry.created
            assert observation.id == observation_retry.id
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
