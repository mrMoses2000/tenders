from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import SearchScope
from procurement_bot.research import LocalityConstraint
from procurement_bot.research_scope import (
    ScopeRevisionConflict,
    ScopeRevisionRejected,
    append_citywide_scope_revision,
    load_effective_scope,
)

ROOT = Path(__file__).parents[1]


def _approved_scope(
    city: str, area: str, scope: SearchScope = SearchScope.SPECIFIC_AREA
) -> dict[str, object]:
    plan: dict[str, object] = {
        "city": city,
        "scope": scope.value,
        "locality_resolution": None,
        "locality_task": None,
        "source_plans": [
            {
                "locality": {
                    "city": city,
                    "scope": scope.value,
                    "raw_area_text": area if scope == SearchScope.SPECIFIC_AREA else None,
                    "canonical_place_id": None,
                    "canonical_place_name": None,
                    "point": None,
                }
            }
        ],
    }
    digest = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"contract_version": 1, "plan_sha256": digest, "plan": plan}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_citywide_scope_revision_is_additive_owner_bound_and_idempotent() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_research_scope_{uuid4().hex}"
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
        max_size=3,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, ROOT / "migrations")
        owner_id, other_user_id = uuid4(), uuid4()
        await pool.executemany(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            [
                (owner_id, 800_000_000 + int(owner_id.hex[:8], 16), "owner"),
                (other_user_id, 1_800_000_000 + int(other_user_id.hex[:8], 16), "other"),
            ],
        )

        async def create_paused_run(
            area: str, scope: SearchScope = SearchScope.SPECIFIC_AREA
        ) -> tuple[object, dict[str, object]]:
            case_id, item_id = uuid4(), uuid4()
            await pool.execute(
                "INSERT INTO procurement_cases(id,owner_user_id,city,status) "
                "VALUES ($1,$2,'Алматы','researching')",
                case_id,
                owner_id,
            )
            await pool.execute(
                "INSERT INTO request_items(id,case_id,line_number,name) VALUES ($1,$2,1,'test')",
                item_id,
                case_id,
            )
            frozen = _approved_scope("Алматы", area, scope)
            run_id = await pool.fetchval(
                "INSERT INTO research_runs(case_id,search_scope,idempotency_key) "
                "VALUES ($1,$2,$3) RETURNING id",
                case_id,
                frozen,
                f"test:{uuid4()}",
            )
            await pool.execute(
                "INSERT INTO research_item_groups(research_run_id,request_item_id,status) "
                "VALUES ($1,$2,'paused')",
                run_id,
                item_id,
            )
            return run_id, frozen

        run_id, frozen = await create_paused_run("Северное кольцо")
        frozen_before = await pool.fetchval(
            "SELECT search_scope FROM research_runs WHERE id=$1", run_id
        )
        frozen_locality = LocalityConstraint(
            city="Алматы", scope=SearchScope.SPECIFIC_AREA, raw_area_text="Северное кольцо"
        )

        # Existing runs with no explicit revision retain the original locality.
        async with pool.acquire() as connection:
            unchanged = await load_effective_scope(connection, run_id, frozen_locality)
        assert unchanged.locality == frozen_locality
        assert unchanged.revision is None
        assert unchanged.preferred_area_text is None

        for legacy_scope in (SearchScope.CITYWIDE, SearchScope.ONLINE_WITH_DELIVERY):
            legacy_run_id, _ = await create_paused_run("", legacy_scope)
            legacy_locality = LocalityConstraint(city="Алматы", scope=legacy_scope)
            async with pool.acquire() as connection:
                legacy_effective = await load_effective_scope(
                    connection, legacy_run_id, legacy_locality
                )
            assert legacy_effective.locality == legacy_locality
            assert legacy_effective.revision is None

        async with pool.acquire() as connection:
            with pytest.raises(ScopeRevisionRejected, match="case owner"):
                await append_citywide_scope_revision(
                    connection,
                    run_id,
                    other_user_id,
                    "human-chat:wrong-owner",
                    "Северное кольцо",
                )

        async with pool.acquire() as connection:
            revision = await append_citywide_scope_revision(
                connection,
                run_id,
                owner_id,
                "human-chat:whole-almaty-including-internal-streets",
                "Северное кольцо в приоритете, включая внутренние улицы",
            )
        assert revision.revision == 1
        assert revision.city == "Алматы"
        assert revision.scope == SearchScope.CITYWIDE
        assert revision.base_plan_sha256 == frozen["plan_sha256"]
        assert len(revision.revision_sha256) == 64
        assert revision.to_context()["authorization_reference"] == (
            "human-chat:whole-almaty-including-internal-streets"
        )

        async with pool.acquire() as connection:
            repeated = await append_citywide_scope_revision(
                connection,
                run_id,
                owner_id,
                "human-chat:whole-almaty-including-internal-streets",
                "Северное кольцо в приоритете, включая внутренние улицы",
            )
        assert repeated == revision
        async with pool.acquire() as connection:
            with pytest.raises(ScopeRevisionConflict, match="different payload"):
                await append_citywide_scope_revision(
                    connection,
                    run_id,
                    owner_id,
                    "human-chat:whole-almaty-including-internal-streets",
                    "Другой приоритет",
                )

        async with pool.acquire() as connection:
            effective = await load_effective_scope(connection, run_id, frozen_locality)
        assert effective.locality.city == "Алматы"
        assert effective.locality.scope == SearchScope.CITYWIDE
        assert effective.locality.raw_area_text is None
        assert effective.preferred_area_text == (
            "Северное кольцо в приоритете, включая внутренние улицы"
        )
        assert effective.revision == revision
        assert effective.to_context()["scope_revision"] == revision.to_context()
        async with pool.acquire() as connection:
            with pytest.raises(ScopeRevisionRejected, match="frozen run city and scope"):
                await load_effective_scope(
                    connection,
                    run_id,
                    LocalityConstraint(
                        city="Астана",
                        scope=SearchScope.SPECIFIC_AREA,
                        raw_area_text="Северное кольцо",
                    ),
                )
        assert await pool.fetchval(
            "SELECT search_scope FROM research_runs WHERE id=$1", run_id
        ) == frozen_before

        active_run_id, _ = await create_paused_run("Северное кольцо")
        await pool.execute(
            "UPDATE research_item_groups SET status='queued' WHERE research_run_id=$1",
            active_run_id,
        )
        async with pool.acquire() as connection:
            with pytest.raises(ScopeRevisionRejected, match="groups"):
                await append_citywide_scope_revision(
                    connection,
                    active_run_id,
                    owner_id,
                    "human-chat:active-group",
                    "Северное кольцо",
                )
        await pool.execute(
            "UPDATE research_item_groups SET status='paused' WHERE research_run_id=$1",
            active_run_id,
        )
        await pool.execute(
            "INSERT INTO jobs(kind,payload,status,idempotency_key,locked_at,locked_by,"
            "lease_expires_at) "
            "VALUES ('research_batch',$1,'running',$2,now(),'test-worker',"
            "now()+interval '1 minute')",
            {"research_run_id": str(active_run_id)},
            f"test:running-batch:{uuid4()}",
        )
        async with pool.acquire() as connection:
            with pytest.raises(ScopeRevisionRejected, match="running batch jobs"):
                await append_citywide_scope_revision(
                    connection,
                    active_run_id,
                    owner_id,
                    "human-chat:running-batch",
                    "Северное кольцо",
                )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
