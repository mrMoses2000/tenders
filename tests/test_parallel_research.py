from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.parallel_research import (
    ParallelResearchRunExecutor,
    _batch_candidate_limit,
    _candidate_receipts,
    _cursor_state_after_result,
    _effective_batch_candidate_limit,
    _is_retryable_technical_failure,
    _limited_plan,
    _safe_error_code,
)
from procurement_bot.providers.agy import FakeExtractor
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    SupplierBrowserResult,
)
from procurement_bot.queue import claim_job, complete_job
from procurement_bot.research import (
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    LocalityConstraint,
    ResearchSource,
    SupplierCandidate,
    SupplierContact,
    SupplierEvidence,
    build_research_plan,
)
from procurement_bot.research_execution import (
    ResearchExecutionRejected,
    _QueryBranch,
    _request_hash,
)
from procurement_bot.research_repository import (
    mark_research_running,
    persist_research_plan,
    recompute_plan_sha256,
)
from procurement_bot.search_profiles import (
    SearchProfile,
)
from procurement_bot.suppliers import (
    OfferInput,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierInput,
    SupplierLocationInput,
    upsert_offer,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
    upsert_supplier_location,
)


def _plan(*, include_olx: bool = False):
    return build_research_plan(
        ParsedProcurementRequest(
            city="Алматы",
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(name="Марля", quantity=10, unit="рулон")],
        ),
        include_olx=include_olx,
    )


def test_runtime_batch_controls_do_not_change_approved_plan_hash() -> None:
    plan = _plan()
    branch_plan = next(
        item for item in plan.source_plans if item.source != ResearchSource.OWN_DATABASE
    )
    branch = _QueryBranch(uuid4(), branch_plan, uuid4())
    original = branch_plan.model_dump(mode="json")

    limited = _limited_plan(
        branch,
        batch_candidate_limit=2,
        excluded_external_ids=["listing-1", "listing-2"],
        execution_generation=7,
    )

    assert branch_plan.model_dump(mode="json") == original
    assert limited.batch_candidate_limit == 2
    assert limited.excluded_external_ids == ["listing-1", "listing-2"]
    assert limited.execution_query_id == branch.id
    assert limited.execution_generation == 7
    assert "batch_candidate_limit" not in limited.model_dump(mode="json")
    assert limited.browser_payload()["batch_candidate_limit"] == 2
    assert limited.browser_payload()["excluded_external_ids"] == ["listing-1", "listing-2"]
    assert _request_hash(limited) != _request_hash(branch_plan)


def test_search_cursor_is_execution_only_and_does_not_change_plan_hash() -> None:
    plan = _plan()
    branch_plan = next(
        item for item in plan.source_plans if item.source != ResearchSource.OWN_DATABASE
    )
    with_cursor = branch_plan.model_copy(
        update={"search_cursor": "https://example.2gis.kz/search?cursor=opaque"}
    )
    execution_plan = plan.model_copy(
        update={
            "source_plans": [
                with_cursor if item is branch_plan else item for item in plan.source_plans
            ]
        }
    )

    assert with_cursor.model_dump(mode="json") == branch_plan.model_dump(mode="json")
    assert recompute_plan_sha256(execution_plan) == plan.plan_sha256
    assert _request_hash(with_cursor) != _request_hash(branch_plan)
    assert with_cursor.browser_payload()["search_cursor"] == (
        "https://example.2gis.kz/search?cursor=opaque"
    )


def test_durable_cursor_advances_on_empty_pages_and_stops_cycles() -> None:
    state: dict[str, object] = {"current": None, "seen": []}

    state, advanced, repeated = _cursor_state_after_result(
        state, "https://example.2gis.kz/search?cursor=a", max_seen=3
    )
    assert advanced and not repeated
    assert state == {
        "current": "https://example.2gis.kz/search?cursor=a",
        "seen": ["https://example.2gis.kz/search?cursor=a"],
    }
    # Page 2 can be empty: cursor movement still makes page 3 schedulable.
    state, advanced, repeated = _cursor_state_after_result(
        state, "https://example.2gis.kz/search?cursor=b", max_seen=3
    )
    assert advanced and not repeated
    assert state["current"] == "https://example.2gis.kz/search?cursor=b"
    assert len(state["seen"]) == 2

    state_after_cycle, advanced, repeated = _cursor_state_after_result(
        state, "https://example.2gis.kz/search?cursor=a", max_seen=3
    )
    assert not advanced and repeated
    assert state_after_cycle == state


def test_durable_cursor_history_is_bounded_to_batch_limit() -> None:
    state: dict[str, object] = {"current": None, "seen": []}
    for suffix in "abcd":
        state, advanced, repeated = _cursor_state_after_result(
            state,
            f"https://example.2gis.kz/search?cursor={suffix}",
            max_seen=3,
        )
        assert advanced and not repeated
    assert state["seen"] == [
        "https://example.2gis.kz/search?cursor=b",
        "https://example.2gis.kz/search?cursor=c",
        "https://example.2gis.kz/search?cursor=d",
    ]


