from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

import procurement_bot.whatsapp_delivery as whatsapp_delivery
from procurement_bot.approvals import (
    approve_request,
    request_approval,
    require_and_consume_approval,
)
from procurement_bot.auto_send_policy import set_case_auto_send_policy
from procurement_bot.autonomous_pipeline import (
    expire_waiting_slots,
    prepare_due_reminders,
    reacquire_queued_dialogue_slots,
    reconcile_transport,
)
from procurement_bot.chat_actors import ChatActorBusy, assign_supplier_dialogue
from procurement_bot.db import run_migrations
from procurement_bot.delivery_receipts import (
    DeliveryReceipt,
    advance_ack,
    parse_receipt,
    project_message_receipts,
    record_receipt,
)
from procurement_bot.errors import ValidationBlocked
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.outbox import WhatsAppOutboxWorker
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.pipeline_metrics import load_pipeline_metrics
from procurement_bot.pipeline_runtime import (
    ProviderQuotaDeferred,
    before_provider_call,
    provider_succeeded,
    schedule_due_pipelines,
    sleep_for_quota,
    wake_conversation_pipeline,
    wake_pipeline,
    wake_session_pipelines,
)
from procurement_bot.providers.waha import WahaSendResult
from procurement_bot.queue import ClaimedOutbox, claim_job, defer_job
from procurement_bot.research import build_research_plan
from procurement_bot.research_repository import mark_research_running, persist_research_plan
from procurement_bot.supplier_dialogue import WhatsAppSendDeferred
from procurement_bot.whatsapp_account import (
    configure_account_budget,
    get_account_state,
    request_account_probe,
    reserve_outbound,
)
from procurement_bot.whatsapp_delivery import (
    WhatsAppDeliveryContext,
    WhatsAppDeliveryPayload,
    complete_whatsapp_delivery,
)
from procurement_bot.whatsapp_send_journal import payload_sha256 as journal_payload_sha256


@pytest.mark.parametrize(
    ("before", "incoming", "expected"),
    [
        (None, 0, 0),
        (None, -1, -1),
        (0, -1, -1),
        (-1, 2, 2),
        (2, 1, 2),
        (3, -1, 3),
        (4, 2, 4),
        (1, 3, 3),
    ],
)
def test_receipt_projection_is_monotonic(before, incoming, expected):
    assert advance_ack(before, incoming) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "x", "fromMe": False, "ack": 2},
        {"id": "x", "fromMe": True, "ack": True},
        {"id": "x", "fromMe": True, "ack": 5},
        {"id": "x", "fromMe": True, "ack": 2, "ackName": "READ"},
        {"id": "", "fromMe": True, "ack": 2},
    ],
)
def test_receipt_rejects_wrong_direction_and_invalid_status(payload):
    with pytest.raises(ValueError):
        parse_receipt({"event": "message.ack", "session": "test", "payload": payload})


@pytest.fixture
async def pipeline_db():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_pipeline_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init(c):
        for typ in ("json", "jsonb"):
            await c.set_type_codec(
                typ, schema="pg_catalog", encoder=json.dumps, decoder=json.loads, format="text"
            )

    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=6, server_settings={"search_path": schema}, init=init
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner, case = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES($1,12345,'test')", owner
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES($1,$2,'Test','Алматы','researching')",
            case,
            owner,
        )
        items = []
        for n in range(1, 5):
            items.append(
                await pool.fetchval(
                    "INSERT INTO request_items(case_id,line_number,name,quantity,unit,status) "
                    "VALUES($1,$2,'Марля',10,'рулон','ready') RETURNING id",
                    case,
                    n,
                )
            )
        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Алматы",
                search_scope=SearchScope.CITYWIDE,
                items=[ProcurementItem(name="Марля", quantity=10, unit="рулон") for _ in items],
            )
        )
        async with pool.acquire() as c:
            run = await persist_research_plan(c, case_id=case, plan=plan, requested_by=owner)
            await mark_research_running(c, run.id)
        executor = ParallelResearchRunExecutor(pool, object())
        await executor.execute(run.id)
        await pool.execute(
            "INSERT INTO case_pipeline_runtime(case_id,research_run_id,enabled) VALUES($1,$2,true)",
            case,
            run.id,
        )
        yield pool, case, owner, run.id, executor
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
async def test_durable_timer_quota_probe_wake_and_generation_preserve_progress(pipeline_db):
    pool, case, owner, run, executor = pipeline_db
    await pool.execute("UPDATE jobs SET status='dead' WHERE kind='parallel_research_task'")
    await pool.execute(
        "UPDATE research_item_groups SET status='partial' WHERE research_run_id=$1", run
    )
    await pool.execute(
        "UPDATE research_runs SET status='partial',completed_at=now() WHERE id=$1", run
    )
    await pool.execute("UPDATE procurement_cases SET status='report_ready' WHERE id=$1", case)
    await pool.execute(
        "UPDATE research_branch_checkpoints SET status='failed',"
        "stop_reason='provider_failure:unsafe_payload' WHERE research_run_id=$1",
        run,
    )
    task = await pool.fetchrow(
        "SELECT cp.* FROM research_branch_checkpoints cp JOIN research_queries q "
        "ON q.id=cp.query_id WHERE cp.research_run_id=$1 AND q.provider='web' "
        "ORDER BY cp.id LIMIT 1",
        run,
    )
    historic_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,idempotency_key,status) "
        "VALUES('parallel_research_task',$1,$2,'dead') RETURNING id",
        {"research_run_id": str(run), "task_id": str(task["id"])},
        str(uuid4()),
    )
    await pool.execute(
        "UPDATE research_branch_checkpoints SET current_job_id=$2 WHERE id=$1",
        task["id"],
        historic_job,
    )
    cursor = {"current": "https://example.test/page2", "seen": ["https://example.test/page2"]}
    await pool.execute(
        "UPDATE research_branch_checkpoints SET current_batch_no=4,batch_attempts=2,"
        "search_cursor_state=$2,excluded_external_ids='[\"kept\"]',"
        "candidate_receipts='[{\"kept\":true}]',pages_examined=5,candidates_persisted=4,"
        "last_error_code='operator_retry_pending',"
        "stop_reason='provider_failure:RetryableProviderError:cli_usage_limit' WHERE id=$1",
        task["id"],
        cursor,
    )
    assert await executor.recover_due_branches(run) == 1
    after = await pool.fetchrow("SELECT * FROM research_branch_checkpoints WHERE id=$1", task["id"])
    assert after["current_batch_no"] == 4 and after["batch_attempts"] == 2
    assert after["recovery_base_attempts"] == 2 and after["recovery_cycles"] == 1
    assert after["search_cursor_state"] == cursor and after["excluded_external_ids"] == ["kept"]
    assert after["candidate_receipts"] == [{"kept": True}] and after["pages_examined"] == 5
    assert (
        after["candidates_persisted"] == 4 and after["last_error_code"] == "operator_retry_pending"
    )
    assert await executor.recover_due_branches(run) == 0
    assert await pool.fetchval("SELECT status FROM jobs WHERE id=$1", historic_job) == "dead"

    async with pool.acquire() as c, c.transaction():
        await before_provider_call(c, run)
    async with pool.acquire() as c, c.transaction():
        with pytest.raises(ProviderQuotaDeferred):
            await before_provider_call(c, run)
    await pool.execute("UPDATE jobs SET available_at=now() WHERE id=$1", after["current_job_id"])
    job = await claim_job(pool, worker_id="quota-test", excluded_kinds=("pipeline_tick",))
    assert job.id == after["current_job_id"]
    await pool.execute(
        "UPDATE research_branch_checkpoints SET status='researching',owner_worker=$2,"
        "batch_attempts=batch_attempts+1,lease_until=now()+interval '5 minutes' "
        "WHERE id=$1",
        task["id"],
        "quota-test",
    )
    async with pool.acquire() as c, c.transaction():
        due = await sleep_for_quota(
            c,
            run_id=run,
            task_id=task["id"],
            job_id=job.id,
            worker_id="quota-test",
            generation=after["owner_generation"],
        )
    assert 1790 < (due - datetime.now(UTC)).total_seconds() <= 1800
    await defer_job(pool, job, available_at=due, reason="provider_quota_sleep")
    assert await pool.fetchval("SELECT attempts FROM jobs WHERE id=$1", job.id) == 0
    assert (
        await pool.fetchval(
            "SELECT batch_attempts FROM research_branch_checkpoints WHERE id=$1", task["id"]
        )
        == 2
    )
    async with pool.acquire() as c:
        assert not await wake_pipeline(c, case_id=case, owner_id=uuid4(), reason="external")
        assert await wake_pipeline(c, case_id=case, owner_id=owner, reason="external")
    assert await pool.fetchval("SELECT available_at<=now() FROM jobs WHERE id=$1", job.id)
    # Exactly one scheduler generation wins across competing workers.
    results = await asyncio.gather(*(schedule_due_pipelines(pool) for _ in range(3)))
    assert sum(results) == 1
    assert await schedule_due_pipelines(pool) == 0
    async with pool.acquire() as c, c.transaction():
        await provider_succeeded(c, run)
    assert await pool.fetchval("SELECT count(*) FROM research_provider_waits") == 0
    await pool.execute("UPDATE case_pipeline_runtime SET paused=true,next_tick_at=now()")
    await pool.execute("UPDATE jobs SET status='succeeded' WHERE kind='pipeline_tick'")
    assert await schedule_due_pipelines(pool) == 0


