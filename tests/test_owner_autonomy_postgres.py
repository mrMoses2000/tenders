"""Synthetic, real-migration native research tests; no provider network calls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import asyncpg
import fitz
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from docx import Document

from procurement_bot.approvals import approve_request
from procurement_bot.cases import IntakeContext, persist_intake_result
from procurement_bot.config import Settings
from procurement_bot.db import _init_connection, run_migrations
from procurement_bot.dialogue import SearchScopePolicy, evaluate_missing_blockers
from procurement_bot.documents import extract_document, joined_text
from procurement_bot.intake import IntakeService, ParsedProcurementRequest, ProcurementRequestPatch
from procurement_bot.mini_app import create_mini_app
from procurement_bot.owner_autonomy import (
    ResearchAutomationPolicy,
    ResearchPolicyDenied,
    autostart_snapshotted_research,
    bind_research_policy,
    record_sourcing_disposition,
    replace_research_profile,
    revoke_research_profile,
)
from procurement_bot.research_workflow import ResearchStartDenied, start_approved_research
from procurement_bot.worker import JobWorker
from scripts.postgres_test_safety import parse_test_postgres_dsn, verify_postgres_test_connection

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def owner_pg():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires the disposable owner-autonomy PostgreSQL cluster")
    target = parse_test_postgres_dsn(dsn, expected_database="test_uran_owner", expected_port=55448)
    await verify_postgres_test_connection(dsn, target)
    admin = await asyncpg.connect(dsn)
    schema = f"owner_autonomy_{uuid4().hex}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, init=_init_connection,
            server_settings={"search_path": schema},
        )
        applied = await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        assert "044_owner_research_autonomy.sql" in applied
        owner, other = uuid4(), uuid4()
        for index, user in enumerate((owner, other)):
            await pool.execute(
                "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,'Synthetic owner')",
                user, 800_000_000 + index,
            )
        yield SimpleNamespace(pool=pool, owner=owner, other=other, schema=schema)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def request_facts(*, city=None, ambiguous=False):
    return ParsedProcurementRequest.model_validate({
        "source_type": "document", "city": city,
        "search_scope": "citywide" if city else None,
        "delivery_address": "Synthetic destination in a different city",
        "warnings": ["SOURCE-E27: inspect source row"],
        "items": [{"name": "Synthetic cable", "quantity": "2", "unit": "m",
                   "source_reference": "page:1:row:1"},
                  {"name": "Synthetic mat", "quantity": "1", "unit": "piece",
                   "source_reference": "page:1:row:2",
                   "ambiguous_fields": ["thickness"] if ambiguous else [],
                   "requirements": [{"name": "Thickness", "expected_value": "8 / 10 mm",
                                     "ambiguous": ambiguous, "source_reference": "page:1:row:2"}]}],
    })


class FakeExtractor:
    def __init__(self, request):
        self.request = request

    async def extract(self, text, schema, *, spec_sha256, context_sha256, **kwargs):
        assert text and schema is ProcurementRequestPatch
        return ProcurementRequestPatch(spec_sha256=spec_sha256,
                                       context_sha256=context_sha256, request=self.request)


async def enroll(state, *, owner=None, customer=None, cities=("City A", "City B", "City C")):
    async with state.pool.acquire() as connection:
        return await replace_research_profile(
            connection, owner_id=owner or state.owner, customer_id=customer,
            policy=ResearchAutomationPolicy(ordered_cities=cities),
            consent_reference="synthetic-consent-v1", consent_text="I consent to public research.",
            research_autostart=True,
        )


async def intake_case(state, *, request=None, customer=None, message_id=None):
    request = request or request_facts()
    message = message_id or uuid4()
    if message_id is None:
        await state.pool.execute(
            """INSERT INTO messages(id,user_id,channel,direction,external_chat_id,
            external_message_id,message_kind,text_content) VALUES($1,$2,'telegram','inbound',
            '800000000',$3,'document','Synthetic request')""", message, state.owner, str(message),
        )
    result = await IntakeService(FakeExtractor(request)).process(
        "Synthetic document facts", source_type="document",
    )
    case = await persist_intake_result(
        state.pool, message_id=message, source_extraction_id=None,
        context=IntakeContext(state.owner, 800_000_000, None, 0, None),
        result=result, customer_id=customer,
    )
    return case, message, result


def worker_for(state):
    worker = object.__new__(JobWorker)
    worker.pool = state.pool
    worker.research_executor = object()  # prepare only; no external calls
    worker.locality_browser = None
    return worker


async def prepare(state, case):
    await worker_for(state)._prepare_research({"case_id": str(case), "intake_version": 1})


async def test_default_off_exact_manual_approval_compatibility(owner_pg):
    state = owner_pg
    case, _, _ = await intake_case(state, request=request_facts(city="City A"))
    await prepare(state, case)
    async with state.pool.acquire() as c:
        approval = await c.fetchrow("SELECT * FROM action_approvals WHERE case_id=$1", case)
        assert set(approval["payload"]) == {"case_id", "research_run_id", "plan_sha256"}
        assert approval["status"] == "requested"
        assert await c.fetchval("SELECT count(*) FROM case_research_policy_snapshots") == 0
        assert await c.fetchval("SELECT count(*) FROM jobs WHERE kind='execute_research'") == 0
        assert await approve_request(c, approval_id=approval["id"], approved_by=state.owner)
    result = await start_approved_research(state.pool, approval_id=approval["id"])
    assert result.newly_started


async def test_cityless_document_defaults_native_start_restart_and_separate_whatsapp(owner_pg):
    state = owner_pg
    await enroll(state)
    case, message, original = await intake_case(state)
    assert not original.ready_to_search
    await prepare(state, case)
    # New worker instances and concurrent native retries must resolve the same start.
    await asyncio.gather(*(prepare(state, case) for _ in range(3)))
    replay, _, _ = await intake_case(state, message_id=message)
    assert replay == case
    async with state.pool.acquire() as c:
        snapshot = await c.fetchrow("SELECT * FROM case_research_policy_snapshots")
        parsed = await c.fetchrow("SELECT * FROM case_parse_versions WHERE case_id=$1", case)
        run = await c.fetchrow("SELECT * FROM research_runs WHERE case_id=$1", case)
        runtime = await c.fetchrow("SELECT * FROM case_pipeline_runtime WHERE case_id=$1", case)
        assert parsed["normalized_payload"]["city"] == "City A"
        assert parsed["normalized_payload"]["delivery_address"] == original.request.delivery_address
        assert snapshot["snapshot"]["source_geography"]["city"] is None
        assert snapshot["snapshot"]["applied_defaults"] == {
            "city": "City A", "search_scope": "citywide"}
        assert run["status"] == "running" and run["search_scope"]["plan"]["city"] == "City A"
        assert runtime["research_policy_snapshot_id"] == snapshot["id"]
        assert (runtime["report_checkpoint_at"] - run["started_at"]).total_seconds() == 4 * 3600
        assert runtime["active_city_tier"] == 0
        for table in ("procurement_cases", "case_research_policy_snapshots", "research_runs",
                      "research_policy_authorizations", "action_approvals"):
            assert await c.fetchval(f"SELECT count(*) FROM {table}") == 1  # noqa: S608
        assert await c.fetchval("SELECT count(*) FROM jobs WHERE kind='execute_research'") == 1
        assert await c.fetchval("SELECT count(*) FROM case_auto_send_policies") == 0
        assert await c.fetchval("SELECT count(*) FROM outbox_events "
                               "WHERE event_type LIKE 'whatsapp%'") == 0
        event = await c.fetchval("SELECT data FROM workflow_events "
                                 "WHERE event_type='research_started'")
        assert event["authorization_mode"] == "owner_research_profile"


async def test_partial_item_conflict_keeps_clear_indices_and_all_source_ids(owner_pg):
    state = owner_pg
    await enroll(state)
    facts = request_facts(ambiguous=True)
    # Put the ambiguous line first to catch accidental index compaction.
    facts.items.reverse()
    gaps = evaluate_missing_blockers(facts, SearchScopePolicy.require_explicit())
    assert not gaps.ready_to_search
    case, _, _ = await intake_case(state, request=facts)
    before = await state.pool.fetch("SELECT id,line_number FROM request_items ORDER BY line_number")
    await prepare(state, case)
    plan = await state.pool.fetchval("SELECT search_scope FROM research_runs")
    assert {branch["item_index"] for branch in plan["plan"]["source_plans"]} == {1}
    after = await state.pool.fetch("SELECT id,line_number FROM request_items ORDER BY line_number")
    assert before == after and len(after) == 2
    snap = await state.pool.fetchval("SELECT snapshot FROM case_research_policy_snapshots")
    assert snap["items"][0]["issues"] and not snap["items"][1]["issues"]


@pytest.mark.parametrize("reason", ["wrong_owner", "wrong_project", "revoked", "conflict"])
async def test_ineligible_profile_stays_manual(owner_pg, reason):
    state = owner_pg
    if reason == "wrong_project":
        project = await state.pool.fetchval(
            "INSERT INTO customers(owner_user_id,display_name) VALUES($1,'Synthetic project') "
            "RETURNING id", state.owner,
        )
        await enroll(state, customer=project)
    else:
        profile = await enroll(state, owner=state.other if reason == "wrong_owner" else state.owner)
        if reason == "revoked":
            async with state.pool.acquire() as c:
                await revoke_research_profile(c, owner_id=state.owner, profile_id=profile)
    case, _, _ = await intake_case(state, request=request_facts(city="Different City"
                                                             if reason == "conflict" else "City A"))
    await prepare(state, case)
    assert await state.pool.fetchval("SELECT status FROM action_approvals") == "requested"
    assert await state.pool.fetchval("SELECT count(*) FROM jobs WHERE kind='execute_research'") == 0


@pytest.mark.parametrize("drift", ["revoked", "owner", "project", "intake", "plan"])
async def test_snapshot_and_start_fail_closed_on_drift(owner_pg, drift):
    state = owner_pg
    profile = await enroll(state)
    case, _, _ = await intake_case(state)
    if drift == "revoked":
        async with state.pool.acquire() as c:
            await revoke_research_profile(c, owner_id=state.owner, profile_id=profile)
    elif drift == "owner":
        await state.pool.execute("UPDATE procurement_cases SET owner_user_id=$2 WHERE id=$1",
                                 case, state.other)
    elif drift == "project":
        project = await state.pool.fetchval(
            "INSERT INTO customers(owner_user_id,display_name) VALUES($1,'Another project') "
            "RETURNING id", state.owner,
        )
        await state.pool.execute("UPDATE procurement_cases SET customer_id=$2 WHERE id=$1",
                                 case, project)
    elif drift == "intake":
        await state.pool.execute("UPDATE case_parse_versions SET normalized_payload="
                                 "jsonb_set(normalized_payload,'{city}','\"Changed\"')")
    else:
        # Start once, then tamper with the persisted plan; exact replay must reject it.
        await prepare(state, case)
        await state.pool.execute("UPDATE research_runs SET search_scope="
                                 "jsonb_set(search_scope,'{plan,city}','\"Changed\"')")
        approval = await state.pool.fetchval("SELECT id FROM action_approvals")
        with pytest.raises(ResearchStartDenied, match="plan hash"):
            await start_approved_research(state.pool, approval_id=approval)
        return
    with pytest.raises(ResearchPolicyDenied):
        await autostart_snapshotted_research(state.pool, case_id=case, intake_version=1)
    assert await state.pool.fetchval("SELECT count(*) FROM research_runs") == 0


@pytest.mark.parametrize("kind", ["pdf", "docx"])
async def test_generated_document_extraction_to_native_autostart(owner_pg, tmp_path, kind):
    state = owner_pg
    path = tmp_path / f"synthetic-request.{kind}"
    if kind == "pdf":
        pdf = fitz.open()
        pdf.new_page().insert_text((72, 72), "Synthetic cable, 2 m; Synthetic mat, 1 piece")
        pdf.save(path)
        pdf.close()
    else:
        doc = Document()
        doc.add_paragraph("Synthetic cable, 2 m; Synthetic mat, 1 piece")
        doc.save(path)
    content = joined_text(extract_document(path, kind))
    assert "Synthetic cable" in content
    await enroll(state)
    message, attachment = uuid4(), uuid4()
    await state.pool.execute(
        """INSERT INTO messages(id,user_id,channel,direction,external_chat_id,
        external_message_id,message_kind) VALUES($1,$2,'telegram','inbound',
        '800000000',$3,'document')""", message, state.owner, str(message),
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    await state.pool.execute(
        """INSERT INTO incoming_attachments(id,message_id,external_file_id,kind,status,
        storage_path,sha256) VALUES($1,$2,$3,'document','downloaded',$4,$5)""",
        attachment, message, str(attachment), str(path), digest,
    )
    extraction = await state.pool.fetchval(
        """INSERT INTO content_extractions(attachment_id,extraction_kind,engine,status,
        content_text,completed_at) VALUES($1,'document_text','synthetic','succeeded',$2,now())
        RETURNING id""", attachment, content,
    )
    worker = worker_for(state)
    worker.intake = IntakeService(FakeExtractor(request_facts()))
    await worker._process_intake({"incoming_message_id": str(message),
                                  "extraction_id": str(extraction), "input_kind": "document"})
    case = await state.pool.fetchval("SELECT id FROM procurement_cases")
    await prepare(state, case)
    assert await state.pool.fetchval("SELECT status FROM research_runs") == "running"
    applied_extraction = await state.pool.fetchval(
        "SELECT source_extraction_id FROM case_parse_versions")
    assert applied_extraction == extraction
    # Exact duplicate worker input is a no-op, including its dialogue state.
    await worker._process_intake({"incoming_message_id": str(message),
                                  "extraction_id": str(extraction), "input_kind": "document"})
    assert await state.pool.fetchval("SELECT count(*) FROM procurement_cases") == 1


def signed_headers(user_id=800_000_000):
    values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", b"synthetic-token", hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": "tma " + urlencode(values)}


def mini_client(state):
    settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="synthetic-token",  # noqa: S106
                        telegram_allowed_user_ids="800000000,800000001")
    return TestClient(TestServer(create_mini_app(state.pool, settings)))


async def test_miniapp_owner_warning_read_and_crossowner_unauthorized(owner_pg):
    state = owner_pg
    await enroll(state)
    case, _, _ = await intake_case(state, request=request_facts(ambiguous=True))
    await prepare(state, case)
    path = f"/api/cases/{case}/source-visibility"
    async with mini_client(state) as client:
        assert (await client.get(path)).status == 401
        assert (await client.get(path, headers=signed_headers(800_000_002))).status == 403
        # Authenticated other owners get no case existence/data disclosure.
        assert (await client.get(path, headers=signed_headers(800_000_001))).status == 404
        response = await client.get(path, headers=signed_headers())
        assert response.status == 200
        data = await response.json()
        assert data["warnings"] == ["SOURCE-E27: inspect source row"]
        assert data["sourcing_city"] == "City A"
        assert data["delivery_destination"] == "Synthetic destination in a different city"
        assert data["items"][1]["issues"] == ["thickness", "requirements.0.ambiguous"]
        assert data["items"][1]["source_reference"] == "page:1:row:2"
        assert data["items"][1]["stage"] == "needs_review"
        assert data["items"][0]["stage"] == "ready_for_search"
        detail_response = await client.get(f"/api/cases/{case}", headers=signed_headers())
        assert detail_response.status == 200
        detail = await detail_response.json()
        assert detail["source_visibility"] == data
        serialized = json.dumps(data)
        assert "tender_unit_price_private" not in serialized and "storage_path" not in serialized


async def test_profile_api_explicit_consent_exact_project_and_revocation(owner_pg):
    state = owner_pg
    project = await state.pool.fetchval(
        "INSERT INTO customers(owner_user_id,display_name) VALUES($1,'Owned project') RETURNING id",
        state.owner,
    )
    body = {"customer_id": str(project), "policy": {"ordered_cities": ["City A", "City B"]},
            "consent_reference": "owner-action:synthetic-v1", "consent_text": "Research only.",
            "research_autostart": True}
    async with mini_client(state) as client:
        assert (await client.post("/api/research-profiles", json=body)).status == 401
        foreign = await client.post("/api/research-profiles", json=body,
                                    headers=signed_headers(800_000_001))
        assert foreign.status == 409
        invalid = await client.post("/api/research-profiles", json={**body, "consent_text": ""},
                                    headers=signed_headers())
        assert invalid.status == 400
        # Outbound consent fields cannot sneak into research policies.
        invalid = await client.post(
            "/api/research-profiles", json={
                **body, "policy": {**body["policy"], "whatsapp_enabled": True}
            }, headers=signed_headers(),
        )
        assert invalid.status == 400
        response = await client.post("/api/research-profiles", json=body, headers=signed_headers())
        assert response.status == 201
        profile_id = (await response.json())["profile_id"]
        repeated = await client.post("/api/research-profiles", json=body, headers=signed_headers())
        assert (await repeated.json())["profile_id"] == profile_id
        assert (await client.post(f"/api/research-profiles/{profile_id}/revoke",
                                  headers=signed_headers(800_000_001))).status == 404
        listing = await client.get("/api/research-profiles", headers=signed_headers(800_000_001))
        assert (await listing.json())["profiles"] == []
        response = await client.post(f"/api/research-profiles/{profile_id}/revoke",
                                     headers=signed_headers())
        assert response.status == 200
    assert await state.pool.fetchval("SELECT count(*) FROM case_auto_send_policies") == 0


async def test_project_profile_applies_only_authenticated_exact_project_intake(owner_pg):
    state = owner_pg
    project = await state.pool.fetchval(
        "INSERT INTO customers(owner_user_id,display_name) VALUES($1,'Exact project') RETURNING id",
        state.owner,
    )
    await enroll(state, customer=project)
    case, _, _ = await intake_case(state, customer=project)
    await prepare(state, case)
    snapshot = await state.pool.fetchrow("SELECT * FROM case_research_policy_snapshots")
    assert snapshot["customer_id"] == project
    assert await state.pool.fetchval("SELECT status FROM research_runs") == "running"


async def test_immutable_episodes_null_uniqueness_and_snapshot_and_binding(owner_pg):
    state = owner_pg
    first = await enroll(state)
    async with state.pool.acquire() as c:
        with pytest.raises(asyncpg.PostgresError, match="immutable"):
            await c.execute("UPDATE owner_research_automation_profiles SET consent_text='changed'")
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute(
                """INSERT INTO owner_research_automation_profiles(owner_user_id,version,
                research_autostart,policy,policy_sha256,consent_reference,consent_text)
                SELECT owner_user_id,version+1,true,policy,policy_sha256,'second','duplicate'
                FROM owner_research_automation_profiles""")
        second = await replace_research_profile(
            c, owner_id=state.owner, customer_id=None, expected_profile_id=first,
            policy=ResearchAutomationPolicy(ordered_cities=("City A", "City D")),
            consent_reference="synthetic-consent-v2", consent_text="Research v2 only.",
            research_autostart=True,
        )
    assert first != second
    assert await state.pool.fetchval("SELECT version FROM owner_research_automation_profiles "
                                     "WHERE id=$1", second) == 2
    case, _, _ = await intake_case(state)
    await prepare(state, case)
    for table in ("case_research_policy_snapshots", "research_policy_authorizations"):
        with pytest.raises(asyncpg.PostgresError, match="immutable"):
            await state.pool.execute(f"DELETE FROM {table}")  # noqa: S608


async def test_atomic_start_rollback_and_recovery_after_job_insert_failure(owner_pg):
    state = owner_pg
    await enroll(state)
    case, _, _ = await intake_case(state)
    await state.pool.execute(
        """CREATE FUNCTION reject_execute_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.kind='execute_research' THEN RAISE EXCEPTION 'synthetic enqueue failpoint';
        END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_execute_job BEFORE INSERT ON jobs FOR EACH ROW
        EXECUTE FUNCTION reject_execute_job()""")
    with pytest.raises(asyncpg.PostgresError, match="synthetic enqueue failpoint"):
        await prepare(state, case)
    assert await state.pool.fetchval("SELECT count(*) FROM research_runs") == 0
    assert await state.pool.fetchval("SELECT count(*) FROM action_approvals") == 0
    assert await state.pool.fetchval("SELECT count(*) FROM case_pipeline_runtime") == 0
    assert await state.pool.fetchval("SELECT status FROM procurement_cases") == "ready"
    await state.pool.execute("DROP TRIGGER reject_execute_job ON jobs")
    await prepare(state, case)
    assert await state.pool.fetchval("SELECT count(*) FROM research_runs") == 1


async def test_snapshot_supports_multiple_exact_native_run_bindings(owner_pg):
    state = owner_pg
    await enroll(state)
    case, _, _ = await intake_case(state)
    await prepare(state, case)
    snapshot = await state.pool.fetchrow(
        "SELECT id FROM case_research_policy_snapshots WHERE case_id=$1", case)
    original = await state.pool.fetchrow(
        """SELECT research_run_id,plan_sha256 FROM research_policy_authorizations
        WHERE snapshot_id=$1""", snapshot["id"],
    )
    scope = await state.pool.fetchval(
        "SELECT search_scope FROM research_runs WHERE id=$1", original["research_run_id"])
    second_run = uuid4()
    await state.pool.execute(
        """INSERT INTO research_runs(id,case_id,status,requested_by,search_scope,idempotency_key)
        VALUES($1,$2,'planned',$3,$4,$5)""", second_run, case, state.owner, scope,
        "synthetic-second-run-for-same-snapshot",
    )
    async with state.pool.acquire() as c:
        await bind_research_policy(
            c, snapshot_id=snapshot["id"], case_id=case, run_id=second_run,
            plan_sha256=original["plan_sha256"],
        )
        assert await c.fetchval(
            "SELECT count(*) FROM research_policy_authorizations WHERE snapshot_id=$1",
            snapshot["id"],
        ) == 2
        with pytest.raises(ResearchPolicyDenied):
            await bind_research_policy(
                c, snapshot_id=snapshot["id"], case_id=case, run_id=second_run,
                plan_sha256="b" * 64,
            )
        other_case, _, _ = await intake_case(state, request=request_facts(city="City A"))
        other_run = uuid4()
        await c.execute(
            """INSERT INTO research_runs(
            id,case_id,status,requested_by,search_scope,idempotency_key)
            VALUES($1,$2,'planned',$3,$4,$5)""", other_run, other_case, state.owner, scope,
            "synthetic-run-for-another-case",
        )
        with pytest.raises(ResearchPolicyDenied, match="does not belong"):
            await bind_research_policy(
                c, snapshot_id=snapshot["id"], case_id=case, run_id=other_run,
                plan_sha256=original["plan_sha256"],
            )


async def test_operator_attestation_records_external_source_without_telegram_artifact(owner_pg):
    state = owner_pg
    case, _, _ = await intake_case(state, request=request_facts(city="City A"))
    item = await state.pool.fetchval("SELECT id FROM request_items WHERE line_number=1")
    kwargs = dict(
        owner_id=state.owner, case_id=case, item_id=item, artifact_id=None,
        artifact_sha256="c" * 64, source_kind="operator_attestation",
        source_reference="owner-upload:reviewed-photo-2026-10-16",
        disposition="skip", quantity=None, unit="m",
        owner_assertion="I confirm the exact requested brush line was already bought.",
        exact_match_rationale=(
            "Owner reviewed the source rows and selected this exact item mapping."
        ),
        idempotency_key="synthetic-operator-attestation",
    )
    async with state.pool.acquire() as c:
        evidence_id = await record_sourcing_disposition(c, **kwargs)
        assert await record_sourcing_disposition(c, **kwargs) == evidence_id
        for override in ({"owner_id": state.other},
                         {"source_reference": " "},
                         {"artifact_sha256": "not-a-digest"},
                         {"artifact_id": uuid4()},
                         {"owner_assertion": "changed assertion"}):
            with pytest.raises(ResearchPolicyDenied):
                await record_sourcing_disposition(c, **{**kwargs, **override})
        row = await c.fetchrow(
            "SELECT source_kind,source_artifact_id,source_reference,artifact_sha256 "
            "FROM item_sourcing_dispositions WHERE id=$1", evidence_id)
        assert row["source_kind"] == "operator_attestation"
        assert row["source_artifact_id"] is None
        assert row["source_reference"] == kwargs["source_reference"]
        assert row["artifact_sha256"] == kwargs["artifact_sha256"]
        with pytest.raises(
            asyncpg.PostgresError, match="operator source attestation is incomplete",
        ):
            await c.execute(
                """INSERT INTO item_sourcing_dispositions(
                case_id,request_item_id,owner_user_id,source_kind,source_reference,
                artifact_sha256,disposition,unit,owner_assertion,exact_match_rationale,
                evidence_sha256,idempotency_key)
                VALUES($1,$2,$3,'operator_attestation',NULL,$4,'skip','m','attested',
                'exact mapping',$5,'partial-provenance')""",
                case, item, state.owner, "d" * 64, "e" * 64,
            )
        with pytest.raises(asyncpg.PostgresError, match="item owner/case mismatch"):
            await c.execute(
                """INSERT INTO item_sourcing_dispositions(
                case_id,request_item_id,owner_user_id,source_kind,source_reference,
                artifact_sha256,disposition,unit,owner_assertion,exact_match_rationale,
                evidence_sha256,idempotency_key)
                VALUES($1,$2,$3,'operator_attestation',$4,$5,'skip','m','attested',
                'exact mapping',$6,'wrong-owner-provenance')""",
                case, item, state.other, kwargs["source_reference"], "d" * 64, "e" * 64,
            )
        assert await c.fetchval("SELECT count(*) FROM purchase_items") == 0
        assert await c.fetchval("SELECT status FROM request_items WHERE id=$1", item) == "ready"
    async with mini_client(state) as client:
        path = f"/api/cases/{case}/items/{item}/sourcing-dispositions"
        body = {**kwargs, "owner_id": None, "item_id": str(item), "case_id": str(case),
                "source_artifact_id": None, "source_sha256": kwargs["artifact_sha256"],
                "quantity": None}
        body.pop("artifact_sha256")
        assert (await client.post(path, json=body)).status == 401
        wrong_owner = await client.post(path, json=body, headers=signed_headers(800_000_001))
        assert wrong_owner.status == 409
        response = await client.post(path, json=body, headers=signed_headers())
        assert response.status == 201
        visibility = await client.get(f"/api/cases/{case}/source-visibility",
                                      headers=signed_headers())
        source_item = (await visibility.json())["items"][0]
        assert source_item["sourcing_evidence"][0]["source_kind"] == "operator_attestation"
        assert source_item["sourcing_evidence"][0]["source_reference"] == kwargs["source_reference"]
        assert source_item["sourcing_evidence"][0]["source_sha256"] == kwargs["artifact_sha256"]
        assert source_item["disposition_applied_to_plan"] is False


async def test_exact_owner_attested_disposition_is_evidence_only_not_accounting(owner_pg):
    state = owner_pg
    case, message, _ = await intake_case(state, request=request_facts(city="City A"))
    item = await state.pool.fetchval("SELECT id FROM request_items WHERE line_number=1")
    attachment = await state.pool.fetchval(
        "INSERT INTO incoming_attachments(message_id,external_file_id,kind) "
        "VALUES($1,'synthetic-photo','photo') RETURNING id", message,
    )
    artifact = await state.pool.fetchval(
        """INSERT INTO source_artifacts(
        attachment_id,artifact_kind,storage_path,sha256,byte_size)
        VALUES($1,'original','synthetic-not-a-real-file',$2,0) RETURNING id""",
        attachment, "a" * 64,
    )
    kwargs = dict(owner_id=state.owner, case_id=case, item_id=item, artifact_id=artifact,
                  artifact_sha256="a" * 64, disposition="reduce_quantity", quantity=Decimal("1"),
                  unit="m", owner_assertion="I bought exactly one metre of source line one.",
                  exact_match_rationale="Reviewed exact item and compatible metre unit.",
                  idempotency_key="synthetic-owner-disposition")
    async with state.pool.acquire() as c:
        first = await record_sourcing_disposition(c, **kwargs)
        assert await record_sourcing_disposition(c, **kwargs) == first
        for override in ({"owner_id": state.other}, {"artifact_sha256": "b" * 64},
                         {"unit": "piece"}, {"quantity": Decimal("3")},
                         {"owner_assertion": "Different assertion"}):
            with pytest.raises(ResearchPolicyDenied):
                await record_sourcing_disposition(c, **{**kwargs, **override})
        assert await c.fetchval("SELECT count(*) FROM purchase_items") == 0
        assert await c.fetchval("SELECT count(*) FROM purchase_documents") == 0
        assert await c.fetchval("SELECT status FROM request_items WHERE id=$1", item) == "ready"
    async with mini_client(state) as client:
        response = await client.get(
            f"/api/cases/{case}/source-visibility", headers=signed_headers(),
        )
        source_item = (await response.json())["items"][0]
        assert source_item["stage"] == "evidence_only"
        assert source_item["sourcing_evidence"][0]["artifact_sha256"] == "a" * 64
        assert source_item["disposition_applied_to_plan"] is False
