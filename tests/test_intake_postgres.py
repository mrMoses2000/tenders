"""Native intake persistence checks against an isolated disposable PostgreSQL DB."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from procurement_bot.cases import IntakeContext, persist_intake_result
from procurement_bot.db import run_migrations
from procurement_bot.intake import (
    INTAKE_SPEC_SHA256,
    IntakeResult,
    IntakeService,
    ProcurementRequestPatch,
    SearchScope,
    SourceType,
)
from procurement_bot.providers.agy import FakeExtractor
from scripts.postgres_test_safety import configured_postgres_target, verify_postgres_test_connection

PRICED_TABLE = (
    "[table:1:row:1]\n"
    "Наименование | Значение\n\n"
    "[table:2:row:1]\n"
    "№ п/п | Наименование | Ед. изм. | Кол-во | Цена | Стоимость | Краткая характеристика\n\n"
    "[table:2:row:2]\n"
    "1 | Силикагель | кг | 5,50 | 3 000 | 16500 | Индикаторный"
)


@pytest_asyncio.fixture
async def pg_intake_database():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set TEST_POSTGRES_DSN to an isolated disposable local PostgreSQL database")
    target = configured_postgres_target(dsn)
    if not target.host or target.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip(
            "intake persistence integration tests require an explicit local PostgreSQL target"
        )
    await verify_postgres_test_connection(dsn, target)

    admin = await asyncpg.connect(dsn)
    schema = f"intake_currency_{uuid4().hex}"
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
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header,expected_price",
    [
        ("Цена", None),
        ("Цена, USD", None),
        ("Цена, KZT/USD", None),
        ("Цена, KZT/JPY", None),
        ("Цена, KZT/KGS", None),
        ("Цена, тенге/рублях", None),
        ("Цена, KZT", Decimal("3000")),
    ],
)
async def test_native_price_currency_preserves_searchable_items(
    pg_intake_database,
    header,
    expected_price,
):
    pool = pg_intake_database
    parsed = await IntakeService(FakeExtractor({})).process(
        PRICED_TABLE.replace("Цена | Стоимость", f"{header} | Стоимость"),
        source_type=SourceType.DOCUMENT,
    )
    assert parsed.request.items[0].tender_unit_price_private == expected_price
    assert bool(parsed.request.warnings) is (expected_price is None)

    # The owner has separately supplied sourcing geography; the table's product
    # facts and warning remain those produced by the native deterministic parser.
    request = parsed.request.model_copy(
        update={"city": "Тараз", "search_scope": SearchScope.CITYWIDE}
    )
    patch = ProcurementRequestPatch(
        spec_sha256=INTAKE_SPEC_SHA256,
        context_sha256="0" * 64,
        request=request,
    )
    result = IntakeResult(
        request=request,
        patch=patch,
        ready_to_search=True,
        parser_name=parsed.parser_name,
    )
    owner_id = uuid4()
    message_id = uuid4()
    async with pool.acquire() as connection:
        telegram_id = 750_000_000 + int(owner_id.hex[:8], 16)
        await connection.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
            owner_id,
            telegram_id,
            "Intake currency integration test",
        )
        await connection.execute(
            """INSERT INTO messages(
                   id,user_id,channel,direction,external_chat_id,external_message_id,message_kind
               ) VALUES($1,$2,'telegram','inbound',$3,$4,'document')""",
            message_id,
            owner_id,
            str(telegram_id),
            f"currency-{uuid4()}",
        )
    case_id = await persist_intake_result(
        pool,
        message_id=message_id,
        source_extraction_id=None,
        context=IntakeContext(
            user_id=owner_id,
            chat_id=telegram_id,
            case_id=None,
            version=0,
            request=None,
        ),
        result=result,
    )

    async with pool.acquire() as connection:
        case = await connection.fetchrow(
            "SELECT status,city FROM procurement_cases WHERE id=$1", case_id
        )
        parse_version = await connection.fetchrow(
            "SELECT normalized_payload FROM case_parse_versions WHERE case_id=$1", case_id
        )
        items = await connection.fetch(
            "SELECT name,quantity,unit,specification_text,status FROM request_items "
            "WHERE case_id=$1 ORDER BY line_number",
            case_id,
        )
        financial_count = await connection.fetchval(
            """SELECT count(*) FROM request_item_financials f
               JOIN request_items i ON i.id=f.request_item_id WHERE i.case_id=$1""",
            case_id,
        )
        prepare_job_count = await connection.fetchval(
            "SELECT count(*) FROM jobs WHERE kind='prepare_research' AND payload->>'case_id'=$1",
            str(case_id),
        )
    assert case["status"] == "ready" and case["city"] == "Тараз"
    assert parse_version["normalized_payload"]["warnings"] == request.warnings
    assert len(items) == 1
    assert items[0]["name"] == "Силикагель"
    assert items[0]["quantity"] == 5.5 and items[0]["unit"] == "кг"
    assert items[0]["specification_text"] == "Техническая характеристика Индикаторный"
    assert items[0]["status"] == "ready"
    assert financial_count == int(expected_price is not None)
    if expected_price is not None:
        async with pool.acquire() as connection:
            financial = await connection.fetchrow(
                "SELECT tender_unit_price_private,currency FROM request_item_financials"
            )
        assert financial["tender_unit_price_private"] == expected_price
        assert financial["currency"] == "KZT"
    assert prepare_job_count == 1
