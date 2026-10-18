from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.providers.agy import FakeExtractor
from procurement_bot.providers.browser import BROWSER_SPEC_SHA256, SupplierBrowserResult
from procurement_bot.queue import claim_job, complete_job
from procurement_bot.research import (
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    LocalityCandidate,
    LocalityResolution,
    LocalityResolutionStatus,
    ResearchSource,
    SupplierCandidate,
    SupplierContact,
    SupplierEvidence,
    build_research_plan,
)
from procurement_bot.research_execution import (
    ResearchExecutionRejected,
    ResearchRunExecutor,
    _request_hash,
)
from procurement_bot.research_repository import mark_research_running, persist_research_plan
from procurement_bot.research_scope import append_citywide_scope_revision
from procurement_bot.search_profiles import (
    SearchIntent,
    SearchProfile,
    ensure_search_profiles,
    load_search_intent_plan,
    load_search_item_snapshot,
)

ROOT = Path(__file__).parents[1]
PREFERRED_AREA = "Северное кольцо, включая внутренние улицы"


def _locality_resolution() -> LocalityResolution:
    candidate = LocalityCandidate(
        canonical_name="Северное кольцо",
        place_type="район",
        city="Алматы",
        source=ResearchSource.TWO_GIS,
        provider_place_id="geo-northern-ring",
        source_url="https://2gis.kz/almaty/geo/geo-northern-ring",
        raw_snapshot_sha256="a" * 64,
        confidence=0.98,
    )
    return LocalityResolution(
        phrase="Северное кольцо",
        city="Алматы",
        status=LocalityResolutionStatus.RESOLVED,
        candidates=[candidate],
        selected=candidate,
    )


def _profile() -> SearchProfile:
    return SearchProfile(
        product_name="Марля",
        categories=["магазин медицинских товаров"],
        intents=[
            SearchIntent(
                source=ResearchSource.TWO_GIS,
                intent_type="business",
                category="магазин медицинских товаров",
                query="магазин медицинских товаров Алматы",
                rationale="Находит профильных продавцов.",
                uncertainty=0.2,
            )
        ],
    )