@pytest.mark.postgres
async def test_inbound_and_session_hints_wake_only_enabled_unpaused_cases(pipeline_db):
    pool, case, _owner, run, _executor = pipeline_db
    conversation_id, *_ = await seed_chat(pool, case, run, 1)
    future = datetime.now(UTC) + timedelta(hours=2)
    await pool.execute(
        "UPDATE case_pipeline_runtime SET next_tick_at=$2,paused=false WHERE case_id=$1",
        case,
        future,
    )

    async with pool.acquire() as connection:
        assert await wake_conversation_pipeline(
            connection,
            conversation_id=conversation_id,
            reason="whatsapp_inbound_received",
        )
    runtime = await pool.fetchrow(
        "SELECT next_tick_at,paused,wake_reason FROM case_pipeline_runtime WHERE case_id=$1",
        case,
    )
    assert runtime["next_tick_at"] <= datetime.now(UTC)
    assert runtime["paused"] is False
    assert runtime["wake_reason"] == "whatsapp_inbound_received"

    await pool.execute(
        "UPDATE case_pipeline_runtime SET next_tick_at=$2,paused=true WHERE case_id=$1",
        case,
        future,
    )
    async with pool.acquire() as connection:
        assert (
            await wake_session_pipelines(
                connection,
                session_name="test",
                reason="whatsapp_session_status_hint",
            )
            == 0
        )
        assert not await wake_conversation_pipeline(
            connection,
            conversation_id=conversation_id,
            reason="whatsapp_inbound_received",
        )
    runtime = await pool.fetchrow(
        "SELECT next_tick_at,paused FROM case_pipeline_runtime WHERE case_id=$1", case
    )
    assert runtime["next_tick_at"] > datetime.now(UTC)
    assert runtime["paused"] is True

    await pool.execute(
        "UPDATE case_pipeline_runtime SET next_tick_at=$2,paused=false WHERE case_id=$1",
        case,
        future,
    )
    async with pool.acquire() as connection:
        assert (
            await wake_session_pipelines(
                connection,
                session_name="test",
                reason="whatsapp_session_status_hint",
            )
            == 1
        )
    runtime = await pool.fetchrow(
        "SELECT next_tick_at,paused,wake_reason FROM case_pipeline_runtime WHERE case_id=$1",
        case,
    )
    assert runtime["next_tick_at"] <= datetime.now(UTC)
    assert runtime["paused"] is False
    assert runtime["wake_reason"] == "whatsapp_session_status_hint"


async def seed_chat(pool, case, run, n):
    supplier = await pool.fetchval(
        "INSERT INTO suppliers(display_name) VALUES('test') RETURNING id"
    )
    query = await pool.fetchval(
        "SELECT id FROM research_queries WHERE research_run_id=$1 LIMIT 1", run
    )
    evidence = await pool.fetchval(
        "INSERT INTO source_evidence(research_query_id,source_type,source_locator,captured_at,"
        "content_sha256,raw_payload,idempotency_key) VALUES($1,'web',$2,now(),$3,'{}',$2) "
        "RETURNING id",
        query,
        str(uuid4()),
        "a" * 64,
    )
    phone = f"+77012340{n:03d}"
    contact = await pool.fetchval(
        "INSERT INTO supplier_contacts(supplier_id,contact_type,display_value,normalized_value,"
        "source_evidence_id,is_whatsapp_confirmed) VALUES($1,'whatsapp',$2,$2,$3,true) "
        "RETURNING id",
        supplier,
        phone,
        evidence,
    )
    chat = await pool.fetchval(
        "INSERT INTO supplier_conversations(case_id,supplier_id,supplier_contact_id,session_name,"
        "external_chat_id,state,idempotency_key,last_outbound_at) VALUES($1,$2,$3,'test',$4,"
        "'waiting_availability',$5,now()-interval '25 hours') RETURNING id",
        case,
        supplier,
        contact,
        phone[1:] + "@c.us",
        str(uuid4()),
    )
    group = await pool.fetchrow(
        "SELECT id,request_item_id FROM research_item_groups "
        "WHERE research_run_id=$1 ORDER BY request_item_id OFFSET $2 LIMIT 1",
        run,
        n - 1,
    )
    lane = await pool.fetchval(
        "SELECT id FROM research_lanes WHERE group_id=$1 ORDER BY lane_no LIMIT 1", group["id"]
    )
    offer = await pool.fetchval(
        "INSERT INTO offers(request_item_id,supplier_id,"
        "idempotency_key) VALUES($1,$2,$3) RETURNING id",
        group["request_item_id"],
        supplier,
        str(uuid4()),
    )
    target = await pool.fetchval(
        "INSERT INTO supplier_conversation_targets(conversation_id,case_id,"
        "supplier_id,request_item_id,"
        "offer_id,provenance_kind,provenance_ref,idempotency_key) "
        "VALUES($1,$2,$3,$4,$5,'research',$6,$6) RETURNING id",
        chat,
        case,
        supplier,
        group["request_item_id"],
        offer,
        str(uuid4()),
    )
    await pool.execute(
        "INSERT INTO supplier_conversation_item_turns(conversation_id,target_id,"
        "turn_no,status,draft_text) VALUES($1,$2,1,'pending_reply',"
        "'Есть ли Марля в наличии?')",
        chat,
        target,
    )
    return chat, supplier, group["id"], lane, group["request_item_id"]


