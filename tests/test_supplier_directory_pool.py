from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.research import ResearchPurpose, ResearchSource, build_research_plan
from procurement_bot.supplier_directory_pool import (
    _validated_page_descriptor,
    directory_pool_identity,
    load_directory_page,
    store_directory_page,
)


def _category_plans():
    research_plan = build_research_plan(
        ParsedProcurementRequest(
            city="Алматы",
            search_scope=SearchScope.CITYWIDE,
            items=[
                ProcurementItem(name="Кисточка 80 мм", quantity=10, unit="шт"),
                ProcurementItem(name="Лак по дереву", quantity=2, unit="л"),
            ],
        )
    )
    branches = [
        branch for branch in research_plan.source_plans
        if branch.source == ResearchSource.TWO_GIS
    ]
    category = branches[0].model_copy(update={"query_terms": ["Строительные магазины"]})
    same_category_other_item = branches[1].model_copy(
        update={
            "query_terms": ["строительные   магазины"],
            "purpose": ResearchPurpose.PRODUCT_EVIDENCE,
            "rendered_query": "лак для дерева Алматы",
        }
    )
    return category, same_category_other_item


def _page_payload() -> dict[str, object]:
    return {
        "spec_sha256": "a" * 64,
        "request_sha256": "b" * 64,
        "cards": [
            {
                "external_id": "101",
                "url": "https://2gis.kz/almaty/firm/101",
                "name": "Строймаркет",
            }
        ],
        "page_url": "https://2gis.kz/almaty/search/stroymarkets",
        "raw_snapshot_sha256": "c" * 64,
        "pages_examined": 1,
        "next_cursor": None,
        "exhausted": True,
        "end_of_results_observed": True,
        "access_issue": None,
    }


def test_directory_pool_accepts_legacy_pages_but_drops_unproven_exhaustion() -> None:
    legacy = _page_payload()
    del legacy["end_of_results_observed"]

    normalized = _validated_page_descriptor(legacy)

    assert normalized["end_of_results_observed"] is False
    assert normalized["exhausted"] is False


def test_directory_pool_keeps_only_proven_non_capped_exhaustion() -> None:
    payload = _page_payload()
    assert _validated_page_descriptor(payload)["exhausted"] is True

    capped = {
        **payload,
        "cards": [
            {"external_id": str(index + 1),
             "url": f"https://2gis.kz/almaty/firm/{index + 1}",
             "name": f"Store {index + 1}"}
            for index in range(20)
        ],
    }
    normalized = _validated_page_descriptor(capped)
    assert normalized["end_of_results_observed"] is True
    assert normalized["exhausted"] is False


def test_directory_pool_identity_uses_category_full_locality_and_cursor_only() -> None:
    first, second = _category_plans()
    key1, *_ = directory_pool_identity(first)
    key2, *_ = directory_pool_identity(second)
    assert key1 == key2
    assert directory_pool_identity(first, search_cursor="cursor-2")[0] != key1
    assert directory_pool_identity(first, search_cursor="cursor-2")[0] != (
        directory_pool_identity(first, search_cursor="cursor-3")[0]
    )

    other_category = second.model_copy(update={"query_terms": ["электротовары"]})
    other_locality = second.model_copy(
        update={"locality": second.locality.model_copy(update={"city": "Астана"})}
    )
    assert directory_pool_identity(other_category)[0] != key1
    assert directory_pool_identity(other_locality)[0] != key1

    unresolved_area = first.locality.model_copy(
        update={"scope": SearchScope.SPECIFIC_AREA, "raw_area_text": "район А"}
    )
    area_plan = first.model_copy(update={"locality": unresolved_area})
    assert directory_pool_identity(area_plan)[0] != key1
    assert directory_pool_identity(area_plan)[2]["point"] is None
    assert directory_pool_identity(area_plan)[2]["canonical_place_id"] is None

    olx = first.model_copy(update={"source": ResearchSource.OLX})
    with pytest.raises(ValueError, match="2GIS only"):
        directory_pool_identity(olx)