class _ObservedCityBrowser:
    def __init__(self) -> None:
        self.plans = []

    async def search_suppliers(self, plan):
        self.plans.append(plan)
        candidate = SupplierCandidate(
            source=ResearchSource.TWO_GIS,
            external_id="moskvina-firm-001",
            name="Магазин на Москвина",
            address="Алматы, улица Москвина, 12",
            contacts=[
                SupplierContact(
                    kind=ContactKind.PHONE,
                    value="+77000000006",
                    confidence=0.95,
                )
            ],
            evidence=[
                SupplierEvidence(
                    evidence_type=EvidenceType.BUSINESS_LISTING,
                    source=ResearchSource.TWO_GIS,
                    source_url="https://2gis.kz/almaty/firm/moskvina-firm-001",
                    observed_at=datetime.now(UTC),
                    raw_snapshot_sha256="b" * 64,
                    claims=[
                        EvidenceClaim.BUSINESS_IDENTITY,
                        EvidenceClaim.ADDRESS,
                        EvidenceClaim.CONTACT,
                    ],
                )
            ],
            contact_discovery_reason="phone_found",
        )
        return SupplierBrowserResult(
            spec_sha256=BROWSER_SPEC_SHA256,
            request_sha256=_request_hash(plan),
            candidates=[candidate],
            exhausted=True,
            pages_examined=1,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_authorized_citywide_revision_reaches_profiles_and_bounded_batch() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_citywide_execution_{uuid4().hex}"
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
        max_size=8,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, ROOT / "migrations")
        owner_id, case_id, item_id = uuid4(), uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,'scope-test-owner')",
            owner_id,
            700_000_000 + int(owner_id.hex[:8], 16),
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,search_area_text,status) "
            "VALUES ($1,$2,'Citywide scope revision','Алматы','Северное кольцо','researching')",
            case_id,
            owner_id,
        )
        item = ProcurementItem(name="Марля медицинская", quantity=10, unit="рулон")
        await pool.execute(
            "INSERT INTO request_items(id,case_id,line_number,name,quantity,unit,status) "
            "VALUES ($1,$2,1,$3,10,'рулон','ready')",
            item_id,
            case_id,
            item.name,
        )
        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Алматы",
                search_scope=SearchScope.SPECIFIC_AREA,
                search_area_text="Северное кольцо",
                items=[item],
            ),
            locality_resolution=_locality_resolution(),
            minimum_unique_candidates=1,
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection,
                case_id=case_id,
                plan=plan,
                requested_by=owner_id,
            )
            await mark_research_running(connection, persisted.id)
        base_scope = await pool.fetchval(
            "SELECT search_scope FROM research_runs WHERE id=$1", persisted.id
        )
        base_plan_sha256 = persisted.plan_sha256

        browser = _ObservedCityBrowser()
        extractor = FakeExtractor(_profile())
        executor = ParallelResearchRunExecutor(
            pool,
            browser,
            max_active_groups=1,
            target_suppliers=1,
            profile_extractor=extractor,
        )
        assert await executor.execute(persisted.id) is None

        # Save the specific-area profile first. The authorized revision must produce
        # a new current profile with priority queries, not rewrite this old version.
        before_revision = await ensure_search_profiles(
            pool, extractor, persisted.id, [item_id]
        )
        assert before_revision[0].profile.profile_version == 1
        await executor.pause_run(persisted.id)
        assert await pool.fetchval(
            "SELECT bool_and(status='paused') FROM research_item_groups WHERE research_run_id=$1",
            persisted.id,
        )

        async with pool.acquire() as connection:
            revision = await append_citywide_scope_revision(
                connection,
                persisted.id,
                owner_id,
                "human-chat:whole-almaty-northern-ring-priority-internal-streets",
                PREFERRED_AREA,
            )
        assert revision.base_plan_sha256 == base_plan_sha256
        assert await pool.fetchval(
            "SELECT search_scope FROM research_runs WHERE id=$1", persisted.id
        ) == base_scope
        with pytest.raises(
            ResearchExecutionRejected,
            match="scope_revision_requires_parallel_execution",
        ):
            await ResearchRunExecutor(pool, browser).execute(persisted.id)
        assert browser.plans == []

        async with pool.acquire() as connection:
            revised_snapshot = await load_search_item_snapshot(connection, persisted.id, item_id)
        assert revised_snapshot.plan_sha256 == base_plan_sha256
        assert revised_snapshot.context["locality"]["scope"] == SearchScope.CITYWIDE.value
        assert revised_snapshot.context["scope_revision"]["revision_sha256"] == (
            revision.revision_sha256
        )

        after_revision = await ensure_search_profiles(
            pool, extractor, persisted.id, [item_id]
        )
        assert after_revision[0].profile.profile_version == 2
        latest_profile = await pool.fetchrow(
            """SELECT profile.id,profile.profile_version,profile.profile,snapshot_sha256
            FROM research_search_profiles profile
            WHERE profile.research_run_id=$1 AND profile.request_item_id=$2
            ORDER BY profile.profile_version DESC LIMIT 1""",
            persisted.id,
            item_id,
        )
        assert latest_profile["profile_version"] == 2
        assert latest_profile["snapshot_sha256"] == revised_snapshot.snapshot_sha256
        profile_queries = await pool.fetch(
            """SELECT query_text,scope_revision FROM research_search_intents
            WHERE profile_id=$1 AND provider='2gis_web' ORDER BY ordinal""",
            latest_profile["id"],
        )
        assert [row["query_text"] for row in profile_queries] == [
            f"магазин медицинских товаров {PREFERRED_AREA} Алматы",
            "магазин медицинских товаров Алматы",
        ]
        assert all(row["scope_revision"] == 2 for row in profile_queries)

        two_gis_branch = next(
            branch for branch in plan.source_plans if branch.source == ResearchSource.TWO_GIS
        )
        two_gis_query_id = next(
            query_id
            for query_id, branch in zip(persisted.query_ids, plan.source_plans, strict=True)
            if branch.source == ResearchSource.TWO_GIS
        )
        await pool.execute(
            "UPDATE research_search_profiles SET snapshot_sha256=$2 WHERE id=$1",
            latest_profile["id"],
            "f" * 64,
        )
        async with pool.acquire() as connection:
            stale_intent_plan = await load_search_intent_plan(
                connection,
                two_gis_branch,
                two_gis_query_id,
                [],
                None,
            )
        assert stale_intent_plan is None
        await pool.execute(
            "UPDATE research_search_profiles SET snapshot_sha256=$2 WHERE id=$1",
            latest_profile["id"],
            revised_snapshot.snapshot_sha256,
        )

        # Pause fences the checkpoints but leaves their old queue rows intact.
        # Keep those stale generations delayed so the next claim gets the fresh
        # jobs created by the explicit resume below.
        await pool.execute(
            """UPDATE jobs SET available_at=now()+interval '1 hour'
            WHERE payload->>'research_run_id'=$1 AND status IN ('pending','retry')""",
            str(persisted.id),
        )
        await executor.resume_run(persisted.id)

        # The scheduler admits one live child job per item. Complete the local
        # database branch first; the next slot is the revised 2GIS browser branch.
        first_job = await claim_job(pool, worker_id="citywide-scope-worker", lease_seconds=60)
        assert first_job is not None
        first_provider = await pool.fetchval(
            """SELECT query.provider FROM research_branch_checkpoints checkpoint
            JOIN research_queries query ON query.id=checkpoint.query_id WHERE checkpoint.id=$1""",
            UUID(first_job.payload["task_id"]),
        )
        assert first_provider == "own_database"
        assert await executor.execute_task(
            first_job.payload,
            job_id=first_job.id,
            worker_id=first_job.locked_by,
        ) is None
        assert await complete_job(pool, first_job.id, first_job.locked_by)
        await executor.reconcile(persisted.id)

        async with pool.acquire() as connection:
            target = await connection.fetchrow(
                """SELECT job.id,checkpoint.id AS task_id
                FROM jobs job
                JOIN research_branch_checkpoints checkpoint ON checkpoint.current_job_id=job.id
                JOIN research_queries query ON query.id=checkpoint.query_id
                WHERE checkpoint.research_run_id=$1 AND query.provider='2gis_web'
                  AND checkpoint.status='queued' AND job.status IN ('pending','retry')
                ORDER BY checkpoint.id LIMIT 1""",
            persisted.id,
        )
            assert target is not None, await pool.fetch(
                """SELECT query.provider,checkpoint.status,checkpoint.current_job_id,
                    job.status AS job_status,job.last_error_code,checkpoint.last_error_code
                FROM research_branch_checkpoints checkpoint
                JOIN research_queries query ON query.id=checkpoint.query_id
                LEFT JOIN jobs job ON job.id=checkpoint.current_job_id
                WHERE checkpoint.research_run_id=$1 ORDER BY checkpoint.lane_no""",
                persisted.id,
            )
        job = await claim_job(pool, worker_id="citywide-scope-worker", lease_seconds=60)
        assert job is not None and job.id == target["id"]
        result = await executor.execute_task(
            job.payload,
            job_id=job.id,
            worker_id=job.locked_by,
        )
        assert await complete_job(pool, job.id, job.locked_by)
        assert result is None
        assert len(browser.plans) == 1

        executed_plan = browser.plans[0]
        assert executed_plan.locality.scope == SearchScope.CITYWIDE
        assert executed_plan.execution_scope_revision_sha256 == revision.revision_sha256
        assert executed_plan.preferred_area_text == PREFERRED_AREA
        assert executed_plan.browser_payload()["execution_scope_revision_sha256"] == (
            revision.revision_sha256
        )

        checkpoint = await pool.fetchrow(
            """SELECT max_batches,candidates_persisted,candidate_receipts
            FROM research_branch_checkpoints WHERE id=$1""",
            target["task_id"],
        )
        assert checkpoint["max_batches"] == 80
        assert checkpoint["candidates_persisted"] == 1
        assert checkpoint["candidate_receipts"][0]["geographic_decision"] == "city_verified"
        group_progress = await executor.load_item_progress(persisted.id)
        assert group_progress[0]["eligible_suppliers"] == 1
        assert group_progress[0]["candidate_receipts"][0]["geographic_decision"] == (
            "city_verified"
        )
        assert await pool.fetchval(
            "SELECT search_scope FROM research_runs WHERE id=$1", persisted.id
        ) == base_scope
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