@pytest.mark.postgres
async def test_account_budget_and_probe_wake_only_enabled_unpaused_session(pipeline_db):
    pool, case, owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    await pool.execute(
        "UPDATE supplier_conversations SET session_name='budget-session' WHERE id=$1", chat
    )
    await pool.execute(
        "UPDATE case_pipeline_runtime SET next_tick_at=now()+interval '1 hour' "
        "WHERE case_id=$1",
        case,
    )

    async with pool.acquire() as connection:
        await configure_account_budget(
            connection,
            "budget-session",
            daily_message_limit=12,
            daily_new_contact_limit=0,
            minimum_interval_seconds=120,
            max_concurrent_posts=1,
            reason="wake after budget update",
            actor_user_id=owner,
        )
        account = await get_account_state(connection, "budget-session")
    runtime = await pool.fetchrow(
        "SELECT enabled,paused,next_tick_at,wake_reason FROM case_pipeline_runtime "
        "WHERE case_id=$1",
        case,
    )
    assert runtime["enabled"] is True
    assert runtime["paused"] is False
    assert runtime["next_tick_at"] <= datetime.now(UTC)
    assert runtime["wake_reason"] == "whatsapp_account_probe:account_budget_updated"
    assert account["operator_held"] is True
    assert account["daily_message_limit"] == 12
    assert account["probe_request_reason"] == "account_budget_updated"

    await pool.execute(
        "UPDATE case_pipeline_runtime SET paused=true,next_tick_at=now()+interval '1 hour' "
        "WHERE case_id=$1",
        case,
    )
    async with pool.acquire() as connection:
        await request_account_probe(
            connection,
            "budget-session",
            reason="operator_requested_probe",
            actor_user_id=owner,
        )
    runtime = await pool.fetchrow(
        "SELECT paused,next_tick_at FROM case_pipeline_runtime WHERE case_id=$1", case
    )
    assert runtime["paused"] is True
    assert runtime["next_tick_at"] > datetime.now(UTC)

    await pool.execute(
        "UPDATE case_pipeline_runtime SET enabled=false,paused=false,"
        "next_tick_at=now()+interval '1 hour' WHERE case_id=$1",
        case,
    )
    async with pool.acquire() as connection:
        await request_account_probe(
            connection,
            "budget-session",
            reason="disabled_pipeline_probe",
            actor_user_id=owner,
        )
    runtime = await pool.fetchrow(
        "SELECT enabled,paused,next_tick_at FROM case_pipeline_runtime WHERE case_id=$1", case
    )
    assert runtime["enabled"] is False
    assert runtime["paused"] is False
    assert runtime["next_tick_at"] > datetime.now(UTC)


@pytest.mark.postgres
async def test_receipt_race_replay_deadlines_reminders_rotation_and_late_slot(pipeline_db):
    pool, case, owner, run, _executor = pipeline_db
    chats = [await seed_chat(pool, case, run, n) for n in range(1, 5)]
    for chat, supplier, group, lane, item in chats[:3]:
        async with pool.acquire() as c:
            await assign_supplier_dialogue(
                c,
                group_id=group,
                lane_id=lane,
                item_id=item,
                supplier_id=supplier,
                conversation_id=chat,
            )
    async with pool.acquire() as c:
        with pytest.raises(ChatActorBusy):
            chat, supplier, group, lane, item = chats[3]
            await assign_supplier_dialogue(
                c,
                group_id=group,
                lane_id=lane,
                item_id=item,
                supplier_id=supplier,
                conversation_id=chat,
            )
        assert await prepare_due_reminders(c, case) == 0
    chat = chats[0][0]
    receipt = DeliveryReceipt("test", "outbound-1", 3, datetime.now(UTC))
    async with pool.acquire() as c:
        assert not await record_receipt(c, receipt)
    async with pool.acquire() as c, c.transaction():
        await set_case_auto_send_policy(
            c,
            case_id=case,
            owner_user_id=owner,
            enabled=True,
            consent_reference="test-consent",
            consent_text="Test consent",
        )
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload={"case_id": str(case)},
        )
    event = await pool.fetchval(
        "INSERT INTO outbox_events(event_type,payload,idempotency_key,status,"
        "external_id,sent_at,approval_id) VALUES('whatsapp.send_text','{}',$1,'sent',"
        "'outbound-1',now(),$2) RETURNING id",
        str(uuid4()),
        approval,
    )
    message = await pool.fetchval(
        "INSERT INTO supplier_conversation_messages(conversation_id,outbox_event_id,direction,"
        "session_name,external_chat_id,external_message_id,raw_payload,payload_sha256,received_at) "
        "SELECT id,$2,'outbound',session_name,external_chat_id,'outbound-1','{}',$3,"
        "last_outbound_at "
        "FROM supplier_conversations WHERE id=$1 RETURNING id",
        chat,
        event,
        "b" * 64,
    )
    async with pool.acquire() as c, c.transaction():
        await project_message_receipts(c, message)
        assert await record_receipt(c, receipt)
        assert await record_receipt(c, DeliveryReceipt("test", "outbound-1", 1, datetime.now(UTC)))
    assert (
        await pool.fetchval("SELECT ack FROM whatsapp_delivery_states WHERE message_id=$1", message)
        == 3
    )
    # A supplier's needs_time reply arrives after the outbound message. That
    # inbound must still leave a due reminder eligible after its 24h promise.
    await pool.execute(
        "UPDATE supplier_conversations SET last_inbound_at=now(),"
        "reply_due_at=now()-interval '1 minute' "
        "WHERE id=$1",
        chat,
    )
    await pool.execute(
        "UPDATE supplier_conversation_item_turns SET status='needs_time',"
        "next_check_at=now()-interval '1 minute' WHERE conversation_id=$1",
        chat,
    )
    async with pool.acquire() as c, c.transaction():
        assert await expire_waiting_slots(c, case) == 3
        assert await prepare_due_reminders(c, case) == 1
        assert await prepare_due_reminders(c, case) == 0
        # A queued, already authorized reminder reacquires its released group
        # before any new outreach can consume the freed capacity.
        assert await reacquire_queued_dialogue_slots(c, case) == 1
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_conversation_item_turns WHERE status='pending_reply'"
        )
        == 3
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_conversation_item_turns WHERE status='needs_time'"
        )
        == 1
    )
    from procurement_bot.whatsapp_inbound import (
        AuthenticatedWhatsAppInbound,
        accept_whatsapp_inbound,
    )

    async with pool.acquire() as c:
        late = AuthenticatedWhatsAppInbound(
            provider_event_id="late-1",
            session_name="test",
            sender_phone="+77000000004",
            provider_message_id="late-1",
            received_at=datetime.now(UTC),
            text="Есть в наличии",
        )
        received = await accept_whatsapp_inbound(c, late, worker_id="test-late")
        assert received.conversation_id == chats[0][0] and received.message_id is not None
        again = await accept_whatsapp_inbound(c, late, worker_id="test-late")
        assert again.message_id == received.message_id
    async with pool.acquire() as c:
        chat4, supplier, group, lane, item = chats[3]
        await assign_supplier_dialogue(
            c,
            group_id=group,
            lane_id=lane,
            item_id=item,
            supplier_id=supplier,
            conversation_id=chat4,
        )
        # Reclaim the original assignment, retaining lane and physical chat.
        chat1, supplier, group, lane, item = chats[0]
        await assign_supplier_dialogue(
            c,
            group_id=group,
            lane_id=lane,
            item_id=item,
            supplier_id=supplier,
            conversation_id=chat1,
        )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_dialogue_assignments WHERE status='assigned'"
        )
        == 2
    )
    async with pool.acquire() as c:
        metrics = await load_pipeline_metrics(c, case)
    assert metrics["accepted"] == metrics["delivered"] == metrics["read"] == 1
    assert metrics["waiting_chats"] == 4


@pytest.mark.postgres
async def test_unknown_provider_acceptance_waits_a_day_without_closing_turns(pipeline_db):
    pool, case, owner, _run, _executor = pipeline_db
    chat, supplier, group, lane, item = await seed_chat(pool, case, _run, 1)
    async with pool.acquire() as c:
        await assign_supplier_dialogue(
            c,
            group_id=group,
            lane_id=lane,
            item_id=item,
            supplier_id=supplier,
            conversation_id=chat,
        )
    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload={"case_id": str(case), "conversation_id": str(chat)},
        )
    await pool.execute(
        """INSERT INTO outbox_events(event_type,payload,idempotency_key,status,
        last_error_code,approval_id,created_at) VALUES($1,$2::jsonb,$3,'retry',
        'provider_acceptance_unknown',$4,now()-interval '25 hours')""",
        "whatsapp.send_text",
        {"conversation_id": str(chat)},
        str(uuid4()),
        approval,
    )

    async with pool.acquire() as c, c.transaction():
        assert await expire_waiting_slots(c, case) == 1
        assert await reacquire_queued_dialogue_slots(c, case) == 0
    conversation = await pool.fetchrow(
        "SELECT waiting_reason,reply_due_at FROM supplier_conversations WHERE id=$1", chat
    )
    assert conversation["waiting_reason"] == "transport_unknown"
    assert conversation["reply_due_at"] < datetime.now(UTC)
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_dialogue_assignments "
            "WHERE conversation_id=$1 AND status='released'",
            chat,
        )
        == 1
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_conversation_item_turns "
            "WHERE conversation_id=$1 AND status='pending_reply'",
            chat,
        )
        == 1
    )