def test_priority_scope_revision_isolated_from_whole_city_page_cache() -> None:
    first, _second = _category_plans()
    revision_hash = "d" * 64
    priority_query = first.model_copy(
        update={
            "execution_scope_revision_sha256": revision_hash,
            "preferred_area_text": "Северное кольцо",
            "rendered_query": "Кисточка Алматы Северное кольцо",
        }
    )
    whole_city_query = first.model_copy(
        update={
            "execution_scope_revision_sha256": revision_hash,
            "preferred_area_text": "Северное кольцо",
            "rendered_query": "Кисточка Алматы",
        }
    )

    assert priority_query.locality.scope == SearchScope.CITYWIDE
    assert priority_query.locality == whole_city_query.locality
    assert directory_pool_identity(priority_query)[0] != directory_pool_identity(
        whole_city_query
    )[0]


def test_exact_revision_query_reuses_page_across_items() -> None:
    first, second = _category_plans()
    revision_hash = "e" * 64
    shared_query = "Строительные магазины Алматы Северное кольцо"
    first = first.model_copy(
        update={
            "execution_scope_revision_sha256": revision_hash,
            "preferred_area_text": "Северное кольцо",
            "rendered_query": shared_query,
        }
    )
    second = second.model_copy(
        update={
            "execution_scope_revision_sha256": revision_hash,
            "preferred_area_text": "Северное кольцо",
            "rendered_query": shared_query,
        }
    )

    assert first.item_index != second.item_index
    assert directory_pool_identity(first)[0] == directory_pool_identity(second)[0]


def test_legacy_unrevised_pool_identity_still_ignores_rendered_query() -> None:
    first, _second = _category_plans()
    same_identity_different_query = first.model_copy(
        update={"rendered_query": "Другая поисковая формулировка Алматы"}
    )

    assert first.execution_scope_revision_sha256 is None
    assert directory_pool_identity(first)[0] == directory_pool_identity(
        same_identity_different_query
    )[0]


@pytest.mark.asyncio
async def test_postgres_directory_pool_reuses_descriptors_with_ttl_and_no_facts() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_directory_pool_{uuid4().hex}"
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
        first, second = _category_plans()
        payload = _page_payload()
        async with pool.acquire() as connection:
            await store_directory_page(connection, first, payload)
            hit = await load_directory_page(connection, second)
            assert hit == payload
            assert hit["request_sha256"] == "b" * 64

            # The whole locality and category are part of the key; unresolved
            # locality receives no implicit point or canonical-area match.
            changed_scope = second.model_copy(
                update={
                    "locality": second.locality.model_copy(
                        update={"city": "Астана", "scope": SearchScope.SPECIFIC_AREA,
                                "raw_area_text": "район А"}
                    )
                }
            )
            assert await load_directory_page(connection, changed_scope) is None
            changed_query = second.model_copy(update={"query_terms": ["электротовары"]})
            assert await load_directory_page(connection, changed_query) is None
            assert await load_directory_page(connection, second, search_cursor="next-page") is None

            cache_key = directory_pool_identity(first)[0]
            await connection.execute(
                "UPDATE supplier_directory_page_pool SET created_at=now()-interval '2 hours', "
                "expires_at=now()+interval '20 hours' "
                "WHERE cache_key=$1",
                cache_key,
            )
            short_ttl = second.model_copy(update={"result_ttl_hours": 1})
            assert await load_directory_page(connection, short_ttl) is None
            assert await load_directory_page(connection, second) == payload
            await connection.execute(
                "UPDATE supplier_directory_page_pool SET expires_at=now()-interval '1 second' "
                "WHERE cache_key=$1",
                cache_key,
            )
            assert await load_directory_page(connection, second) is None

            malicious = {**payload, "supplier_match_claim": "exact"}
            with pytest.raises(ValueError, match="DiscoveryPage fields"):
                await store_directory_page(connection, first, malicious)
            assert await connection.fetchval(
                "SELECT count(*) FROM supplier_directory_page_pool"
            ) == 1
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