def test_runtime_batch_candidate_limit_is_source_specific_without_plan_mutation() -> None:
    plan = _plan(include_olx=True)
    olx_branch = next(item for item in plan.source_plans if item.source == ResearchSource.OLX)
    two_gis_branch = next(
        item for item in plan.source_plans if item.source == ResearchSource.TWO_GIS
    )

    assert _batch_candidate_limit(ResearchSource.OLX) == 1
    assert _batch_candidate_limit(ResearchSource.TWO_GIS) == 2
    assert olx_branch.browser_payload().get("batch_candidate_limit") is None
    original = olx_branch.model_dump(mode="json")
    limited = _limited_plan(
        _QueryBranch(uuid4(), olx_branch, uuid4()),
        batch_candidate_limit=_batch_candidate_limit(ResearchSource.OLX),
        excluded_external_ids=[],
    )
    assert limited.browser_payload()["batch_candidate_limit"] == 1
    two_gis_limited = _limited_plan(
        _QueryBranch(uuid4(), two_gis_branch, uuid4()),
        batch_candidate_limit=_batch_candidate_limit(ResearchSource.TWO_GIS),
        excluded_external_ids=[],
    )
    assert two_gis_limited.browser_payload()["batch_candidate_limit"] == 2
    assert olx_branch.model_dump(mode="json") == original
    assert olx_branch.batch_candidate_limit is None


def test_staged_candidate_limit_caps_two_gis_only_when_configured() -> None:
    assert _batch_candidate_limit(ResearchSource.TWO_GIS) == 2
    staged_limit = _effective_batch_candidate_limit(ResearchSource.TWO_GIS, 1)
    assert staged_limit == 1
    assert _effective_batch_candidate_limit(ResearchSource.TWO_GIS, None) == 2
    assert _effective_batch_candidate_limit(ResearchSource.OLX, 2) == 1
    legacy_plan = _plan()
    branch_plan = next(
        item for item in legacy_plan.source_plans if item.source == ResearchSource.TWO_GIS
    )
    legacy_limited = _limited_plan(
        _QueryBranch(uuid4(), branch_plan, uuid4()),
        batch_candidate_limit=_effective_batch_candidate_limit(ResearchSource.TWO_GIS, None),
        excluded_external_ids=[],
    )
    staged_limited = _limited_plan(
        _QueryBranch(uuid4(), branch_plan, uuid4()),
        batch_candidate_limit=_effective_batch_candidate_limit(ResearchSource.TWO_GIS, 1),
        excluded_external_ids=[],
    )
    assert legacy_limited.browser_payload()["batch_candidate_limit"] == 2
    assert staged_limited.browser_payload()["batch_candidate_limit"] == 1


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("provider_failure:RetryableProviderError:timeout", True),
        ("provider_failure:BrowserProviderError:exit", True),
        ("provider_failure:PermanentProviderError:invalid", False),
        ("source_exhausted", False),
        ("access_issue:no_results", False),
    ],
)
def test_operator_retry_classifies_only_transient_provider_failures(
    reason: str, expected: bool
) -> None:
    assert _is_retryable_technical_failure(reason) is expected


def test_allowlisted_diagnostics_remain_visible_without_changing_retry_classification() -> None:
    class DiagnosedRetryable(RetryableProviderError):
        diagnostic_code = "cli_usage_limit"

        def __str__(self) -> str:
            return "sensitive provider response must not be persisted"

    class DiagnosedPermanent(PermanentProviderError):
        diagnostic_code = "cli_auth_failed"

        def __str__(self) -> str:
            return "sensitive auth response must not be persisted"

    retry_code = _safe_error_code(DiagnosedRetryable())
    permanent_code = _safe_error_code(DiagnosedPermanent())

    assert retry_code == "RetryableProviderError:cli_usage_limit"
    assert permanent_code == "DiagnosedPermanent:cli_auth_failed"
    assert "sensitive" not in retry_code + permanent_code
    assert _is_retryable_technical_failure(f"provider_failure:{retry_code}")
    assert not _is_retryable_technical_failure(f"provider_failure:{permanent_code}")


def test_parallel_executor_enforces_four_stable_lane_slots_and_bounded_batches() -> None:
    class NoopBrowser:
        pass

    executor = ParallelResearchRunExecutor(
        object(),
        NoopBrowser(),
        max_active_groups=3,
        lanes_per_group=4,
        target_suppliers=20,
        max_batches=7,
    )
    assert executor.max_active_groups == 3
    assert executor.lanes_per_group == 4
    assert executor.target_suppliers == 20
    assert executor.max_batches == 7
    with pytest.raises(ValueError):
        ParallelResearchRunExecutor(object(), NoopBrowser(), lanes_per_group=3)
    with pytest.raises(ValueError):
        ParallelResearchRunExecutor(object(), NoopBrowser(), max_batches=101)
    with pytest.raises(ValueError):
        ParallelResearchRunExecutor(object(), NoopBrowser(), card_candidate_limit=0)