@pytest.mark.postgres
async def test_semantic_quota_recovery_preserves_original_reason_and_query_cursor(pipeline_db):
    pool, case, _owner, run, executor = pipeline_db
    await pool.execute("UPDATE jobs SET status='dead' WHERE kind='parallel_research_task'")
    await pool.execute(
        "UPDATE research_branch_checkpoints SET status='succeeded',stop_reason='source_exhausted'"
    )
    await pool.execute("UPDATE research_item_groups SET status='partial'")
    await pool.execute(
        "UPDATE research_runs SET status='partial',completed_at=now() WHERE id=$1", run
    )
    task = await pool.fetchrow(
        "SELECT cp.*,g.request_item_id FROM research_branch_checkpoints cp "
        "JOIN research_queries q ON q.id=cp.query_id "
        "JOIN research_item_groups g ON g.id=cp.group_id "
        "WHERE cp.research_run_id=$1 AND q.provider='web' LIMIT 1",
        run,
    )
    profile = await pool.fetchval(
        "INSERT INTO research_search_profiles(research_run_id,request_item_id,profile_version,"
        "snapshot_sha256,context_sha256,plan_sha256,model_version,prompt_version,"
        "profile,idempotency_key) "
        "VALUES($1,$2,1,$3,$3,$3,'test','test','{}',$4) RETURNING id",
        run,
        task["request_item_id"],
        "a" * 64,
        str(uuid4()),
    )
    old_cursor = {
        "current": "https://example.test/recover",
        "seen": ["https://example.test/recover"],
    }
    reason = "provider_failure:RetryableProviderError:cli_usage_limit"
    intent = await pool.fetchval(
        "INSERT INTO research_search_intents(profile_id,research_run_id,request_item_id,ordinal,"
        "provider,intent_type,category,query_text,rationale,uncertainty,scope_revision,intent_sha256,"
        "idempotency_key,stop_reason,cursor_state) "
        "VALUES($1,$2,$3,0,'web','product','test','test','test',0,1,$4,$5,$6,$7) RETURNING id",
        profile,
        run,
        task["request_item_id"],
        "b" * 64,
        str(uuid4()),
        reason,
        old_cursor,
    )
    last_cursor = {"current": "https://example.test/later", "seen": ["https://example.test/later"]}
    await pool.execute(
        "UPDATE research_branch_checkpoints SET semantic_intent_ordinal=1,"
        "search_cursor_state=$2,batch_attempts=2,stop_reason=$3 WHERE id=$1",
        task["id"],
        last_cursor,
        "source_exhausted;semantic_intent_failures:" + reason,
    )
    assert await executor.recover_due_branches(run) == 1
    cp = await pool.fetchrow("SELECT * FROM research_branch_checkpoints WHERE id=$1", task["id"])
    assert cp["semantic_intent_ordinal"] == 0 and cp["search_cursor_state"] == old_cursor
    recovered = await pool.fetchrow("SELECT * FROM research_search_intents WHERE id=$1", intent)
    assert recovered["stop_reason"] == reason and recovered["recovery_stop_reason"] == ""
    event = await pool.fetchval(
        "SELECT data FROM workflow_events WHERE event_type='research_branch_auto_recovery'"
    )
    assert event["previous_cursor"] == last_cursor and event["previous_intent_ordinal"] == 1
    await pool.execute(
        "UPDATE jobs SET status='running',locked_at=now(),locked_by='semantic-test',"
        "lease_expires_at=now()+interval '5 minutes' WHERE id=$1",
        cp["current_job_id"],
    )
    await pool.execute(
        "UPDATE research_branch_checkpoints SET status='researching',"
        "owner_worker='semantic-test',lease_until=now()+interval '5 minutes' WHERE id=$1",
        cp["id"],
    )
    await executor._persist_batch_result(
        run,
        cp["id"],
        cp["current_job_id"],
        "semantic-test",
        cp["owner_generation"],
        cp,
        candidates_persisted=0,
        own_database_candidates=0,
        pages_examined=1,
        external_ids=[],
        exhausted=True,
        stop_reason="source_exhausted",
        execution_intent_ordinal=0,
    )
    recovered = await pool.fetchrow("SELECT * FROM research_search_intents WHERE id=$1", intent)
    assert recovered["stop_reason"] == reason
    assert recovered["recovery_stop_reason"] == "source_exhausted"
    await pool.execute(
        "UPDATE research_branch_checkpoints SET recovery_due_at=now()-interval '1 minute'"
    )
    assert await executor.recover_due_branches(run) == 0


@pytest.mark.postgres
async def test_receipt_id_collision_fails_closed_and_error_is_not_hidden_by_pending(pipeline_db):
    pool, case, owner, run, _executor = pipeline_db
    chats = [await seed_chat(pool, case, run, n) for n in (1, 2)]
    message_ids = []
    for chat, *_ in chats:
        async with pool.acquire() as c, c.transaction():
            approval = await request_approval(
                c,
                case_id=case,
                action_type="send_whatsapp",
                requested_by=owner,
                payload={"case_id": str(case), "conversation_id": str(chat)},
            )
        outbox_id = await pool.fetchval(
            "INSERT INTO outbox_events(event_type,payload,idempotency_key,status,external_id,"
            "sent_at,approval_id) VALUES('whatsapp.send_text','{}',$1,'sent',"
            "'collision-id',now(),$2) "
            "RETURNING id",
            str(uuid4()),
            approval,
        )
        message_ids.append(
            await pool.fetchval(
                "INSERT INTO supplier_conversation_messages(conversation_id,outbox_event_id,"
                "direction,session_name,external_chat_id,external_message_id,raw_payload,"
                "payload_sha256,received_at) SELECT id,$2,'outbound',session_name,"
                "external_chat_id,'collision-id','{}',$3,now() FROM supplier_conversations "
                "WHERE id=$1 RETURNING id",
                chat,
                outbox_id,
                "c" * 64,
            )
        )

    async with pool.acquire() as c:
        assert not await record_receipt(
            c, DeliveryReceipt("test", "collision-id", 2, datetime.now(UTC))
        )
    for message_id in message_ids:
        async with pool.acquire() as c, c.transaction():
            await project_message_receipts(c, message_id)
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM whatsapp_delivery_states WHERE message_id=ANY($1)", message_ids
        )
        == 2
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM whatsapp_delivery_states "
            "WHERE message_id=ANY($1) AND ack IS NOT NULL",
            message_ids,
        )
        == 0
    )

    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload={
                "case_id": str(case),
                "conversation_id": str(chats[0][0]),
                "message": "ordered",
            },
        )
    unique_outbox = await pool.fetchval(
        "INSERT INTO outbox_events(event_type,payload,idempotency_key,status,external_id,sent_at,"
        "approval_id) VALUES('whatsapp.send_text','{}',$1,'sent','ordered-id',now(),$2) "
        "RETURNING id",
        str(uuid4()),
        approval,
    )
    unique_message = await pool.fetchval(
        "INSERT INTO supplier_conversation_messages(conversation_id,outbox_event_id,direction,"
        "session_name,external_chat_id,external_message_id,raw_payload,payload_sha256,received_at) "
        "SELECT id,$2,'outbound',session_name,external_chat_id,'ordered-id','{}',$3,now() "
        "FROM supplier_conversations WHERE id=$1 RETURNING id",
        chats[0][0],
        unique_outbox,
        "d" * 64,
    )
    async with pool.acquire() as c, c.transaction():
        await record_receipt(c, DeliveryReceipt("test", "ordered-id", 0, datetime.now(UTC)))
        await record_receipt(c, DeliveryReceipt("test", "ordered-id", -1, datetime.now(UTC)))
    assert (
        await pool.fetchval(
            "SELECT ack FROM whatsapp_delivery_states WHERE message_id=$1", unique_message
        )
        == -1
    )
    assert advance_ack(-1, 0) == -1


