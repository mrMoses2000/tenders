"""Focused synthetic checks; PG cases require the exact disposable local target.

These exercise native helpers with fake providers, never the production workflow.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.approvals import payload_sha256
from procurement_bot.cases import IntakeContext, persist_intake_result
from procurement_bot.city_tier_runtime import (
    CITIES,
    _binding,
    _satisfied,
    bootstrap_existing_first_city_run,
    checkpoint_delivery_allowed,
    city_plan,
    city_tick,
    discovery_outcome,
    enum_city,
    full_instruction,
    start_city_research,
    validate_city_admission,
)
from procurement_bot.db import _init_connection, run_migrations
from procurement_bot.intake import IntakeService, ParsedProcurementRequest, ProcurementRequestPatch
from procurement_bot.manual_quotes import ManualQuoteInput, record_manual_quote
from procurement_bot.offer_technical import record_technical_review
from procurement_bot.owner_autonomy import (
    ResearchAutomationPolicy,
    ResearchPolicyDenied,
    autostart_snapshotted_research,
    bind_research_policy,
    load_valid_snapshot,
    record_sourcing_disposition,
    replace_research_profile,
    revoke_research_profile,
)
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.providers.browser import BROWSER_SPEC_SHA256, SupplierBrowserResult
from procurement_bot.queue import claim_job, complete_job
from procurement_bot.research_execution import (
    ResearchExecutionRejected,
    ResearchRunExecutor,
    _request_hash,
)
from procurement_bot.research_repository import (
    _query_payload,
    persist_research_plan,
    recompute_plan_sha256,
)
from procurement_bot.search_profiles import load_search_item_snapshot
from procurement_bot.suppliers import (
    OfferInput,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierInput,
    upsert_offer,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
)
from procurement_bot.worker import JobWorker
from scripts.postgres_test_safety import parse_test_postgres_dsn, verify_postgres_test_connection

CITY_TEST_BUDGET = {"target_suppliers": 20, "max_batches": 8}


def facts(*, ambiguous=False):
    return ParsedProcurementRequest.model_validate(
        {
            "source_type": "document",
            "city": CITIES[0],
            "search_scope": "citywide",
            "delivery_address": "Synthetic delivery destination",
            "items": [
                {
                    "name": f"Synthetic part {i}",
                    "quantity": "2",
                    "unit": "шт",
                    "source_reference": f"page:1:row:{i + 1}",
                    "ambiguous_fields": ["material"] if ambiguous and i == 0 else [],
                    "requirements": [
                        {
                            "name": "Material",
                            "expected_value": "synthetic steel",
                            "source_reference": f"page:1:row:{i + 1}",
                        }
                    ],
                }
                for i in range(3)
            ],
        }
    )


def test_exact_city_adapter_and_ephemeral_original_index_plan():
    source = facts(ambiguous=True)
    before = source.model_dump(mode="json")
    digest = payload_sha256(before)
    for tier, city in enumerate(CITIES):
        assert enum_city(city).value == ("Taraz", "Shymkent", "Almaty")[tier]
        assert enum_city(enum_city(city).value) == enum_city(city)
        plan = city_plan(source, tier, [1, 2])
        assert plan.city == city and len(plan.source_plans) == 8
        assert {row.item_index for row in plan.source_plans} == {1, 2}
        assert all(row.locality.city == city for row in plan.source_plans)
        assert plan.plan_sha256 == recompute_plan_sha256(plan)
    assert source.model_dump(mode="json") == before
    assert payload_sha256(source.model_dump(mode="json")) == digest
    for unknown in ("тараз", "Тараз ", "Shymkent ", "Astana", "Астана"):
        with pytest.raises(ResearchPolicyDenied):
            enum_city(unknown)
    for tier, indices in (
        (3, [1]),
        (-1, [1]),
        (True, [1]),
        (0, [0]),
        (1, [2, 1]),
        (1, [1, 1]),
        (1, [4]),
    ):
        with pytest.raises(ResearchPolicyDenied):
            city_plan(source, tier, indices)


async def test_native_snapshot_keeps_original_uuids_for_a_later_subset():
    plan = city_plan(facts(ambiguous=True), 1, [1, 2])
    run_id, case_id = uuid4(), uuid4()
    ids = [uuid4() for _ in range(3)]
    providers = {
        "own_database": "own_database",
        "2gis": "2gis_web",
        "olx": "marketplace",
        "web": "web",
    }

    class Connection:
        async def fetch(self, sql, *_args):
            if "FROM request_items" in sql:
                return [{"id": item_id, "line_number": i + 1} for i, item_id in enumerate(ids)]
            return [
                {
                    "id": uuid4(),
                    "research_run_id": run_id,
                    "provider": providers[b.source.value],
                    "query_text": b.rendered_query,
                    "city": b.locality.city,
                    "search_area_text": "",
                    "latitude": None,
                    "longitude": None,
                    "radius_meters": None,
                    "parameters": _query_payload(b, i),
                }
                for i, b in enumerate(plan.source_plans)
            ]

    run = {
        "id": run_id,
        "case_id": case_id,
        "search_scope": {
            "contract_version": 1,
            "plan_sha256": plan.plan_sha256,
            "plan": plan.model_dump(mode="json", exclude={"plan_sha256"}),
        },
    }
    snapshot = await ResearchRunExecutor(Connection(), None)._load_snapshot(Connection(), run)
    assert {b.request_item_id for b in snapshot.branches} == set(ids[1:])


@pytest.mark.parametrize("drift", ["city", "hash", "source", "items", "plan"])
async def test_immutable_admission_rejects_city_hash_source_item_and_plan_drift(monkeypatch, drift):
    source = facts()
    snapshot = {
        "id": uuid4(),
        "case_id": uuid4(),
        "snapshot_sha256": "a" * 64,
        "parse_version_id": uuid4(),
        "source_payload_sha256": "b" * 64,
        "applied_payload_sha256": "c" * 64,
    }
    run = uuid4()
    items = [{"id": uuid4()} for _ in source.items]
    plan = city_plan(source, 1, [0, 2])
    binding = _binding(snapshot, run, 1, plan, [0, 2], items, CITY_TEST_BUDGET)
    admission = {
        "snapshot_id": snapshot["id"],
        "case_id": snapshot["case_id"],
        "tier": 1,
        "city": CITIES[1],
        "enum_city": "Shymkent",
        "binding": binding,
        "binding_sha256": payload_sha256(binding),
    }
    scope = {
        "plan_sha256": plan.plan_sha256,
        "plan": plan.model_dump(mode="json", exclude={"plan_sha256"}),
    }

    async def source_items(*_args):
        return source, items

    class Connection:
        async def fetchrow(self, *_args):
            return admission

        async def fetchval(self, *_args):
            return scope

    monkeypatch.setattr("procurement_bot.city_tier_runtime._source_items", source_items)
    assert await validate_city_admission(Connection(), snapshot, run) is admission
    if drift == "city":
        admission["city"] = "Астана"
    elif drift == "hash":
        admission["binding_sha256"] = "0" * 64
    elif drift == "source":
        snapshot["source_payload_sha256"] = "0" * 64
    elif drift == "items":
        items[1]["id"] = uuid4()  # even excluded original identities are immutable
    else:
        scope["plan"]["source_plans"][0]["locality"]["city"] = CITIES[2]
    with pytest.raises(ResearchPolicyDenied):
        await validate_city_admission(Connection(), snapshot, run)


def test_only_proven_success_or_explicit_budget_completes_a_city():
    budget = {"target_suppliers": 20, "max_batches": 8}

    def row(reason, status="succeeded", **extra):
        return {
            "status": status, "stop_reason": reason, "last_error_code": "",
            "live_lease": False, **extra,
        }

    cases = [
        ([row("source_exhausted")], None, "exhausted"),
        ([row("semantic_intents_exhausted")], budget, "exhausted"),
        ([row("target_reached", target_suppliers=20, eligible_suppliers=20)], budget,
         "budget_complete"),
        ([row("batch_limit_reached", max_batches=8, current_batch_no=7)], budget,
         "budget_complete"),
        ([row("target_reached", target_suppliers=20, eligible_suppliers=20, status="skipped")],
         budget, "incomplete"),
        ([row("target_reached", target_suppliers=30, eligible_suppliers=30)], budget,
         "incomplete"),
        ([row("target_reached", target_suppliers=20, eligible_suppliers=20)], None,
         "incomplete"),
        ([row("batch_limit_reached", max_batches=80, current_batch_no=79)], budget,
         "incomplete"),
        ([row("repeated_cursor")], budget, "incomplete"),
        ([row("no_new_candidates")], budget, "incomplete"),
        ([row("access_issue:captcha")], budget, "incomplete"),
        ([row("provider_failure:quota", status="failed")], budget, "failed"),
        ([row("provider_failure:timeout", status="failed")], budget, "failed"),
        ([row("provider_failure:auth_required", status="blocked")], budget, "blocked"),
        ([row("", status="paused")], budget, "paused"),
    ]
    for checkpoints, allowed_budget, expected in cases:
        assert discovery_outcome(
            checkpoints, complete=True, unsafe=False,
            successful_work_budget=allowed_budget,
        ) == expected
        assert discovery_outcome(
            checkpoints, complete=False, unsafe=False,
            successful_work_budget=allowed_budget,
        ) == "incomplete"
    assert discovery_outcome([], complete=True, unsafe=False) == "not_started"


@pytest.mark.parametrize(
    "kind,quantity,expected",
    [
        ("skip", None, True),
        ("skip", Decimal(2), True),
        ("skip", Decimal(1), False),
        ("bought", Decimal(2), True),
        ("bought", Decimal(1), False),
        ("reduce_quantity", Decimal(2), True),
    ],
)
def test_partial_instructions_never_satisfy_full_original_demand(kind, quantity, expected):
    assert (
        full_instruction(
            {"unit": "шт", "disposition": kind, "quantity": quantity},
            {"unit": "шт", "quantity": Decimal(2)},
        )
        is expected
    )


async def test_comparable_filter_requires_stock_technical_and_bound_evidence():
    item, offer, supplier, evidence, review = (uuid4() for _ in range(5))
    row = {
        "offer_id": offer,
        "request_item_id": item,
        "supplier_id": supplier,
        "offer_status": "exact",
        "quantity": Decimal(2),
        "unit": "шт",
        "tender_currency": "KZT",
        "quote_snapshot_id": uuid4(),
        "snapshot": {
            "facts": {
                name: {"value": value, "source_evidence_id": str(evidence), "source_type": "manual"}
                for name, value in {
                    "price_amount": "10",
                    "currency": "KZT",
                    "price_unit": "шт",
                    "available_qty": "2",
                    "availability_status": "in_stock",
                }.items()
            }
        },
    }
    reviews = [
        {
            "id": review,
            "offer_id": offer,
            "request_item_id": item,
            "requirement_id": None,
            "verdict": "match",
            "note": "Synthetic source match",
            "source_evidence_id": evidence,
            "created_at": datetime.now(UTC),
        }
    ]

    class Connection:
        async def fetch(self, sql, *_args):
            if "item_sourcing_dispositions" in sql or "FROM item_requirements" in sql:
                return []
            if "FROM offer_technical_reviews" in sql:
                return reviews
            if "FROM offer_observations" in sql:
                return [{"offer_id": offer, "source_evidence_id": evidence}]
            return [row]

    snapshot = {
        "case_id": uuid4(),
        "owner_user_id": uuid4(),
        "parse_version_id": uuid4(),
        "applied_payload_sha256": "a" * 64,
    }
    assert item in await _satisfied(Connection(), snapshot, [])
    reviews[0]["source_evidence_id"] = None
    assert not await _satisfied(Connection(), snapshot, [])
    reviews[0]["source_evidence_id"] = evidence
    row["snapshot"]["facts"]["available_qty"]["value"] = "1"
    assert not await _satisfied(Connection(), snapshot, [])
    row["snapshot"]["facts"]["available_qty"]["value"] = "2"
    row["snapshot"]["facts"]["price_amount"]["source_type"] = "web"
    assert not await _satisfied(Connection(), snapshot, [])


class Extractor:
    def __init__(self, request):
        self.request = request

    async def extract(self, _text, schema, *, spec_sha256, context_sha256, **_kwargs):
        assert schema is ProcurementRequestPatch
        return schema(spec_sha256=spec_sha256, context_sha256=context_sha256, request=self.request)


class EmptyProvider:
    def __init__(self, *, blocked_index=None):
        self.blocked_index = blocked_index
        self.calls = []

    async def search_suppliers(self, plan):
        self.calls.append((plan.item_index, plan.locality.city))
        if plan.item_index == self.blocked_index:
            raise ResearchExecutionRejected("synthetic_provider_blocked")
        return SupplierBrowserResult(
            spec_sha256=BROWSER_SPEC_SHA256,
            request_sha256=_request_hash(plan),
            candidates=[],
            exhausted=True,
            pages_examined=1,
        )


@pytest.fixture
async def city_pg(tmp_path):
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires 127.0.0.1:55453/test_uran_city; no alternative target allowed")
    target = parse_test_postgres_dsn(dsn, expected_database="test_uran_city", expected_port=55453)
    assert target.host == "127.0.0.1" and target.socket_path is None
    await verify_postgres_test_connection(dsn, target)
    admin = await asyncpg.connect(dsn)
    schema = f"test_city_runtime_{uuid4().hex}"
    pool = None
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=4,
            init=_init_connection,
            server_settings={"search_path": schema},
        )
        applied = await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        assert "046_city_tier_runtime.sql" in applied
        owner = uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) "
            "VALUES($1,800000946,'Synthetic city owner')",
            owner,
        )
        async with pool.acquire() as c:
            profile = await replace_research_profile(
                c,
                owner_id=owner,
                customer_id=None,
                policy=ResearchAutomationPolicy(ordered_cities=CITIES),
                research_autostart=True,
                consent_reference="synthetic-consent",
                consent_text="Synthetic research consent",
            )
        yield SimpleNamespace(pool=pool, owner=owner, profile=profile, results_root=tmp_path)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def new_case(state, *, ambiguous=False, start=True, request=None):
    message = uuid4()
    await state.pool.execute(
        """INSERT INTO messages(id,user_id,channel,direction,external_chat_id,
        external_message_id,message_kind,text_content) VALUES($1,$2,'telegram','inbound',
        '800000946',$3,'document','Synthetic city document')""",
        message,
        state.owner,
        str(message),
    )
    result = await IntakeService(
        Extractor(request or facts(ambiguous=ambiguous))
    ).process(
        "Synthetic city document",
        source_type="document",
    )
    case = await persist_intake_result(
        state.pool,
        message_id=message,
        source_extraction_id=None,
        context=IntakeContext(state.owner, 800000946, None, 0, None),
        result=result,
    )
    if start:
        await autostart_snapshotted_research(
            state.pool, case_id=case, intake_version=1, city_budget=CITY_TEST_BUDGET,
        )
    return case


async def current(state, case):
    return await state.pool.fetchval(
        "SELECT research_run_id FROM case_pipeline_runtime WHERE case_id=$1",
        case,
    )


async def finish_city(state, case, *, blocked_index=None):
    """Native checkpoint executor/queue helpers, synthetic empty provider only."""
    run = await current(state, case)
    provider = EmptyProvider(blocked_index=blocked_index)
    executor = ParallelResearchRunExecutor(state.pool, provider)
    await executor.execute(run)
    excluded = tuple(
        r["kind"]
        for r in await state.pool.fetch(
            "SELECT DISTINCT kind FROM jobs WHERE kind<>'parallel_research_task'",
        )
    )
    result = None
    for _ in range(60):
        job = await claim_job(
            state.pool, worker_id="synthetic-city-worker", excluded_kinds=excluded
        )
        if job is None:
            break
        result = await executor.execute_task(
            job.payload, job_id=job.id, worker_id="synthetic-city-worker"
        )
        assert await complete_job(state.pool, job.id, "synthetic-city-worker")
        if result is not None:
            break
    assert result is not None
    return run, executor, result


@pytest.mark.postgres
async def test_pg_imported_raw_spec_is_exactly_accepted_and_drift_is_rejected(city_pg):
    from procurement_bot.city_tier_runtime import _source_items

    request = ParsedProcurementRequest.model_validate({
        "source_type": "document",
        "city": CITIES[0],
        "search_scope": "citywide",
        "items": [{
            "name": "Synthetic imported item",
            "quantity": "1",
            "unit": "шт",
            "requirements": [{
                "name": "Техническая характеристика",
                "expected_value": "35см",
                "source_reference": "page:1:row:1",
            }],
        }],
    })
    case = await new_case(city_pg, request=request)
    async with city_pg.pool.acquire() as c:
        snapshot_id = await c.fetchval(
            "SELECT research_policy_snapshot_id FROM case_pipeline_runtime WHERE case_id=$1",
            case,
        )
        snapshot = await load_valid_snapshot(c, snapshot_id=snapshot_id, case_id=case)
        item_id = await c.fetchval("SELECT id FROM request_items WHERE case_id=$1", case)
        # Recreate the exact raw specification_text form retained by the legacy importer.
        await c.execute(
            "UPDATE request_items SET specification_text=$2 WHERE id=$1", item_id, "35см",
        )
        _, items = await _source_items(c, snapshot)
        assert items[0]["specification_text"] == "35см"
        await c.execute(
            "UPDATE request_items SET specification_text=$2 WHERE id=$1", item_id, "36см",
        )
        with pytest.raises(ResearchPolicyDenied, match="original item identity drift"):
            await _source_items(c, snapshot)
        await c.execute(
            "UPDATE request_items SET specification_text=$2 WHERE id=$1", item_id, "35см",
        )
        await c.execute(
            "UPDATE item_requirements SET is_hard=false WHERE request_item_id=$1", item_id,
        )
        with pytest.raises(ResearchPolicyDenied, match="source technical requirements drift"):
            await _source_items(c, snapshot)


@pytest.mark.postgres
async def test_pg_migration_046_bootstraps_and_resumes_exact_active_044_taraz_run(
    city_pg, monkeypatch
):
    s = city_pg
    # Rewind only this synthetic schema to the real pre-046 migration state.
    await s.pool.execute("DROP TABLE research_city_item_outcomes")
    await s.pool.execute("DROP TABLE research_city_item_cursors")
    await s.pool.execute("DROP TABLE research_city_report_checkpoints")
    await s.pool.execute("DROP TABLE research_city_admissions")
    await s.pool.execute(
        "DELETE FROM schema_migrations WHERE name='046_city_tier_runtime.sql'"
    )

    case = await new_case(s, start=False)
    # Simulate the deployed 044 first-city start path before migration 046 existed.
    import procurement_bot.city_tier_runtime as city_runtime
    async def no_city_admission(*_args):
        return None
    monkeypatch.setattr(city_runtime, "city_route", lambda _snapshot: False)
    monkeypatch.setattr(city_runtime, "validate_city_admission", no_city_admission)
    monkeypatch.setattr(city_runtime, "activate_city_admission", no_city_admission)
    assert await autostart_snapshotted_research(
        s.pool, case_id=case, intake_version=1
    )
    monkeypatch.undo()
    first_run = await current(s, case)
    # Migration 044 had already constructed real native checkpoint budgets.
    await ParallelResearchRunExecutor(s.pool, EmptyProvider()).execute(first_run)
    before = await s.pool.fetchrow(
        """SELECT r.search_scope,a.binding,a.binding_sha256,
        p.snapshot_sha256,p.source_payload_sha256,p.applied_payload_sha256,
        v.normalized_payload
        FROM research_runs r
        JOIN research_policy_authorizations a ON a.research_run_id=r.id
        JOIN case_pipeline_runtime rt ON rt.research_run_id=r.id
        JOIN case_research_policy_snapshots p ON p.id=rt.research_policy_snapshot_id
        JOIN case_parse_versions v ON v.id=p.parse_version_id
        WHERE r.id=$1""",
        first_run,
    )
    assert await s.pool.fetchval("SELECT count(*) FROM research_runs") == 1
    assert await s.pool.fetchval(
        "SELECT status FROM research_runs WHERE id=$1", first_run
    ) == "running"

    applied = await run_migrations(s.pool, Path(__file__).parents[1] / "migrations")
    assert applied == ["046_city_tier_runtime.sql"]
    async with s.pool.acquire() as c:
        snapshot_id = await c.fetchval(
            "SELECT research_policy_snapshot_id FROM case_pipeline_runtime WHERE case_id=$1",
            case,
        )
        snapshot = await load_valid_snapshot(c, snapshot_id=snapshot_id, case_id=case)
        assert await bootstrap_existing_first_city_run(c, snapshot, first_run)
        assert not await bootstrap_existing_first_city_run(c, snapshot, first_run)
    assert await current(s, case) == first_run
    assert await s.pool.fetchval("SELECT count(*) FROM research_runs") == 1
    assert await s.pool.fetchval(
        "SELECT count(*) FROM research_city_admissions WHERE research_run_id=$1", first_run
    ) == 1
    assert await s.pool.fetchval(
        "SELECT count(*) FROM research_city_item_cursors WHERE current_run_id=$1", first_run
    ) == 3

    # Continue the exact pre-existing run through native task execution after 046.
    await finish_city(s, case)
    async with s.pool.acquire() as c:
        await city_tick(c, case)
    next_run = await current(s, case)
    assert next_run != first_run
    assert await s.pool.fetchval("SELECT count(*) FROM research_runs") == 2
    next_admission = await s.pool.fetchrow(
        "SELECT city,tier,binding FROM research_city_admissions WHERE research_run_id=$1",
        next_run,
    )
    assert (next_admission["city"], next_admission["tier"]) == (CITIES[1], 1)
    await ParallelResearchRunExecutor(s.pool, EmptyProvider()).execute(next_run)
    next_item_id = await s.pool.fetchval(
        "SELECT request_item_id FROM research_city_item_cursors "
        "WHERE current_run_id=$1 ORDER BY request_item_id LIMIT 1",
        next_run,
    )
    async with s.pool.acquire() as c:
        loaded = await load_search_item_snapshot(c, next_run, next_item_id)
    assert loaded.context["city"] == CITIES[1]
    assert await s.pool.fetchval(
        "SELECT city FROM procurement_cases WHERE id=$1", case
    ) == CITIES[0]
    after = await s.pool.fetchrow(
        """SELECT r.search_scope,a.binding,a.binding_sha256,
        p.snapshot_sha256,p.source_payload_sha256,p.applied_payload_sha256,
        v.normalized_payload
        FROM research_runs r
        JOIN research_policy_authorizations a ON a.research_run_id=r.id
        JOIN case_pipeline_runtime rt ON rt.research_policy_snapshot_id=$2
        JOIN case_research_policy_snapshots p ON p.id=rt.research_policy_snapshot_id
        JOIN case_parse_versions v ON v.id=p.parse_version_id
        WHERE r.id=$1""",
        first_run, snapshot_id,
    )
    assert dict(after) == dict(before)
    assert await s.pool.fetchval(
        "SELECT research_run_id FROM case_pipeline_runtime WHERE case_id=$1", case
    ) == next_run


@pytest.mark.postgres
async def test_pg_batch_three_ordered_tiers_partial_items_replay_and_original_ids(city_pg):
    s = city_pg
    case = await new_case(s, ambiguous=True)
    initial_items = await s.pool.fetch("SELECT * FROM request_items ORDER BY line_number")
    source = await s.pool.fetchrow("SELECT * FROM case_parse_versions WHERE case_id=$1", case)
    await asyncio.gather(
        *(autostart_snapshotted_research(
            s.pool, case_id=case, intake_version=1, city_budget=CITY_TEST_BUDGET,
        ) for _ in range(3))
    )
    assert await s.pool.fetchval("SELECT count(*) FROM research_city_admissions") == 1
    for tier in range(3):
        run, _, result = await finish_city(s, case)
        admission = await s.pool.fetchrow(
            "SELECT * FROM research_city_admissions WHERE research_run_id=$1",
            run,
        )
        assert admission["city"] == CITIES[tier] and admission["tier"] == tier
        assert admission["binding"]["item_indices"] == [1, 2]
        assert admission["binding"]["item_ids"] == [str(i["id"]) for i in initial_items[1:]]
        worker = object.__new__(JobWorker)
        worker.pool = s.pool
        await worker._finish_research_result(result, run)
    assert await s.pool.fetchval("SELECT count(*) FROM research_runs") == 3
    assert await s.pool.fetch("SELECT * FROM request_items ORDER BY line_number") == initial_items
    assert (
        await s.pool.fetchrow("SELECT * FROM case_parse_versions WHERE case_id=$1", case) == source
    )
    assert (
        await s.pool.fetchval("SELECT city FROM procurement_cases WHERE id=$1", case) == CITIES[0]
    )
    assert set(
        await s.pool.fetchval(
            "SELECT array_agg(tier) FROM research_city_item_cursors "
            "WHERE request_item_id=ANY($1::uuid[])",
            [r["id"] for r in initial_items[1:]],
        )
    ) == {3}


@pytest.mark.postgres
async def test_pg_successful_native_target_cap_is_budget_complete_and_advances(city_pg):
    s = city_pg
    case = await new_case(s)
    run = await current(s, case)
    items = await s.pool.fetch("SELECT * FROM request_items ORDER BY line_number")
    executor = ParallelResearchRunExecutor(s.pool, EmptyProvider())
    await executor.execute(run)
    # Pending supplier replies are separate dialogue work and do not block discovery.
    async with s.pool.acquire() as c:
        supplier = await upsert_supplier(
            c,
            SupplierInput(
                display_name="Synthetic pending reply",
                identity_type="domain",
                identity_value="pending-city.example",
            ),
        )
        evidence = await upsert_source_evidence(
            c,
            SourceEvidenceInput(
                source_type="manual",
                source_locator="synthetic-only",
                captured_at=datetime.now(UTC),
                content_sha256="a" * 64,
                raw_payload={"synthetic": True},
                idempotency_key="pending-city-evidence",
            ),
        )
        contact = await upsert_supplier_contact(
            c, supplier.id,
            SupplierContactInput(
                contact_type="phone", display_value="+19995550123",
                source_evidence_id=evidence.id,
            ),
        )
    await s.pool.execute(
        """INSERT INTO supplier_conversations(case_id,supplier_id,supplier_contact_id,
        session_name,external_chat_id,state,idempotency_key)
        VALUES($1,$2,$3,'synthetic','pending-only','waiting_price','pending-city-reply')""",
        case, supplier.id, contact.id,
    )
    await finish_city(s, case)
    item_id = items[0]["id"]
    await s.pool.execute(
        """UPDATE research_item_groups SET eligible_suppliers=target_suppliers
        WHERE research_run_id=$1 AND request_item_id=$2""", run, item_id,
    )
    await s.pool.execute(
        """UPDATE research_branch_checkpoints SET status='succeeded',
        stop_reason='target_reached',last_error_code=''
        WHERE research_run_id=$1 AND group_id IN
          (SELECT id FROM research_item_groups WHERE research_run_id=$1 AND request_item_id=$2)""",
        run, item_id,
    )
    await s.pool.execute(
        """UPDATE research_search_intents SET stop_reason='target_reached',recovery_stop_reason=''
        WHERE profile_id IN (SELECT id FROM research_search_profiles
          WHERE research_run_id=$1 AND request_item_id=$2
          AND profile_version=(SELECT max(profile_version) FROM research_search_profiles
            WHERE research_run_id=$1 AND request_item_id=$2))""",
        run, item_id,
    )
    async with s.pool.acquire() as c:
        await city_tick(c, case)
    outcome = await s.pool.fetchrow(
        """SELECT status,proof FROM research_city_item_outcomes
        WHERE research_run_id=$1 AND request_item_id=$2 ORDER BY created_at DESC LIMIT 1""",
        run, item_id,
    )
    assert outcome["status"] == "budget_complete"
    assert outcome["proof"]["successful_work_budget"] == CITY_TEST_BUDGET
    assert outcome["proof"]["checkpoints"]
    assert all(
        cp["target_suppliers"] == CITY_TEST_BUDGET["target_suppliers"]
        and cp["eligible_suppliers"] >= cp["target_suppliers"]
        for cp in outcome["proof"]["checkpoints"]
    )
    cursor = await s.pool.fetchrow(
        "SELECT tier,current_run_id,satisfied FROM research_city_item_cursors "
        "WHERE snapshot_id=(SELECT snapshot_id FROM research_city_admissions "
        "WHERE research_run_id=$1) AND request_item_id=$2",
        run, item_id,
    )
    next_run = await current(s, case)
    assert cursor["tier"] == 1 and cursor["current_run_id"] == next_run
    assert not cursor["satisfied"]
    assert await s.pool.fetchval(
        "SELECT tier FROM research_city_admissions WHERE research_run_id=$1", next_run
    ) == 1
    assert await s.pool.fetchval(
        "SELECT state FROM supplier_conversations WHERE idempotency_key='pending-city-reply'"
    ) == "waiting_price"
    assert await current(s, case) != run


@pytest.mark.postgres
async def test_pg_blocked_neighbour_advances_and_stale_completion_cannot_move_pointer(city_pg):
    s = city_pg
    case = await new_case(s)
    old, executor, result = await finish_city(s, case, blocked_index=0)
    async with s.pool.acquire() as c:
        await city_tick(c, case)
    newer = await current(s, case)
    assert newer != old
    cursors = await s.pool.fetch(
        "SELECT * FROM research_city_item_cursors ORDER BY request_item_id"
    )
    assert sorted(r["tier"] for r in cursors) == [0, 1, 1]
    assert {r["current_run_id"] for r in cursors} == {old, newer}
    # A delayed aggregate completion can write only its own historical run.
    await s.pool.execute("UPDATE research_runs SET status='running' WHERE id=$1", old)
    async with s.pool.acquire() as c:
        await executor._executor._terminalize(
            c, run_id=old, case_id=case, status=result.status, error_code=result.error_code
        )
    assert await current(s, case) == newer
    assert await s.pool.fetchval("SELECT status FROM procurement_cases WHERE id=$1", case) == (
        "researching"
    )
    # Retry/restart does not overlap or create another city batch.
    async with s.pool.acquire() as c:
        await city_tick(c, case)
    assert await current(s, case) == newer


@pytest.mark.postgres
@pytest.mark.parametrize("denial", ["city", "hash", "revoked", "hold", "predecessor"])
async def test_pg_city_hash_revocation_hold_and_predecessor_fail_closed(city_pg, denial):
    s = city_pg
    case = await new_case(s)
    async with s.pool.acquire() as c:
        snapshot_id = await c.fetchval("SELECT id FROM case_research_policy_snapshots")
        snapshot = await load_valid_snapshot(c, snapshot_id=snapshot_id, case_id=case)
        if denial == "revoked":
            await revoke_research_profile(c, owner_id=s.owner, profile_id=s.profile)
            with pytest.raises(ResearchPolicyDenied):
                await load_valid_snapshot(c, snapshot_id=snapshot_id, case_id=case)
        elif denial == "hold":
            await c.execute("UPDATE case_pipeline_runtime SET paused=true WHERE case_id=$1", case)
            with pytest.raises(ResearchPolicyDenied):
                await start_city_research(c, snapshot, 1)
        elif denial == "predecessor":
            assert not await start_city_research(c, snapshot, 2)
            assert await c.fetchval("SELECT count(*) FROM research_runs") == 1
        else:
            plan = city_plan(facts(), 1, [0, 1, 2])
            persisted = await persist_research_plan(
                c, case_id=case, plan=plan, requested_by=s.owner
            )
            # Later city without admission, or a false plan hash, cannot bind policy.
            with pytest.raises(ResearchPolicyDenied):
                await bind_research_policy(
                    c,
                    snapshot_id=snapshot_id,
                    case_id=case,
                    run_id=persisted.id,
                    plan_sha256="0" * 64 if denial == "hash" else plan.plan_sha256,
                )


@pytest.mark.postgres
async def test_pg_source_bound_full_skip_partial_and_evidence_only(city_pg):
    s = city_pg
    case = await new_case(s, start=False)
    items = await s.pool.fetch("SELECT * FROM request_items ORDER BY line_number")
    async with s.pool.acquire() as c:
        source_hash = payload_sha256(
            await c.fetchval("SELECT normalized_payload FROM case_parse_versions")
        )
        for index, item in enumerate(items):
            bound = index != 2
            await record_sourcing_disposition(
                c,
                owner_id=s.owner,
                case_id=case,
                item_id=item["id"],
                artifact_id=None,
                artifact_sha256="a" * 64,
                source_kind="operator_attestation",
                source_reference="synthetic-source-row",
                disposition="skip" if index == 0 else (
                    "reduce_quantity" if index == 3 else "bought"
                ),
                quantity=None if index == 0 else Decimal(1 if index == 1 else 2),
                unit="шт",
                owner_assertion="Synthetic full or partial instruction",
                exact_match_rationale="Synthetic original source line match",
                idempotency_key=str(uuid4()),
                applies_to_sourcing=bound,
                expected_source_payload_sha256=source_hash if bound else None,
            )
    await autostart_snapshotted_research(
        s.pool, case_id=case, intake_version=1, city_budget=CITY_TEST_BUDGET,
    )
    admission = await s.pool.fetchval("SELECT binding FROM research_city_admissions")
    assert admission["item_indices"] == [1, 2]
    assert (
        await s.pool.fetchval("SELECT quantity FROM request_items WHERE id=$1", items[1]["id"]) == 2
    )


@pytest.mark.postgres
async def test_pg_comparable_offer_filters_later_city_and_pending_replies_do_not_block(city_pg):
    s = city_pg
    case = await new_case(s)
    items = await s.pool.fetch("SELECT * FROM request_items ORDER BY line_number")
    async with s.pool.acquire() as c:
        supplier = await upsert_supplier(
            c,
            SupplierInput(
                display_name="Synthetic supplier",
                identity_type="domain",
                identity_value="synthetic-city.example",
            ),
        )
        offer = await upsert_offer(
            c,
            OfferInput(
                request_item_id=items[0]["id"],
                supplier_id=supplier.id,
                idempotency_key="synthetic-offer",
            ),
        )
        quote = await record_manual_quote(
            c,
            case_id=case,
            offer_id=offer.id,
            owner_user_id=s.owner,
            quote=ManualQuoteInput(
                idempotency_key=uuid4(),
                variant_confirmed=True,
                price_amount=Decimal(10),
                price_unit="шт",
                available_qty=Decimal(2),
                availability_status="in_stock",
                observed_at=datetime.now(UTC),
                source_text="Synthetic steel, two units at ten KZT each",
            ),
        )
        requirements = await c.fetch(
            "SELECT id FROM item_requirements WHERE request_item_id=$1", items[0]["id"]
        )
        for rid in [None, *(r["id"] for r in requirements)]:
            await record_technical_review(
                c,
                case_id=case,
                offer_id=offer.id,
                owner_user_id=s.owner,
                requirement_id=rid,
                verdict="match",
                note="Synthetic source-backed steel match",
                source_evidence_id=quote.source_evidence_id,
                idempotency_key=str(uuid4()),
            )
        # Reserved fictional number, local rows only, no messaging boundary invoked.
        contact = await upsert_supplier_contact(
            c,
            supplier.id,
            SupplierContactInput(
                contact_type="phone",
                display_value="+19995550123",
                source_evidence_id=quote.source_evidence_id,
            ),
        )
        # Native pending dialogue is independent of discovery progression.
        await c.execute(
            """INSERT INTO supplier_conversations(case_id,supplier_id,supplier_contact_id,
            session_name,external_chat_id,state,idempotency_key)
            VALUES($1,$2,$3,'synthetic','synthetic-only','waiting_price','synthetic-dialogue')""",
            case,
            supplier.id,
            contact.id,
        )
    await finish_city(s, case)
    async with s.pool.acquire() as c:
        await city_tick(c, case)
    run = await current(s, case)
    admission = await s.pool.fetchval(
        "SELECT binding FROM research_city_admissions WHERE research_run_id=$1", run
    )
    assert admission["city"] == CITIES[1] and admission["item_indices"] == [1, 2]


@pytest.mark.postgres
async def test_pg_live_lease_replay_and_four_hour_report_is_durable_once(city_pg):
    s = city_pg
    case = await new_case(s)
    run = await current(s, case)
    executor = ParallelResearchRunExecutor(s.pool, EmptyProvider())
    await executor.execute(run)
    start_event_before = await s.pool.fetchrow(
        """SELECT id,event_type,data,created_at FROM workflow_events
        WHERE event_type='research_started' AND data->>'research_run_id'=$1""",
        str(run),
    )
    assert start_event_before is not None
    # Expired/stale job ownership is rejected by the native executor before I/O.
    task = await s.pool.fetchrow("SELECT * FROM research_branch_checkpoints LIMIT 1")
    with pytest.raises(ResearchExecutionRejected):
        async with s.pool.acquire() as c:
            await executor._assert_fence(c, task["id"], task["current_job_id"], "stale-worker", 999)
    async with s.pool.acquire() as c:
        await city_tick(c, case, results_root=s.results_root)
    assert await current(s, case) == run  # queued work never proves exhaustion
    assert await s.pool.fetchval("SELECT count(*) FROM report_artifacts") == 0
    # Advance this synthetic checkpoint's clock by four hours, preserving its interval.
    await s.pool.execute(
        "UPDATE research_city_report_checkpoints SET "
        "started_at=started_at-interval '4 hours',due_at=due_at-interval '4 hours'"
    )
    for _ in range(3):
        async with s.pool.acquire() as c:
            await city_tick(c, case, results_root=s.results_root)
    checkpoint = await s.pool.fetchrow("SELECT * FROM research_city_report_checkpoints")
    assert checkpoint["artifact_id"] is not None and checkpoint["outbox_event_id"] is not None
    assert await s.pool.fetchval("SELECT count(*) FROM report_artifacts") == 1
    assert (
        await s.pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE idempotency_key "
            "LIKE 'telegram.city-checkpoint:%'"
        )
        == 1
    )
    assert await s.pool.fetchval(
        "SELECT id FROM outbox_events WHERE id=$1", checkpoint["outbox_event_id"]
    ) == checkpoint["outbox_event_id"]
    assert await s.pool.fetchrow(
        """SELECT id,event_type,data,created_at FROM workflow_events
        WHERE event_type='research_started' AND data->>'research_run_id'=$1""",
        str(run),
    ) == start_event_before
    assert await s.pool.fetchval("SELECT status FROM procurement_cases WHERE id=$1", case) == (
        "researching"
    )
    assert await current(s, case) == run
    async with s.pool.acquire() as c:
        assert await checkpoint_delivery_allowed(c, checkpoint["outbox_event_id"])
        await c.execute("UPDATE case_pipeline_runtime SET paused=true WHERE case_id=$1", case)
        assert not await checkpoint_delivery_allowed(c, checkpoint["outbox_event_id"])
        await c.execute("UPDATE case_pipeline_runtime SET paused=false WHERE case_id=$1", case)
        await revoke_research_profile(c, owner_id=s.owner, profile_id=s.profile)
        assert not await checkpoint_delivery_allowed(c, checkpoint["outbox_event_id"])