def test_candidate_receipts_keep_contact_outcome_without_raw_contact_values() -> None:
    plan = _plan()
    branch_plan = next(
        item for item in plan.source_plans if item.source != ResearchSource.OWN_DATABASE
    )
    result = SupplierBrowserResult(
        spec_sha256=BROWSER_SPEC_SHA256,
        request_sha256=_request_hash(branch_plan),
        candidates=[],
        exhausted=True,
        pages_examined=1,
    )
    assert _candidate_receipts(result, batch_no=0) == []


def test_candidate_receipt_keeps_geographic_decision_without_rejecting_candidate() -> None:
    result = SimpleNamespace(candidates=[SimpleNamespace(
        source=ResearchSource.TWO_GIS,
        external_id="firm-1",
        contacts=[],
        contact_discovery_reason=None,
        address="Северное Кольцо шоссе, 36",
        point=None,
    )])
    locality = LocalityConstraint(
        city="Алматы",
        scope=SearchScope.SPECIFIC_AREA,
        raw_area_text="Северное Кольцо",
    )

    receipt = _candidate_receipts(result, batch_no=0, locality=locality)[0]

    assert receipt["geographic_decision"] == "accepted_by_address"
    assert receipt["external_id"] == "firm-1"


class _PagingBrowser:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: dict[str, list[list[str]]] = {}

    async def search_suppliers(self, plan):
        self.started.set()
        await self.release.wait()
        if plan.source != ResearchSource.TWO_GIS:
            return SupplierBrowserResult(
                spec_sha256=BROWSER_SPEC_SHA256,
                request_sha256=_request_hash(plan),
                candidates=[],
                exhausted=True,
                pages_examined=1,
            )
        product = plan.query_terms[0]
        item_no = int(product.rsplit(" ", 1)[-1])
        excluded = list(plan.excluded_external_ids)
        self.calls.setdefault(product, []).append(excluded)
        external_id = f"{2_000_000_000_000 + item_no * 10 + bool(excluded)}"
        candidate = SupplierCandidate(
            source=ResearchSource.TWO_GIS,
            external_id=external_id,
            name=f"Магазин {external_id}",
            address="Алматы",
            contacts=[
                SupplierContact(
                    kind=ContactKind.PHONE,
                    value=f"+7701123{item_no:03d}{'1' if excluded else '0'}",
                    confidence=0.9,
                )
            ],
            evidence=[
                SupplierEvidence(
                    evidence_type=EvidenceType.BUSINESS_LISTING,
                    source=ResearchSource.TWO_GIS,
                    source_url=f"https://2gis.kz/almaty/firm/{external_id}",
                    observed_at=datetime.now(UTC),
                    raw_snapshot_sha256="0" * 64,
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
            exhausted=bool(excluded),
            pages_examined=1,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_parallel_groups_paging_fencing_and_resume() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_parallel_research_{uuid4().hex}"
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
        max_size=12,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        user_id = uuid4()
        case_id = uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            user_id,
            900_000_000 + int(user_id.hex[:8], 16),
            "Parallel test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,$3,$4,'researching')",
            case_id,
            user_id,
            "Parallel run test",
            "Алматы",
        )
        items = [
            ProcurementItem(name=f"Марля {index}", quantity=10, unit="рулон") for index in range(4)
        ]
        item_ids: list[UUID] = []
        for index, item in enumerate(items, start=1):
            item_ids.append(
                await pool.fetchval(
                    """INSERT INTO request_items(case_id,line_number,name,quantity,unit,status)
                VALUES ($1,$2,$3,$4,$5,'ready') RETURNING id""",
                    case_id,
                    index,
                    item.name,
                    item.quantity,
                    item.unit,
                )
            )
        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Алматы",
                search_scope=SearchScope.CITYWIDE,
                items=items,
            ),
            minimum_unique_candidates=3,
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection,
                case_id=case_id,
                plan=plan,
                requested_by=user_id,
            )
            await mark_research_running(connection, persisted.id)

        browser = _PagingBrowser()
        executor = ParallelResearchRunExecutor(
            pool,
            browser,
            max_active_groups=3,
            lanes_per_group=4,
            target_suppliers=20,
            max_batches=20,
        )
        assert await executor.execute(persisted.id) is None
        assert (
            await pool.fetchval(
                """SELECT count(*) FROM research_item_groups
                WHERE research_run_id=$1 AND status='researching'""",
                persisted.id,
            )
            == 3
        )
        assert (
            await pool.fetchval(
                """SELECT count(*) FROM research_item_groups
                WHERE research_run_id=$1 AND status='queued'""",
                persisted.id,
            )
            == 1
        )
        assert (
            await pool.fetchval(
                """SELECT count(*) FROM research_lanes lane
                JOIN research_item_groups grp ON grp.id=lane.group_id
                WHERE grp.research_run_id=$1""",
                persisted.id,
            )
            == 16
        )

        old_job_ids = await pool.fetch(
            "SELECT current_job_id FROM research_branch_checkpoints WHERE research_run_id=$1",
            persisted.id,
        )
        await executor.pause_run(persisted.id)
        await executor.resume_run(persisted.id)
        new_job_ids = await pool.fetch(
            "SELECT current_job_id,current_batch_no FROM research_branch_checkpoints "
            "WHERE research_run_id=$1 ORDER BY id",
            persisted.id,
        )
        assert all(row["current_batch_no"] == 0 for row in new_job_ids)
        old_job_ids_non_null = {
            row["current_job_id"] for row in old_job_ids if row["current_job_id"] is not None
        }
        new_job_ids_non_null = {
            row["current_job_id"] for row in new_job_ids if row["current_job_id"] is not None
        }
        assert old_job_ids_non_null.isdisjoint(new_job_ids_non_null)
        await pool.execute(
            "UPDATE jobs SET available_at=now()+interval '1 hour' WHERE id=ANY($1::uuid[])",
            [row["current_job_id"] for row in old_job_ids],
        )

        first_job = await claim_job(pool, worker_id="parallel-test-worker", lease_seconds=60)
        assert first_job is not None
        stale_task_id = UUID(first_job.payload["task_id"])
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='researching',
                owner_generation=5,owner_worker=$2,lease_until=$3 WHERE id=$1""",
            stale_task_id,
            first_job.locked_by,
            first_job.lease_expires_at,
        )
        with pytest.raises(ResearchExecutionRejected):
            async with pool.acquire() as connection:
                await executor._assert_fence(
                    connection,
                    stale_task_id,
                    first_job.id,
                    first_job.locked_by,
                    4,
                )
        await pool.execute(
            """UPDATE research_branch_checkpoints
            SET status='queued',owner_worker=NULL,lease_until=NULL WHERE id=$1""",
            stale_task_id,
        )

        async def run_claimed(job):
            result = await executor.execute_task(
                job.payload,
                job_id=job.id,
                worker_id=job.locked_by,
            )
            assert await complete_job(pool, job.id, job.locked_by)
            return result

        first_wave = [first_job]
        for _ in range(30):
            job = await claim_job(pool, worker_id="parallel-test-worker", lease_seconds=60)
            if job is None:
                break
            first_wave.append(job)
        assert len(first_wave) == 3
        first_wave_task_ids = [UUID(job.payload["task_id"]) for job in first_wave]
        assert await pool.fetchval(
            """SELECT count(DISTINCT group_id) FROM research_branch_checkpoints
            WHERE id=ANY($1::uuid[])""",
            first_wave_task_ids,
        ) == 3

        # The first wave has one checkpoint per admitted item group.
        await asyncio.gather(*(run_claimed(job) for job in first_wave))
        second_wave = []
        for _ in range(30):
            job = await claim_job(pool, worker_id="parallel-test-worker", lease_seconds=60)
            if job is None:
                break
            second_wave.append(job)
        assert len(second_wave) == 3
        second_wave_task_ids = [UUID(job.payload["task_id"]) for job in second_wave]
        assert await pool.fetchval(
            """SELECT count(DISTINCT group_id) FROM research_branch_checkpoints
            WHERE id=ANY($1::uuid[])""",
            second_wave_task_ids,
        ) == 3

        # The next round also gives each active item one source slot.
        wave = asyncio.gather(*(run_claimed(job) for job in second_wave))
        await asyncio.wait_for(browser.started.wait(), timeout=10)
        assert (
            await pool.fetchval(
                """SELECT count(*) FROM research_item_groups
                WHERE research_run_id=$1 AND status='researching'""",
                persisted.id,
            )
            == 3
        )
        browser.release.set()
        await wave

        while True:
            jobs = []
            for _ in range(20):
                job = await claim_job(pool, worker_id="parallel-test-worker", lease_seconds=60)
                if job is None:
                    break
                jobs.append(job)
            if not jobs:
                break
            await asyncio.gather(*(run_claimed(job) for job in jobs))

        groups = await pool.fetch(
            """SELECT request_item_id,status,eligible_suppliers,found_leads,
                qualified_price_suppliers,reason
            FROM research_item_groups WHERE research_run_id=$1 ORDER BY request_item_id""",
            persisted.id,
        )
        assert len(groups) == 4
        assert {group["status"] for group in groups} == {"partial"}
        assert {group["eligible_suppliers"] for group in groups} == {2}
        assert {group["found_leads"] for group in groups} == {2}
        assert {group["qualified_price_suppliers"] for group in groups} == {0}
        assert all("target_not_met:2/20" in group["reason"] for group in groups)
        assert all(len(pages) == 2 and pages[0] == [] for pages in browser.calls.values())
        assert all(pages[1] for pages in browser.calls.values())
        progress = await executor.load_item_progress(persisted.id)
        assert len(progress) == 4
        assert all(
            any(
                receipt["contact_discovery_reason"] == "phone_found"
                for receipt in item["candidate_receipts"]
            )
            for item in progress
        )
        terminal_run = await pool.fetchval(
            "SELECT status FROM research_runs WHERE id=$1",
            persisted.id,
        )
        assert terminal_run == "partial"

        retryable_cp = await pool.fetchrow(
            """SELECT cp.* FROM research_branch_checkpoints cp
            WHERE cp.research_run_id=$1 AND cp.status='succeeded'
              AND cp.candidates_persisted>0 AND cp.current_job_id IS NOT NULL
            ORDER BY cp.created_at,cp.id LIMIT 1""",
            persisted.id,
        )
        permanent_cp = await pool.fetchrow(
            """SELECT cp.* FROM research_branch_checkpoints cp
            WHERE cp.research_run_id=$1 AND cp.status='succeeded'
              AND cp.id<>$2 AND cp.current_job_id IS NOT NULL
            ORDER BY cp.created_at,cp.id LIMIT 1""",
            persisted.id,
            retryable_cp["id"],
        )
        assert retryable_cp is not None and permanent_cp is not None
        await pool.execute(
            "UPDATE procurement_cases SET status='report_ready' WHERE id=$1", case_id
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='failed',
                stop_reason='provider_failure:RetryableProviderError:synthetic',
                completed_at=now() WHERE id=$1""",
            retryable_cp["id"],
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='failed',
                stop_reason='provider_failure:PermanentProviderError:synthetic',
                completed_at=now() WHERE id=$1""",
            permanent_cp["id"],
        )
        await pool.execute(
            """UPDATE research_item_groups SET status='partial',completed_at=now()
            WHERE id=ANY($1::uuid[])""",
            [retryable_cp["group_id"], permanent_cp["group_id"]],
        )

        await pool.execute(
            "UPDATE research_item_groups SET status='paused' WHERE id=$1",
            retryable_cp["group_id"],
        )
        with pytest.raises(ResearchExecutionRejected, match="resume the research run"):
            await executor.retry_failed(persisted.id)
        await pool.execute(
            "UPDATE research_item_groups SET status='partial' WHERE id=$1",
            retryable_cp["group_id"],
        )

        retried = await executor.retry_failed(persisted.id)
        assert retried == [retryable_cp["id"]]
        assert await pool.fetchval(
            "SELECT status FROM research_runs WHERE id=$1", persisted.id
        ) == "running"
        assert await pool.fetchval(
            "SELECT status FROM procurement_cases WHERE id=$1", case_id
        ) == "researching"
        retried_cp = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE id=$1", retryable_cp["id"]
        )
        assert retried_cp["status"] == "queued"
        assert retried_cp["owner_generation"] == retryable_cp["owner_generation"] + 1
        assert retried_cp["current_batch_no"] == retryable_cp["current_batch_no"]
        assert retried_cp["excluded_external_ids"] == retryable_cp["excluded_external_ids"]
        assert retried_cp["candidate_receipts"] == retryable_cp["candidate_receipts"]
        assert retried_cp["candidates_persisted"] == retryable_cp["candidates_persisted"]
        retry_job = await pool.fetchrow(
            "SELECT payload,max_attempts,status,available_at FROM jobs WHERE id=$1",
            retried_cp["current_job_id"],
        )
        assert retry_job["payload"]["operator_retry"] is True
        assert retry_job["max_attempts"] == 2
        assert retry_job["status"] == "pending"
        assert retry_job["available_at"] > await pool.fetchval(
            "SELECT now()+interval '20 seconds'"
        )

        await pool.execute(
            "UPDATE jobs SET status='succeeded' WHERE id=$1",
            retried_cp["current_job_id"],
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='failed',
                stop_reason='provider_failure:RetryableProviderError:synthetic_again',
                completed_at=now() WHERE id=$1""",
            retryable_cp["id"],
        )
        assert await executor.retry_failed(persisted.id) == []
        assert await pool.fetchval(
            "SELECT status FROM research_branch_checkpoints WHERE id=$1", permanent_cp["id"]
        ) == "failed"
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reconcile_recovers_stale_checkpoint_and_preserves_pause() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_parallel_reconcile_{uuid4().hex}"
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
        dsn, min_size=1, max_size=4, server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        user_id, case_id = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            user_id, 910_000_000 + int(user_id.hex[:8], 16), "Recovery test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,$3,$4,'researching')",
            case_id, user_id, "Recovery run test", "Almaty",
        )
        item = ProcurementItem(name="Gauze roll", quantity=10, unit="roll")
        await pool.execute(
            "INSERT INTO request_items(case_id,line_number,name,quantity,unit,status) "
            "VALUES ($1,1,$2,$3,$4,'ready')",
            case_id, item.name, item.quantity, item.unit,
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection,
                case_id=case_id,
                plan=build_research_plan(
                    ParsedProcurementRequest(
                        city="Almaty", search_scope=SearchScope.CITYWIDE, items=[item],
                    ),
                ),
                requested_by=user_id,
            )
            await mark_research_running(connection, persisted.id)

        executor = ParallelResearchRunExecutor(pool, _PagingBrowser())
        await executor.execute(persisted.id)
        checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE research_run_id=$1 "
            "AND current_job_id IS NOT NULL ORDER BY id LIMIT 1", persisted.id,
        )
        stage_job = await claim_job(pool, worker_id="recovery-stage-worker", lease_seconds=60)
        assert stage_job is not None and stage_job.id == checkpoint["current_job_id"]
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='researching',
                owner_generation=1,owner_worker=$2,lease_until=$3,batch_attempts=1
                WHERE id=$1""",
            checkpoint["id"], stage_job.locked_by, stage_job.lease_expires_at,
        )
        await pool.execute(
            """UPDATE research_lanes SET status='researching',owner_worker=$2,lease_until=$3
                WHERE id=$1""",
            checkpoint["lane_id"], stage_job.locked_by, stage_job.lease_expires_at,
        )
        stage_checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE id=$1", checkpoint["id"],
        )
        await executor._persist_batch_result(
            persisted.id,
            checkpoint["id"],
            stage_job.id,
            stage_job.locked_by,
            1,
            stage_checkpoint,
            candidates_persisted=0,
            own_database_candidates=0,
            pages_examined=1,
            external_ids=[],
            exhausted=False,
            stop_reason="stage_continues_without_new_card",
            source_progress_token="a" * 64,
        )
        assert await complete_job(pool, stage_job.id, stage_job.locked_by)
        stage_progress = await pool.fetchrow(
            "SELECT status,current_batch_no,source_progress_token,current_job_id "
            "FROM research_branch_checkpoints WHERE id=$1",
            checkpoint["id"],
        )
        assert stage_progress["status"] == "queued"
        assert stage_progress["current_batch_no"] == 1
        assert stage_progress["source_progress_token"] == "a" * 64
        assert stage_progress["current_job_id"] is not None

        checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE id=$1", checkpoint["id"],
        )
        old_job_id = checkpoint["current_job_id"]
        await pool.execute(
            "UPDATE jobs SET status='dead',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL "
            "WHERE id=$1",
            old_job_id,
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='researching',
                owner_generation=4,batch_attempts=1,owner_worker='dead-worker',
                lease_until=now()-interval '1 second' WHERE id=$1""",
            checkpoint["id"],
        )
        assert await executor.reconcile(persisted.id) == 1
        recovered = await pool.fetchrow(
            "SELECT status,owner_generation,current_job_id,batch_attempts "
            "FROM research_branch_checkpoints WHERE id=$1",
            checkpoint["id"],
        )
        assert recovered["status"] == "queued"
        assert recovered["owner_generation"] == 5
        assert recovered["current_job_id"] is not None
        assert recovered["current_job_id"] != old_job_id
        assert recovered["batch_attempts"] == 1

        expired_job_id = recovered["current_job_id"]
        await pool.execute(
            """UPDATE jobs SET status='running',locked_at=now()-interval '2 minutes',
                locked_by='expired-worker',lease_expires_at=now()-interval '1 second'
                WHERE id=$1""",
            expired_job_id,
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='researching',
                owner_worker='expired-worker',lease_until=now()-interval '1 second'
                WHERE id=$1""",
            checkpoint["id"],
        )
        assert await executor.reconcile(persisted.id) == 1
        lease_recovered = await pool.fetchrow(
            "SELECT status,owner_generation,current_job_id FROM research_branch_checkpoints "
            "WHERE id=$1",
            checkpoint["id"],
        )
        assert lease_recovered["status"] == "queued"
        assert lease_recovered["owner_generation"] == 6
        assert lease_recovered["current_job_id"] not in {None, expired_job_id}

        terminal_job_id = lease_recovered["current_job_id"]
        await pool.execute(
            "UPDATE jobs SET status='succeeded',locked_at=NULL,locked_by=NULL,"
            "lease_expires_at=NULL WHERE id=$1",
            terminal_job_id,
        )
        await pool.execute(
            "UPDATE research_branch_checkpoints SET status='researching',batch_attempts=2 "
            "WHERE id=$1",
            checkpoint["id"],
        )
        assert await executor.reconcile(persisted.id) == 1
        terminal = await pool.fetchrow(
            "SELECT status,current_job_id,stop_reason FROM research_branch_checkpoints "
            "WHERE id=$1",
            checkpoint["id"],
        )
        assert terminal["status"] == "failed"
        assert terminal["current_job_id"] is None
        assert terminal["stop_reason"] == "recovery_attempts_exhausted"

        await pool.execute(
            "UPDATE research_item_groups SET status='paused' WHERE id=$1",
            checkpoint["group_id"],
        )
        await pool.execute(
            "UPDATE research_branch_checkpoints SET status='researching',current_job_id=NULL "
            "WHERE id=$1",
            checkpoint["id"],
        )
        assert await executor.reconcile(persisted.id) == 1
        paused = await pool.fetchrow(
            "SELECT status,current_job_id FROM research_branch_checkpoints WHERE id=$1",
            checkpoint["id"],
        )
        assert paused["status"] == "paused"
        assert paused["current_job_id"] is None
        assert await pool.fetchval(
            "SELECT status FROM research_item_groups WHERE id=$1", checkpoint["group_id"]
        ) == "paused"
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_semantic_intent_exhaustion_advances_durable_checkpoint() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_parallel_intents_{uuid4().hex}"
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
        dsn, min_size=1, max_size=6, server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        user_id, case_id = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            user_id, 920_000_000 + int(user_id.hex[:8], 16), "Intent test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,$3,$4,'researching')",
            case_id, user_id, "Intent run test", "Almaty",
        )
        item = ProcurementItem(name="Gauze roll", quantity=10, unit="roll")
        item_id = await pool.fetchval(
            """INSERT INTO request_items(case_id,line_number,name,quantity,unit,status)
            VALUES ($1,1,$2,$3,$4,'ready') RETURNING id""",
            case_id, item.name, item.quantity, item.unit,
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection,
                case_id=case_id,
                plan=build_research_plan(
                    ParsedProcurementRequest(
                        city="Almaty", search_scope=SearchScope.CITYWIDE, items=[item],
                    ),
                ),
                requested_by=user_id,
            )
            await mark_research_running(connection, persisted.id)
        await ParallelResearchRunExecutor(pool, object()).execute(persisted.id)
        extractor_profile = SearchProfile.model_validate({
            "product_name": "Gauze roll",
            "categories": ["medical textile", "medical supplier"],
            "intents": [
                {
                    "source": "web", "intent_type": "product",
                    "category": "medical textile", "query": "gauze roll suppliers",
                    "rationale": "Find product listings.", "uncertainty": 0.1,
                },
                {
                    "source": "web", "intent_type": "business",
                    "category": "medical textile", "query": "medical textile suppliers",
                    "rationale": "Find specialized sellers.", "uncertainty": 0.2,
                },
                {
                    "source": "2gis", "intent_type": "business",
                    "category": "medical supplier", "query": "medical suppliers Almaty",
                    "rationale": "Find local suppliers.", "uncertainty": 0.25,
                },
            ],
        })
        async with pool.acquire() as connection:
            evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="web",
                    source_locator="web:supplier:contact-threshold",
                    source_url="https://supplier.example.test",
                    captured_at=datetime.now(UTC),
                    content_sha256="c" * 64,
                    raw_payload={"name": "Example supplier"},
                    idempotency_key=f"intent-test-evidence:{persisted.id}",
                ),
            )
            supplier = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Example supplier",
                    identity_type="2gis_firm_id",
                    identity_value=f"intent-test-{persisted.id}",
                    source_evidence_id=evidence.id,
                ),
            )
            await upsert_supplier_contact(
                connection,
                supplier.id,
                SupplierContactInput(
                    contact_type="website",
                    display_value="https://supplier.example.test",
                    source_evidence_id=evidence.id,
                ),
            )
            await upsert_offer(
                connection,
                OfferInput(
                    request_item_id=item_id,
                    supplier_id=supplier.id,
                    idempotency_key=f"intent-test-offer:{persisted.id}",
                ),
            )
            eligible_executor = ParallelResearchRunExecutor(pool, object())
            assert await eligible_executor._qualified_supplier_count_for_item(
                connection, item_id, "Almaty"
            ) == 0
            await upsert_supplier_contact(
                connection,
                supplier.id,
                SupplierContactInput(
                    contact_type="phone",
                    display_value="8 700 000 00 12",
                    source_evidence_id=evidence.id,
                ),
            )
            assert await eligible_executor._qualified_supplier_count_for_item(
                connection, item_id, "Almaty"
            ) == 1
            specific_area = LocalityConstraint(
                city="Almaty",
                scope=SearchScope.SPECIFIC_AREA,
                raw_area_text="Северное Кольцо",
            )
            assert await eligible_executor._qualified_supplier_count_for_item(
                connection, item_id, "Almaty", locality=specific_area
            ) == 0
            location = await upsert_supplier_location(
                connection,
                supplier.id,
                SupplierLocationInput(
                    source_evidence_id=evidence.id,
                    city="Almaty",
                    address_text="Северное Кольцо шоссе, 36",
                    source_provider="2gis",
                    provider_place_id=f"intent-location-{persisted.id}",
                ),
            )
            await connection.execute(
                "UPDATE offers SET supplier_location_id=$2 WHERE request_item_id=$1",
                item_id,
                location.id,
            )
            assert await eligible_executor._qualified_supplier_count_for_item(
                connection, item_id, "Almaty", locality=specific_area
            ) == 1

        class EmptyBrowser:
            def __init__(self) -> None:
                self.plans = []
                self.failed_first_intent = False

            async def search_suppliers(self, plan):
                self.plans.append(plan)
                if plan.rendered_query == "gauze roll suppliers" and not self.failed_first_intent:
                    self.failed_first_intent = True
                    raise PermanentProviderError("synthetic provider issue")
                return SupplierBrowserResult(
                    spec_sha256=BROWSER_SPEC_SHA256,
                    request_sha256=_request_hash(plan),
                    candidates=[],
                    exhausted=True,
                    pages_examined=1,
                )

        extractor = FakeExtractor(extractor_profile)
        browser = EmptyBrowser()
        executor = ParallelResearchRunExecutor(
            pool,
            browser,
            profile_extractor=extractor,
        )
        task = None
        for _ in range(4):
            job = await claim_job(pool, worker_id="intent-test-worker", lease_seconds=60)
            assert job is not None
            provider = await pool.fetchval(
                """SELECT query.provider FROM research_queries query
                JOIN research_branch_checkpoints cp ON cp.query_id=query.id
                WHERE cp.id=$1""",
                UUID(job.payload["task_id"]),
            )
            if provider == "web":
                task = await pool.fetchrow(
                    "SELECT * FROM research_branch_checkpoints WHERE id=$1",
                    UUID(job.payload["task_id"]),
                )
                await executor.execute_task(
                    job.payload, job_id=job.id, worker_id=job.locked_by,
                )
                assert await complete_job(pool, job.id, job.locked_by)
                break
            await executor.execute_task(
                job.payload, job_id=job.id, worker_id=job.locked_by,
            )
            assert await complete_job(pool, job.id, job.locked_by)
        assert task is not None
        checkpoint = await pool.fetchrow(
            "SELECT status,current_batch_no,semantic_intent_ordinal,stop_reason "
            "FROM research_branch_checkpoints WHERE id=$1",
            task["id"],
        )
        assert checkpoint["status"] == "queued"
        assert checkpoint["current_batch_no"] == 1
        assert checkpoint["semantic_intent_ordinal"] == 1
        assert checkpoint["stop_reason"] == ""
        web_plan = next(
            plan for plan in browser.plans if plan.rendered_query == "gauze roll suppliers"
        )
        assert web_plan.execution_query_id == task["query_id"]
        assert web_plan.execution_generation == 1
        assert web_plan.execution_intent_ordinal == 0
        assert len(extractor.calls) == 1
        intent = await pool.fetchrow(
            """SELECT intent.stop_reason FROM research_search_intents intent
            JOIN research_search_profiles profile ON profile.id=intent.profile_id
            WHERE profile.research_run_id=$1 AND profile.request_item_id=$2
              AND intent.provider='web' AND intent.ordinal=0""",
            persisted.id,
            item_id,
        )
        assert intent is not None
        assert intent["stop_reason"] == (
            "provider_failure:PermanentProviderError:synthetic provider issue"
        )
        retry_job = await claim_job(pool, worker_id="intent-test-worker", lease_seconds=60)
        assert retry_job is not None
        assert UUID(retry_job.payload["task_id"]) == task["id"]
        await executor.execute_task(
            retry_job.payload,
            job_id=retry_job.id,
            worker_id=retry_job.locked_by,
        )
        assert await complete_job(pool, retry_job.id, retry_job.locked_by)
        final_checkpoint = await pool.fetchrow(
            "SELECT status,current_batch_no,semantic_intent_ordinal,stop_reason "
            "FROM research_branch_checkpoints WHERE id=$1",
            task["id"],
        )
        assert final_checkpoint["status"] == "succeeded"
        assert final_checkpoint["current_batch_no"] == 1
        assert final_checkpoint["semantic_intent_ordinal"] == 1
        assert "semantic_intent_failures:0=provider_failure:" in final_checkpoint["stop_reason"]
        next_intent = await pool.fetchrow(
            """SELECT intent.stop_reason FROM research_search_intents intent
            JOIN research_search_profiles profile ON profile.id=intent.profile_id
            WHERE profile.research_run_id=$1 AND profile.request_item_id=$2
              AND intent.provider='web' AND intent.ordinal=1""",
            persisted.id,
            item_id,
        )
        assert next_intent is not None and next_intent["stop_reason"] == "source_exhausted"
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def test_parallel_migration_is_additive_and_has_fenced_checkpoint_contract() -> None:
    sql = (Path(__file__).parents[1] / "migrations" / "025_parallel_research.sql").read_text()
    lowered = sql.lower()
    assert "create table if not exists research_item_groups" in lowered
    assert "create table if not exists research_lanes" in lowered
    assert "create table if not exists research_branch_checkpoints" in lowered
    assert "unique (research_run_id, request_item_id)" in lowered
    assert "unique (research_run_id, query_id)" in lowered
    assert "check (lane_no between 0 and 3)" in lowered
    assert "owner_generation" in lowered
    assert "current_job_id" in lowered
    assert "legacy_uncheckpointed" in lowered
    assert "candidate_receipts" in lowered
    assert "paused" in lowered
    assert "update research_queries" not in lowered
    assert "delete from source_evidence" not in lowered

    cursor_sql = (
        Path(__file__).parents[1] / "migrations" / "027_research_search_cursor.sql"
    ).read_text().lower()
    assert "add column if not exists search_cursor_state jsonb not null" in cursor_sql
    assert '"current": null, "seen": []' in cursor_sql
    assert "jsonb_typeof(search_cursor_state->'seen') = 'array'" in cursor_sql


def test_retry_and_candidate_target_budgets_are_explicit_and_finite() -> None:
    source = (Path(__file__).parents[1] / "src/procurement_bot/parallel_research.py").read_text()
    assert "_MAX_BATCH_ATTEMPTS = 2" in source
    assert "_BATCH_CANDIDATE_LIMIT = 2" in source
    assert "_MAX_BATCHES_PER_QUERY = 20" in source
    assert "eligible < self.target_suppliers" in source
    assert "batch_limit_reached" in source
    assert "source_exhausted" in source
    assert "contact_discovery_reason" in source