@pytest.mark.postgres
async def test_complete_whatsapp_delivery_persists_reply_deadline(pipeline_db):
    pool, case, owner, run, _executor = pipeline_db
    chat, _supplier, _group, _lane, _item = await seed_chat(pool, case, run, 1)
    text = "Здравствуйте! Есть ли товар в наличии?"
    action_id = "availability:test"
    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload={
                "case_id": str(case),
                "conversation_id": str(chat),
                "logical_action_id": action_id,
            },
        )
    event_id = uuid4()
    locked_by = "delivery-test"
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "text": text,
        "conversation_id": str(chat),
        "logical_action_id": action_id,
        "is_followup": False,
    }
    await pool.execute(
        "INSERT INTO outbox_events(id,event_type,payload,status,locked_at,locked_by,"
        "lease_expires_at,idempotency_key,approval_id) "
        "VALUES($1,'whatsapp.send_text',$2,'sending',now(),$3,now()+interval '5 minutes',$4,$5)",
        event_id,
        payload,
        locked_by,
        str(uuid4()),
        approval,
    )
    event = ClaimedOutbox(
        id=event_id,
        event_type="whatsapp.send_text",
        payload=payload,
        attempts=1,
        max_attempts=8,
        idempotency_key=str(uuid4()),
        locked_by=locked_by,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        approval_id=approval,
    )
    context = WhatsAppDeliveryContext(
        payload=WhatsAppDeliveryPayload(
            case_id=case,
            recipient="+77000000004",
            text=text,
            conversation_id=chat,
            logical_action_id=action_id,
            is_followup=False,
        ),
        session_name="test",
        external_chat_id="77000000004@c.us",
    )
    timestamp = datetime.now(UTC)
    assert await complete_whatsapp_delivery(
        pool,
        event,
        context,
        WahaSendResult(external_message_id="sent-live-test", raw={"id": "sent-live-test"}),
        received_at=timestamp,
    )
    due = await pool.fetchval("SELECT reply_due_at FROM supplier_conversations WHERE id=$1", chat)
    assert due == timestamp + timedelta(hours=24)
    assert await pool.fetchval(
        "SELECT ack IS NULL FROM whatsapp_delivery_states WHERE message_id=("
        "SELECT id FROM supplier_conversation_messages WHERE external_message_id='sent-live-test')"
    )


@pytest.mark.postgres
async def test_completed_send_records_truth_without_resurrecting_opt_out(pipeline_db):
    pool, case, owner, run, _executor = pipeline_db
    chat, _supplier, _group, _lane, _item = await seed_chat(pool, case, run, 1)
    await pool.execute(
        "UPDATE supplier_conversations SET state='opted_out',opted_out=true,"
        "opted_out_at=now(),opt_out_reason='supplier_request',"
        "waiting_reason='supplier_opted_out' WHERE id=$1",
        chat,
    )
    text = "Здравствуйте! Есть ли товар в наличии?"
    action_id = "initial-availability:optout-race"
    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload={
                "case_id": str(case),
                "conversation_id": str(chat),
                "logical_action_id": action_id,
            },
        )
    event_id = uuid4()
    locked_by = "delivery-optout-test"
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "text": text,
        "conversation_id": str(chat),
        "logical_action_id": action_id,
        "is_followup": False,
    }
    await pool.execute(
        "INSERT INTO outbox_events(id,event_type,payload,status,locked_at,locked_by,"
        "lease_expires_at,idempotency_key,approval_id) "
        "VALUES($1,'whatsapp.send_text',$2,'sending',now(),$3,now()+interval '5 minutes',$4,$5)",
        event_id,
        payload,
        locked_by,
        str(uuid4()),
        approval,
    )
    event = ClaimedOutbox(
        id=event_id,
        event_type="whatsapp.send_text",
        payload=payload,
        attempts=1,
        max_attempts=8,
        idempotency_key=str(uuid4()),
        locked_by=locked_by,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        approval_id=approval,
    )
    context = WhatsAppDeliveryContext(
        payload=WhatsAppDeliveryPayload(
            case_id=case,
            recipient="+77000000004",
            text=text,
            conversation_id=chat,
            logical_action_id=action_id,
            is_followup=False,
        ),
        session_name="test",
        external_chat_id="77000000004@c.us",
    )
    assert await complete_whatsapp_delivery(
        pool,
        event,
        context,
        WahaSendResult(external_message_id="sent-before-optout", raw={"id": "sent-before-optout"}),
        received_at=datetime.now(UTC),
    )
    state = await pool.fetchrow(
        "SELECT c.state,c.opted_out,c.waiting_reason,c.reply_due_at,c.outbound_count,"
        "(SELECT count(*) FROM supplier_conversation_messages m WHERE m.conversation_id=c.id "
        "AND m.direction='outbound') AS ledger_count "
        "FROM supplier_conversations c WHERE c.id=$1",
        chat,
    )
    assert state["state"] == "opted_out"
    assert state["opted_out"] is True
    assert state["waiting_reason"] == "supplier_opted_out"
    assert state["reply_due_at"] is None
    assert state["outbound_count"] == state["ledger_count"] == 1


@pytest.mark.postgres
@pytest.mark.parametrize("inbound_offset_seconds", [0, 5])
async def test_timed_reminder_is_blocked_after_supplier_reply(
    pipeline_db, monkeypatch, inbound_offset_seconds
):
    pool, case, owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    last_outbound = datetime.now(UTC) - timedelta(hours=25)
    last_inbound = last_outbound + timedelta(seconds=inbound_offset_seconds)
    await pool.execute(
        "UPDATE supplier_conversations SET last_outbound_at=$2,last_inbound_at=$3 WHERE id=$1",
        chat,
        last_outbound,
        last_inbound,
    )

    monkeypatch.setattr(
        whatsapp_delivery,
        "assert_auto_send_authorization_active",
        lambda *args, **kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        whatsapp_delivery,
        "get_case_send_policy",
        lambda *args, **kwargs: asyncio.sleep(0, result=whatsapp_delivery.DEFAULT_SEND_POLICY),
    )

    async def no_slot(*_args, **_kwargs):
        return None

    monkeypatch.setattr("procurement_bot.autonomous_pipeline.ensure_dialogue_slot", no_slot)
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "text": "Напоминаю наш вопрос: Есть ли Марля в наличии?",
        "conversation_id": str(chat),
        "logical_action_id": "timed-reminder:test-turn",
        "is_followup": True,
    }
    async with pool.acquire() as connection, connection.transaction():
        approval = await request_approval(
            connection,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload=payload,
        )
    event_id = uuid4()
    await pool.execute(
        "INSERT INTO outbox_events(id,event_type,payload,idempotency_key,status,approval_id) "
        "VALUES($1,'whatsapp.send_text',$2,$3,'pending',$4)",
        event_id,
        payload,
        str(uuid4()),
        approval,
    )
    event = ClaimedOutbox(
        id=event_id,
        event_type="whatsapp.send_text",
        payload=payload,
        attempts=0,
        max_attempts=8,
        idempotency_key=str(uuid4()),
        locked_by="reminder-guard-test",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        approval_id=approval,
    )

    with pytest.raises(ValidationBlocked, match="supplier replied before reminder"):
        await whatsapp_delivery.assert_whatsapp_delivery_allowed(pool, event)
    outbox = await pool.fetchrow(
        "SELECT status,provider_request_id FROM outbox_events WHERE id=$1", event_id
    )
    assert outbox["status"] == "pending"
    assert outbox["provider_request_id"] is None


