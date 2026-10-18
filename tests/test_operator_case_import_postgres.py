"""Real PostgreSQL coverage for operator import; requires an isolated TEST_POSTGRES_DSN."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest
from procurement_bot.operator_case_import import (
    CaseImportConflict,
    apply_operator_import,
    canonical_request_hash,
    item_projection_sha256,
    preflight_operator_import,
    source_manifest_sha256,
)
from scripts.postgres_test_safety import configured_postgres_target, verify_postgres_test_connection


@pytest_asyncio.fixture
async def pg_import_case():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set TEST_POSTGRES_DSN to an isolated disposable local PostgreSQL database")
    target = configured_postgres_target(dsn)
    if not target.host or target.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip(
            "operator import integration tests require an explicit local TCP PostgreSQL target"
        )
    await verify_postgres_test_connection(dsn, target)

    admin = await asyncpg.connect(dsn)
    schema = f"operator_import_{uuid4().hex}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:

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
        migration_names = await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        expected_migrations = sorted(
            path.name
            for path in (Path(__file__).parents[1] / "migrations").glob("[0-9][0-9][0-9]_*.sql")
        )
        assert migration_names == expected_migrations
        assert "042_pipeline_reviews.sql" in migration_names
        owner_id = uuid4()
        case_id = uuid4()
        item_id = uuid4()
        async with pool.acquire() as conn:
            telegram_id = 700_000_000 + int(owner_id.hex[:8], 16)
            await conn.execute(
                "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
                owner_id,
                telegram_id,
                "Operator import integration test",
            )
            await conn.execute(
                "INSERT INTO procurement_cases(id,owner_user_id,source_kind,status,title) "
                "VALUES($1,$2,'manual','draft','Do not replace this title')",
                case_id,
                owner_id,
            )
            await conn.execute(
                "INSERT INTO request_items(id,case_id,line_number,name,specification_text,"
                "quantity,unit,status) VALUES($1,$2,1,$3,$4,4,$5,'draft')",
                item_id,
                case_id,
                "Кабель силовой",
                "сечение 4x10 мм²",
                "шт",
            )
        yield pool, case_id, owner_id, item_id
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _request() -> ParsedProcurementRequest:
    return ParsedProcurementRequest.model_validate(
        {
            "source_type": "document",
            "city": "Тараз",
            "search_scope": "citywide",
            "items": [
                {
                    "name": "Кабель силовой",
                    "quantity": "4",
                    "unit": "шт",
                    "tender_unit_price_private": None,
                    "source_reference": "таблица:строка 1",
                    "requirements": [
                        {
                            "name": "Техническая характеристика",
                            "expected_value": "сечение 4x10 мм²",
                            "unit": None,
                            "source_reference": "таблица:строка 1",
                            "ambiguous": False,
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_postgres_import_null_telegram_refs_requirements_and_retry(pg_import_case):
    pool, case_id, owner_id, item_id = pg_import_case
    request = _request()
    artifact = [{"reference": "reviewed-source.docx", "sha256": "a" * 64}]
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": request,
        "expected_request_sha256": canonical_request_hash(request),
        "expected_projection_sha256": "",
        "source_sha256": source_manifest_sha256(artifact),
        "source_artifacts": artifact,
        "source_reference": "reviewed-source.docx#sha256=" + "a" * 64,
    }
    async with pool.acquire() as conn:
        projection_rows = await conn.fetch(
            "SELECT id,line_number,name,specification_text,quantity,unit,status,analogs_allowed "
            "FROM request_items WHERE case_id=$1 ORDER BY line_number",
            case_id,
        )
    kwargs["expected_projection_sha256"] = item_projection_sha256(projection_rows)
    preflight = await preflight_operator_import(
        pool,
        case_id=case_id,
        owner_id=owner_id,
        request=request,
        expected_request_sha256=kwargs["expected_request_sha256"],
        expected_projection_sha256=kwargs["expected_projection_sha256"],
    )
    assert preflight["ready_for_import"] is True
    assert preflight["requirements_to_add"] == 1
    first = await apply_operator_import(pool, **kwargs)
    second = await apply_operator_import(pool, **kwargs)

    assert first["idempotent"] is False and second["idempotent"] is True
    async with pool.acquire() as conn:
        version = await conn.fetchrow("SELECT * FROM case_parse_versions WHERE case_id=$1", case_id)
        case = await conn.fetchrow("SELECT * FROM procurement_cases WHERE id=$1", case_id)
        item = await conn.fetchrow("SELECT * FROM request_items WHERE id=$1", item_id)
        reqs = await conn.fetch("SELECT * FROM item_requirements WHERE request_item_id=$1", item_id)
        event = await conn.fetchrow("SELECT * FROM workflow_events WHERE case_id=$1", case_id)
    assert version["source_message_id"] is None and version["source_extraction_id"] is None
    assert version["status"] == "applied" and version["version"] == 1
    assert case["status"] == "ready" and case["city"] == "Тараз"
    assert case["title"] == "Do not replace this title"
    assert item["id"] == item_id and item["status"] == "ready"
    assert len(reqs) == 1 and reqs[0]["expected_value"] == {
        "value": "сечение 4x10 мм²",
        "unit": None,
    }
    assert event["actor_type"] == "agent" and event["data"]["item_ids"] == [str(item_id)]
    with pytest.raises(CaseImportConflict, match="draft"):
        await preflight_operator_import(
            pool,
            case_id=case_id,
            owner_id=owner_id,
            request=request,
            expected_request_sha256=kwargs["expected_request_sha256"],
            expected_projection_sha256=kwargs["expected_projection_sha256"],
        )


@pytest.mark.asyncio
async def test_postgres_import_rejects_projection_drift_without_partial_writes(pg_import_case):
    pool, case_id, owner_id, _item_id = pg_import_case
    async with pool.acquire() as conn:
        await conn.execute("UPDATE request_items SET name='changed' WHERE case_id=$1", case_id)
        before = await conn.fetchrow("SELECT status FROM procurement_cases WHERE id=$1", case_id)
    request = _request()
    artifact = [{"reference": "reviewed-source.docx", "sha256": "b" * 64}]
    with pytest.raises(CaseImportConflict, match="name differs"):
        await apply_operator_import(
            pool,
            case_id=case_id,
            owner_id=owner_id,
            request=request,
            expected_request_sha256=canonical_request_hash(request),
            expected_projection_sha256="0" * 64,
            source_sha256=source_manifest_sha256(artifact),
            source_artifacts=artifact,
            source_reference="reviewed-source.docx#sha256=" + "b" * 64,
        )
    async with pool.acquire() as conn:
        case = await conn.fetchrow("SELECT status FROM procurement_cases WHERE id=$1", case_id)
        versions = await conn.fetchval(
            "SELECT count(*) FROM case_parse_versions WHERE case_id=$1", case_id
        )
        reqs = await conn.fetchval("SELECT count(*) FROM item_requirements")
    assert case["status"] == before["status"] == "draft"
    assert versions == 0 and reqs == 0


@pytest.mark.asyncio
async def test_postgres_import_rejects_non_equals_requirement_semantics(pg_import_case):
    pool, case_id, owner_id, item_id = pg_import_case
    request = _request()
    artifact = [{"reference": "reviewed-source.docx", "sha256": "c" * 64}]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id,line_number,name,specification_text,quantity,unit,status,analogs_allowed "
            "FROM request_items WHERE case_id=$1 ORDER BY line_number",
            case_id,
        )
        await conn.execute(
            "INSERT INTO item_requirements(request_item_id,requirement_key,operator,expected_value,"
            "is_hard,source_pointer) VALUES($1,'Техническая характеристика','minimum',"
            "$2::jsonb,true,$3)",
            item_id,
            {"value": "сечение 4x10 мм²", "unit": None},
            "таблица:строка 1",
        )

    with pytest.raises(CaseImportConflict, match="requirement facts"):
        await apply_operator_import(
            pool,
            case_id=case_id,
            owner_id=owner_id,
            request=request,
            expected_request_sha256=canonical_request_hash(request),
            expected_projection_sha256=item_projection_sha256(rows),
            source_sha256=source_manifest_sha256(artifact),
            source_artifacts=artifact,
            source_reference="reviewed-source.docx#sha256=" + "c" * 64,
        )
    async with pool.acquire() as conn:
        case = await conn.fetchrow("SELECT status FROM procurement_cases WHERE id=$1", case_id)
        version_count = await conn.fetchval(
            "SELECT count(*) FROM case_parse_versions WHERE case_id=$1", case_id
        )
        requirement_count = await conn.fetchval(
            "SELECT count(*) FROM item_requirements WHERE request_item_id=$1", item_id
        )
    assert case["status"] == "draft"
    assert version_count == 0 and requirement_count == 1


@pytest.mark.asyncio
async def test_postgres_import_rolls_back_all_rows_when_late_event_insert_fails(pg_import_case):
    pool, case_id, owner_id, item_id = pg_import_case
    request = _request()
    artifact = [{"reference": "reviewed-source.docx", "sha256": "e" * 64}]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id,line_number,name,specification_text,quantity,unit,status,analogs_allowed "
            "FROM request_items WHERE case_id=$1 ORDER BY line_number",
            case_id,
        )
        await conn.execute(
            "CREATE FUNCTION reject_operator_import_event() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'operator import event failpoint'; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER reject_operator_import_event BEFORE INSERT ON workflow_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_operator_import_event()"
        )

    with pytest.raises(asyncpg.PostgresError, match="operator import event failpoint"):
        await apply_operator_import(
            pool,
            case_id=case_id,
            owner_id=owner_id,
            request=request,
            expected_request_sha256=canonical_request_hash(request),
            expected_projection_sha256=item_projection_sha256(rows),
            source_sha256=source_manifest_sha256(artifact),
            source_artifacts=artifact,
            source_reference="reviewed-source.docx#sha256=" + "e" * 64,
        )

    async with pool.acquire() as conn:
        case = await conn.fetchrow("SELECT status FROM procurement_cases WHERE id=$1", case_id)
        item = await conn.fetchrow("SELECT status FROM request_items WHERE id=$1", item_id)
        versions = await conn.fetchval(
            "SELECT count(*) FROM case_parse_versions WHERE case_id=$1", case_id
        )
        requirements = await conn.fetchval(
            "SELECT count(*) FROM item_requirements WHERE request_item_id=$1", item_id
        )
        events = await conn.fetchval(
            "SELECT count(*) FROM workflow_events WHERE case_id=$1", case_id
        )
    assert case["status"] == "draft" and item["status"] == "draft"
    assert versions == requirements == events == 0
