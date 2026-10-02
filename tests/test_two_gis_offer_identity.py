from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import BROWSER_SPEC_SHA256, SupplierBrowserResult
from procurement_bot.research import (
    EvidenceClaim,
    EvidenceType,
    ResearchSource,
    SupplierCandidate,
    SupplierEvidence,
    build_research_plan,
)
from procurement_bot.research_execution import ResearchRunExecutor, _QueryBranch, _request_hash


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_two_gis_offer_is_shared_between_queries_but_scoped_per_item() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_two_gis_offer_identity_{uuid4().hex}"
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
        user_id = await pool.fetchval(
            "INSERT INTO users(telegram_id) VALUES($1) RETURNING id",
            1_100_000_000 + int(uuid4().hex[:8], 16),
        )
        case_id, run_id = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,status) VALUES($1,$2,'researching')",
            case_id,
            user_id,
        )
        await pool.execute(
            "INSERT INTO research_runs(id,case_id,status,idempotency_key) "
            "VALUES($1,$2,'running',$3)",
            run_id,
            case_id,
            f"two-gis-offer-run:{run_id}",
        )
        items = [
            ProcurementItem(name="Марля", quantity=10, unit="рулон"),
            ProcurementItem(name="Бинт", quantity=5, unit="шт"),
        ]
        item_ids = [uuid4(), uuid4()]
        await pool.executemany(
            "INSERT INTO request_items(id,case_id,line_number,name,quantity,unit) "
            "VALUES($1,$2,$3,$4,$5,$6)",
            [
                (item_ids[0], case_id, 1, items[0].name, items[0].quantity, items[0].unit),
                (item_ids[1], case_id, 2, items[1].name, items[1].quantity, items[1].unit),
            ],
        )
        plan = build_research_plan(
            ParsedProcurementRequest(city="Алматы", search_scope=SearchScope.CITYWIDE, items=items)
        )
        two_gis_by_item = {
            branch.item_index: branch
            for branch in plan.source_plans
            if branch.source == ResearchSource.TWO_GIS
        }
        query_ids = [uuid4(), uuid4(), uuid4()]
        await pool.executemany(
            """INSERT INTO research_queries(
                   id,research_run_id,provider,query_text,city,idempotency_key
               ) VALUES($1,$2,'2gis_web',$3,'Алматы',$4)""",
            [
                (query_ids[0], run_id, "Строительные магазины Алматы", f"q:{query_ids[0]}"),
                (query_ids[1], run_id, "Поставщики медтоваров Алматы", f"q:{query_ids[1]}"),
                (query_ids[2], run_id, "Аптеки Алматы", f"q:{query_ids[2]}"),
            ],
        )

        branches = [
            _QueryBranch(query_ids[0], two_gis_by_item[0], item_ids[0]),
            _QueryBranch(query_ids[1], two_gis_by_item[0], item_ids[0]),
            _QueryBranch(query_ids[2], two_gis_by_item[1], item_ids[1]),
        ]
        executor = ResearchRunExecutor(pool, object())  # type: ignore[arg-type]

        async def persist(branch: _QueryBranch, firm_id: str) -> None:
            evidence = SupplierEvidence(
                evidence_type=EvidenceType.BUSINESS_LISTING,
                source=ResearchSource.TWO_GIS,
                source_url=f"https://2gis.kz/almaty/firm/{firm_id}",
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256=(firm_id[-1] if firm_id[-1].isalnum() else "a") * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.ADDRESS],
            )
            candidate = SupplierCandidate(
                source=ResearchSource.TWO_GIS,
                external_id=firm_id,
                name="Медицинский склад",
                address="Алматы, ул. Абая, 10",
                evidence=[evidence],
            )
            result = SupplierBrowserResult(
                spec_sha256=BROWSER_SPEC_SHA256,
                request_sha256=_request_hash(branch.plan),
                candidates=[candidate],
                exhausted=True,
                pages_examined=1,
            )
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await executor._persist_branch(connection, branch, result)

        firm_id = "70000001077212301"
        await asyncio.wait_for(
            asyncio.gather(
                persist(branches[0], firm_id),
                persist(branches[1], firm_id),
            ),
            timeout=15,
        )
        await persist(branches[2], firm_id)

        offers = await pool.fetch(
            "SELECT id,request_item_id,supplier_id FROM offers "
            "WHERE request_item_id=ANY($1::uuid[]) ORDER BY request_item_id,id",
            item_ids,
        )
        assert len(offers) == 2
        assert offers[0]["request_item_id"] != offers[1]["request_item_id"]
        assert offers[0]["id"] != offers[1]["id"]
        assert offers[0]["supplier_id"] == offers[1]["supplier_id"]

        observations = await pool.fetch(
            """SELECT observation.offer_id,offer.request_item_id,evidence.research_query_id,
                      observation.price_amount,observation.availability_status
               FROM offer_observations observation
               JOIN offers offer ON offer.id=observation.offer_id
               JOIN source_evidence evidence ON evidence.id=observation.source_evidence_id
               WHERE evidence.external_id=$1 AND evidence.source_type='2gis'
               ORDER BY evidence.research_query_id""",
            firm_id,
        )
        assert len(observations) == 3
        assert len({row["offer_id"] for row in observations}) == 2
        assert {row["research_query_id"] for row in observations} == set(query_ids)
        same_item_observations = [
            row for row in observations if row["request_item_id"] == item_ids[0]
        ]
        other_item_observations = [
            row for row in observations if row["request_item_id"] == item_ids[1]
        ]
        assert len(same_item_observations) == 2
        assert len({row["offer_id"] for row in same_item_observations}) == 1
        assert len(other_item_observations) == 1
        assert other_item_observations[0]["offer_id"] != same_item_observations[0]["offer_id"]
        assert all(row["price_amount"] is None for row in observations)
        assert all(row["availability_status"] == "unknown" for row in observations)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