@pytest.mark.postgres
@pytest.mark.parametrize("mutation", ["opt_out", "pause_case"])
async def test_final_account_reservation_rechecks_case_and_contact_state(
    pipeline_db, mutation
):
    pool, case, owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "text": "Здравствуйте! Есть ли Марля в наличии?",
        "conversation_id": str(chat),
        "logical_action_id": "availability-v1:final-gate",
        "is_followup": False,
    }
    async with pool.acquire() as connection, connection.transaction():
        approval = await request_approval(
            connection,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload=payload,
        )
        assert await approve_request(connection, approval_id=approval, approved_by=owner)
        await require_and_consume_approval(
            connection, case_id=case, action_type="send_whatsapp", payload=payload
        )

    if mutation == "opt_out":
        await pool.execute(
            "UPDATE supplier_conversations SET opted_out=TRUE,state='opted_out',"
            "opt_out_reason='owner_test',opted_out_at=now() WHERE id=$1",
            chat,
        )
    else:
        await pool.execute(
            "UPDATE case_pipeline_runtime SET paused=TRUE WHERE case_id=$1", case
        )

    event_id = uuid4()
    worker_id = "final-reservation-recheck"
    await pool.execute(
        "INSERT INTO outbox_events(id,event_type,payload,status,attempts,max_attempts,"
        "idempotency_key,locked_at,locked_by,lease_expires_at,approval_id) "
        "VALUES($1,'whatsapp.send_text',$2,'sending',1,8,$3,now(),$4,"
        "now()+interval '5 minutes',$5)",
        event_id,
        payload,
        f"final-gate:{event_id}",
        worker_id,
        approval,
    )
    event = ClaimedOutbox(
        id=event_id,
        event_type="whatsapp.send_text",
        payload=payload,
        attempts=1,
        max_attempts=8,
        idempotency_key=f"final-gate:{event_id}",
        locked_by=worker_id,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        approval_id=approval,
    )

    expected = ValidationBlocked if mutation == "opt_out" else WhatsAppSendDeferred
    with pytest.raises(expected):
        await reserve_outbound(
            pool,
            event,
            provider_request_id="must-not-post",
            worker_id=worker_id,
        )
    stored = await pool.fetchrow(
        "SELECT provider_request_id FROM outbox_events WHERE id=$1", event_id
    )
    assert stored["provider_request_id"] is None
    assert await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations "
        "WHERE outbox_event_id=$1)",
        event_id,
    ) is False


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("prior_status", "has_acceptance_journal"),
    [("dead", False), ("dead", True), ("sending", False)],
)
async def test_dead_unknown_reserved_send_reconciles_without_post(
    pipeline_db, prior_status, has_acceptance_journal
):
    pool, case, owner, run, _executor = pipeline_db
    chat, _supplier, _group, _lane, _item = await seed_chat(pool, case, run, 1)
    text = "Здравствуйте! Есть ли товар в наличии?"
    action_id = "initial-availability:crash-recovery"
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "text": text,
        "conversation_id": str(chat),
        "logical_action_id": action_id,
        "is_followup": False,
    }
    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload=payload,
        )
        assert await approve_request(c, approval_id=approval, approved_by=owner)
        await require_and_consume_approval(
            c, case_id=case, action_type="send_whatsapp", payload=payload
        )

    event_id, provider_id = uuid4(), "reserved-after-crash"
    idempotency_key = f"crashed-send:{event_id}"
    prior_error = "provider_outcome_unknown" if prior_status == "dead" else "RuntimeError"
    await pool.execute(
        "INSERT INTO outbox_events(id,event_type,payload,status,attempts,max_attempts,"
        "idempotency_key,external_id,last_error_code,approval_id,provider_request_id,"
        "locked_at,locked_by,lease_expires_at) "
        "VALUES($1,'whatsapp.send_text',$2,$3,8,8,$4,NULL,$5,$6,$7,"
        "CASE WHEN $3='sending' THEN now()-interval '2 minutes' END,"
        "CASE WHEN $3='sending' THEN 'crashed-worker' END,"
        "CASE WHEN $3='sending' THEN now()-interval '1 minute' END)",
        event_id,
        payload,
        prior_status,
        idempotency_key,
        prior_error,
        approval,
        provider_id,
    )
    expected_full_id = f"true_77000000004@c.us_{provider_id}"
    accepted_payload = {
        "id": expected_full_id,
        "fromMe": True,
        "chatId": "77000000004@c.us",
        "body": text,
    }
    if has_acceptance_journal:
        await pool.execute(
            "INSERT INTO whatsapp_send_acceptance_journal(outbox_event_id,idempotency_key,"
            "session_name,external_chat_id,provider_request_id,payload_sha256,external_message_id,"
            "source,result_identity,accepted_at) VALUES($1,$2,'test','77000000004@c.us',$3,$4,$5,"
            "'http',$6,now())",
            event_id,
            idempotency_key,
            provider_id,
            journal_payload_sha256(
                session_name="test",
                external_chat_id="77000000004@c.us",
                text=text,
                idempotency_key=idempotency_key,
                provider_request_id=provider_id,
            ),
            expected_full_id,
            {
                "external_message_id": expected_full_id,
                "accepted": True,
                "provider_result": accepted_payload,
            },
        )

    class ReconcileOnlyWaha:
        session_name = "test"

        def __init__(self):
            self.get_calls = []
            self.post_calls = []

        async def fetch_message(self, chat_id, message_id):
            self.get_calls.append((chat_id, message_id))
            return accepted_payload

        async def send_text(self, **kwargs):
            self.post_calls.append(kwargs)
            raise AssertionError("a reserved provider ID must never enter the POST path")

    client = ReconcileOnlyWaha()
    worker = WhatsAppOutboxWorker(pool=pool, client=client, worker_id="recovery-worker")
    assert await worker.run_once()
    assert client.post_calls == []
    assert client.get_calls == (
        [] if has_acceptance_journal else [("77000000004@c.us", expected_full_id)]
    )
    outcome = await pool.fetchrow(
        "SELECT status,attempts,provider_request_id,external_id FROM outbox_events WHERE id=$1",
        event_id,
    )
    assert outcome["status"] == "sent"
    assert outcome["attempts"] == 9
    assert outcome["provider_request_id"] == provider_id
    assert outcome["external_id"] == expected_full_id
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_conversation_messages WHERE outbox_event_id=$1 "
            "AND direction='outbound'",
            event_id,
        )
        == 1
    )
    evidence = await pool.fetchrow(
        "SELECT data FROM workflow_events WHERE idempotency_key=$1",
        f"waha-outcome-reconcile:{event_id}:{provider_id}:{prior_status}:8",
    )
    assert evidence is not None
    assert evidence["data"]["prior_status"] == prior_status
    assert evidence["data"]["prior_attempts"] == 8
    assert evidence["data"]["provider_request_id"] == provider_id
    assert evidence["data"]["prior_error_code"] == prior_error


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("error_code", "has_acceptance_journal"),
    [("ValueError", False), ("ValueError", True), ("PermanentProviderError", True)],
)
async def test_arbitrary_dead_reserved_send_is_not_retried(
    pipeline_db, error_code, has_acceptance_journal
):
    pool, case, owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "text": "Здравствуйте! Есть ли товар в наличии?",
        "conversation_id": str(chat),
        "logical_action_id": "invalid-payload-case",
        "is_followup": False,
    }
    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(
            c,
            case_id=case,
            action_type="send_whatsapp",
            requested_by=owner,
            payload=payload,
        )
        assert await approve_request(c, approval_id=approval, approved_by=owner)
        await require_and_consume_approval(
            c, case_id=case, action_type="send_whatsapp", payload=payload
        )
    event_id = await pool.fetchval(
        "INSERT INTO outbox_events(event_type,payload,status,attempts,max_attempts,"
        "idempotency_key,last_error_code,approval_id,provider_request_id) "
        "VALUES('whatsapp.send_text',$1,'dead',8,8,$2,$4,$3,'reserved-invalid') "
        "RETURNING id",
        payload,
        str(uuid4()),
        approval,
        error_code,
    )
    if has_acceptance_journal:
        await pool.execute(
            "INSERT INTO whatsapp_send_acceptance_journal(outbox_event_id,idempotency_key,"
            "session_name,external_chat_id,provider_request_id,payload_sha256,"
            "external_message_id,source,result_identity,accepted_at) "
            "VALUES($1,$2,'test','77000000004@c.us','reserved-invalid',$3,'true_accepted',"
            "'http',$4,now())",
            event_id,
            f"accepted:{event_id}",
            journal_payload_sha256(
                session_name="test",
                external_chat_id="77000000004@c.us",
                text=payload["text"],
                idempotency_key=f"accepted:{event_id}",
                provider_request_id="reserved-invalid",
            ),
            {"external_message_id": "true_accepted"},
        )
    from procurement_bot.chat_actors import claim_whatsapp_outbox

    assert await claim_whatsapp_outbox(pool, worker_id="recovery-worker") is None
    assert await pool.fetchval("SELECT status FROM outbox_events WHERE id=$1", event_id) == "dead"


@pytest.mark.asyncio
async def test_transport_reconciliation_waits_for_available_session(pipeline_db):
    pool, case, _owner, _run, _executor = pipeline_db

    class FailedSession:
        session_name = "test"

        async def get_session_status(self):
            return "FAILED"

        async def fetch_message(self, *_args):
            pytest.fail("message lookup must wait until the session is available")

        async def fetch_chat_messages(self, *_args, **_kwargs):
            pytest.fail("history lookup must wait until the session is available")

    summary = await reconcile_transport(pool, FailedSession(), case)
    assert summary["waha_status"] == "FAILED"
    assert summary["checked"] == summary["incoming"] == summary["receipts"] == 0


@pytest.mark.postgres
async def test_verified_contact_fix_requires_exact_runtime_and_preserves_exhausted_marker(
    pipeline_db,
):
    import hashlib

    from procurement_bot import research_execution

    pool, case, _owner, run, executor = pipeline_db
    await pool.execute("UPDATE jobs SET status='dead' WHERE kind='parallel_research_task'")
    await pool.execute(
        "UPDATE research_item_groups SET status='partial' WHERE research_run_id=$1", run
    )
    await pool.execute(
        "UPDATE research_branch_checkpoints SET status='failed',"
        "stop_reason='recovery_attempts_exhausted',last_error_code='recovery_attempts_exhausted',"
        "batch_attempts=2,current_batch_no=5,search_cursor_state='{\"kept\":true}',"
        "source_progress_token=repeat('a',64) WHERE research_run_id=$1",
        run,
    )
    tasks = await pool.fetch("SELECT id FROM research_branch_checkpoints ORDER BY id LIMIT 2")
    digest = hashlib.sha256(Path(research_execution.__file__).read_bytes()).hexdigest()
    for n, task in enumerate(tasks):
        await pool.execute(
            "INSERT INTO workflow_events(case_id,event_type,actor_type,actor_id,"
            "idempotency_key,data) "
            "VALUES($1,'research_contact_fix_verified','user','test',$2,$3)",
            case,
            str(uuid4()),
            {"checkpoint_id": str(task["id"]), "runtime_sha256": digest if n == 0 else "0" * 64},
        )
    assert await executor.recover_due_branches(run) == 1
    restored = await pool.fetchrow(
        "SELECT * FROM research_branch_checkpoints WHERE id=$1", tasks[0]["id"]
    )
    assert restored["status"] in {"queued", "researching"}
    assert restored["batch_attempts"] == restored["recovery_base_attempts"] == 2
    assert restored["current_batch_no"] == 5
    assert restored["stop_reason"] == restored["last_error_code"] == "recovery_attempts_exhausted"
    assert restored["search_cursor_state"] == {"kept": True}
    assert restored["source_progress_token"] == "a" * 64
    assert (
        await pool.fetchval(
            "SELECT status FROM research_branch_checkpoints WHERE id=$1", tasks[1]["id"]
        )
        == "failed"
    )
    assert await executor.recover_due_branches(run) == 0


@pytest.mark.postgres
async def test_contact_repair_authorization_checks_owner_and_original_dead_job(
    pipeline_db, monkeypatch
):
    from scripts import verify_uran_contact_recovery as repair

    pool, case, owner, run, _executor = pipeline_db
    task = await pool.fetchrow("SELECT * FROM research_branch_checkpoints LIMIT 1")
    historic_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,idempotency_key,status,last_error_code) "
        "VALUES('parallel_research_task',$1,$2,'dead','ValueError') RETURNING id",
        {"research_run_id": str(run), "task_id": str(task["id"])},
        str(uuid4()),
    )
    monkeypatch.setattr(repair, "CASE", case)
    monkeypatch.setattr(repair, "TARGETS", {task["id"]: ("ValueError", [historic_job])})
    async with pool.acquire() as c, c.transaction():
        with pytest.raises(ValueError, match="owned Uran"):
            await repair.authorize_repairs(c, owner_id=uuid4(), runtime_sha256="a" * 64)
    async with pool.acquire() as c, c.transaction():
        assert await repair.authorize_repairs(c, owner_id=owner, runtime_sha256="a" * 64) == 1
        assert await repair.authorize_repairs(c, owner_id=owner, runtime_sha256="a" * 64) == 1
    after = await pool.fetchrow("SELECT * FROM research_branch_checkpoints WHERE id=$1", task["id"])
    for field in (
        "current_batch_no",
        "batch_attempts",
        "search_cursor_state",
        "stop_reason",
        "last_error_code",
    ):
        assert after[field] == task[field]
    assert await pool.fetchval("SELECT status FROM jobs WHERE id=$1", historic_job) == "dead"
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM workflow_events WHERE event_type='research_contact_fix_verified'"
        )
        == 1
    )


@pytest.mark.postgres
async def test_outreach_coalesces_duplicates_preserving_attempts_and_changes(pipeline_db):
    from procurement_bot.outreach_schedule import (
        begin_outreach,
        coalesce_legacy_outreach,
        finish_outreach,
        request_outreach,
    )

    pool, case, *_ = pipeline_db
    old = []
    for _n in range(5):
        old.append(
            await pool.fetchval(
                "INSERT INTO jobs(kind,payload,idempotency_key,status,attempts,last_error_code) "
                "VALUES('prepare_supplier_outreach',$1,$2,'pending',3,'waha_session_unavailable') "
                "RETURNING id",
                {"case_id": str(case)},
                str(uuid4()),
            )
        )
    async with pool.acquire() as c:
        assert await coalesce_legacy_outreach(c, case) == 4
        assert await coalesce_legacy_outreach(c, case) == 0
        canonical = await request_outreach(c, case_id=case, source_key="new-offer")
        generation = await begin_outreach(c, case_id=case, job_id=canonical)
        assert await request_outreach(c, case_id=case, source_key="late-reply") == canonical
        await finish_outreach(c, case_id=case, job_id=canonical, generation=generation)
    assert await pool.fetchval("SELECT count(*) FROM outreach_coalesced_jobs") == 4
    assert await pool.fetchval("SELECT bool_and(attempts=3) FROM jobs WHERE id=ANY($1)", old)
    marker = await pool.fetchrow("SELECT * FROM case_outreach_requests WHERE case_id=$1", case)
    assert marker["current_job_id"] != canonical
    assert marker["dirty_generation"] > marker["prepared_generation"]


@pytest.mark.postgres
async def test_account_probe_sleeps_but_external_hint_wakes_without_releasing_hold(pipeline_db):
    from procurement_bot.transport_recovery import probe_account
    from procurement_bot.whatsapp_account import request_account_probe

    pool, *_ = pipeline_db

    class Client:
        session_name = "test"
        calls = 0

        async def get_account_restrictions(self):
            self.calls += 1
            return {"state": "clear"}

    client = Client()
    first = await probe_account(pool, client)
    assert first["operator_held"] and not first["outbound_allowed"]
    await probe_account(pool, client)
    assert client.calls == 1
    async with pool.acquire() as c:
        await request_account_probe(c, "test", reason="owner-wake")
    await probe_account(pool, client)
    assert client.calls == 2
    assert await pool.fetchval(
        "SELECT operator_held FROM whatsapp_account_controls WHERE session_name='test'"
    )


@pytest.mark.postgres
async def test_history_walk_uses_fixed_window_and_advances_offset(pipeline_db):
    from procurement_bot.transport_recovery import reconcile_history

    pool, case, _owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)

    class Client:
        session_name = "test"
        calls = []

        async def fetch_chat_messages(self, chat_id, **kwargs):
            self.calls.append(kwargs)
            if kwargs["offset"] == 0:
                return [{"id": f"own-{n}", "fromMe": True} for n in range(50)]
            return []

    client = Client()
    await reconcile_history(pool, client, case)
    assert (
        await pool.fetchval(
            "SELECT history_page_offset FROM supplier_conversations WHERE id=$1", chat
        )
        == 50
    )
    await reconcile_history(pool, client, case)
    assert [call["offset"] for call in client.calls] == [0, 50]
    assert client.calls[0]["timestamp_lte"] == client.calls[1]["timestamp_lte"]
    assert client.calls[0]["timestamp_gte"] == client.calls[1]["timestamp_gte"]
    assert (
        await pool.fetchval(
            "SELECT history_page_offset FROM supplier_conversations WHERE id=$1", chat
        )
        == 0
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM supplier_conversation_messages WHERE direction='inbound'"
        )
        == 0
    )


@pytest.mark.postgres
async def test_empty_history_pass_preserves_older_late_backfill_and_deduplicates(pipeline_db):
    from procurement_bot.transport_recovery import reconcile_history

    pool, case, _owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    created = datetime.now(UTC) - timedelta(days=2)
    reply_at = created + timedelta(hours=4)
    await pool.execute(
        "UPDATE supplier_conversations SET created_at=$2,"
        "history_window_start=now()-interval '5 minutes' WHERE id=$1",
        chat,
        created,
    )
    chat_id = await pool.fetchval(
        "SELECT external_chat_id FROM supplier_conversations WHERE id=$1", chat
    )

    class Client:
        session_name = "test"
        calls = []

        async def fetch_chat_messages(self, _chat_id, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return []
            if kwargs["timestamp_gte"] > int(reply_at.timestamp()):
                return []
            return [{
                "id": f"false_{chat_id}_late-backfill",
                "from": chat_id,
                "fromMe": False,
                "timestamp": int(reply_at.timestamp()),
                "body": "Здравствуйте, есть",
            }]

    client = Client()
    first = await reconcile_history(pool, client, case)
    assert first["history_empty"] == 1
    second = await reconcile_history(pool, client, case)
    assert second["incoming"] == 1
    third = await reconcile_history(pool, client, case)
    assert third["incoming"] == 0
    assert all(
        call["timestamp_gte"] == int((created - timedelta(minutes=5)).timestamp())
        for call in client.calls
    )
    assert await pool.fetchval(
        "SELECT count(*) FROM supplier_conversation_messages "
        "WHERE conversation_id=$1 AND direction='inbound'", chat
    ) == 1


@pytest.mark.postgres
async def test_tick_repairs_search_and_coalesces_while_outbound_stays_held(pipeline_db):
    from types import SimpleNamespace

    from procurement_bot.autonomous_pipeline import run_pipeline_tick

    pool, case, _owner, run, executor = pipeline_db

    class Client:
        session_name = "test"
        outbound_enabled = False

        async def get_account_restrictions(self):
            return {"state": "unavailable"}

        async def get_session_status(self):
            return "STOPPED"

    for _ in range(3):
        await pool.execute(
            "INSERT INTO jobs(kind,payload,idempotency_key,status) "
            "VALUES('prepare_supplier_outreach',$1,$2,'pending')",
            {"case_id": str(case)},
            str(uuid4()),
        )
    await run_pipeline_tick(
        SimpleNamespace(pool=pool, waha_client=Client(), research_executor=executor), case
    )
    summary = await pool.fetchval(
        "SELECT last_tick_summary FROM case_pipeline_runtime WHERE case_id=$1", case
    )
    assert summary["account"]["operator_held"]
    assert summary["account"]["outbound_allowed"] is False
    assert summary["coalesced_outreach_jobs"] == 2
    assert summary["reminders_prepared"] == 0
    assert summary["research_progress"]["run_status"] == "running"
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM jobs WHERE kind='prepare_supplier_outreach' "
            "AND status IN ('pending','retry','running')"
        )
        == 1
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE event_type='whatsapp.send_text'"
        )
        == 0
    )


@pytest.mark.postgres
async def test_history_does_not_advance_when_private_quarantine_is_full(pipeline_db, monkeypatch):
    from procurement_bot.transport_recovery import reconcile_history

    pool, case, _owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)

    async def full(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        "procurement_bot.transport_recovery.persist_authenticated_lid_quarantine", full
    )

    class Client:
        session_name = "test"

        async def fetch_chat_messages(self, *_args, **_kwargs):
            return [
                {
                    "id": "false_123456789@lid_abcd",
                    "from": "123456789@lid",
                    "fromMe": False,
                    "timestamp": int(datetime.now(UTC).timestamp()),
                    "body": "Есть",
                }
            ] * 50

    result = await reconcile_history(pool, Client(), case)
    assert result["history_unavailable"] == 1
    assert (
        await pool.fetchval(
            "SELECT history_page_offset FROM supplier_conversations WHERE id=$1", chat
        )
        == 0
    )
    assert await pool.fetchval(
        "SELECT history_error_code FROM supplier_conversations WHERE id=$1", chat
    )


@pytest.mark.postgres
async def test_unknown_followup_blocks_reply_projection_without_losing_incoming(pipeline_db):
    from procurement_bot.supplier_dialogue import WhatsAppSendDeferred
    from procurement_bot.worker import JobWorker

    pool, case, owner, run, _executor = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    await pool.execute("UPDATE supplier_conversations SET outbound_count=1 WHERE id=$1", chat)
    payload = {
        "case_id": str(case),
        "channel": "whatsapp",
        "recipient": "+77000000004",
        "conversation_id": str(chat),
        "text": "Какой размер?",
        "logical_action_id": "uncertain-followup",
        "is_followup": True,
    }
    async with pool.acquire() as c:
        approval = await request_approval(
            c, case_id=case, action_type="send_whatsapp", requested_by=owner, payload=payload
        )
    await pool.execute(
        "INSERT INTO outbox_events(event_type,payload,idempotency_key,status,"
        "approval_id,provider_request_id,last_error_code) "
        "VALUES('whatsapp.send_text',$1,$2,'retry',$3,'reserved-uncertain',"
        "'provider_acceptance_unknown')",
        payload,
        str(uuid4()),
        approval,
    )
    worker = object.__new__(JobWorker)
    worker.pool = pool
    with pytest.raises(WhatsAppSendDeferred, match="supplier_reply_original_outbound_unresolved"):
        await worker._process_supplier_reply(
            {"conversation_id": str(chat), "message_id": str(uuid4())}
        )
    assert (
        await pool.fetchval(
            "SELECT provider_request_id FROM outbox_events WHERE event_type='whatsapp.send_text'"
        )
        == "reserved-uncertain"
    )
