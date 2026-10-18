from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.pipeline_metrics import load_pipeline_health, load_pipeline_metrics
from procurement_bot.research import build_research_plan
from procurement_bot.research_repository import mark_research_running, persist_research_plan


class _NoCallBrowser:
    async def search_suppliers(self, plan):
        raise AssertionError("metrics loading must remain read-only")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pipeline_health_reports_aggregate_controls_ingress_and_stall_state() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_pipeline_visibility_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init(connection: asyncpg.Connection) -> None:
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
        max_size=5,
        server_settings={"search_path": schema},
        init=init,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        user_id, case_id = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
            user_id,
            930_000_000 + int(user_id.hex[:8], 16),
            "Visibility test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES($1,$2,$3,$4,'researching')",
            case_id,
            user_id,
            "Pipeline visibility",
            "Алматы",
        )
        item = ProcurementItem(name="Gauze roll", quantity=10, unit="roll")
        await pool.execute(
            "INSERT INTO request_items(case_id,line_number,name,quantity,unit,status) "
            "VALUES($1,1,$2,$3,$4,'ready')",
            case_id,
            item.name,
            item.quantity,
            item.unit,
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection,
                case_id=case_id,
                plan=build_research_plan(
                    ParsedProcurementRequest(
                        city="Алматы", search_scope=SearchScope.CITYWIDE, items=[item]
                    )
                ),
                requested_by=user_id,
            )
            await mark_research_running(connection, persisted.id)
        await ParallelResearchRunExecutor(pool, _NoCallBrowser()).execute(persisted.id)
        checkpoint = await pool.fetchrow(
            "SELECT * FROM research_branch_checkpoints WHERE research_run_id=$1 "
            "AND current_job_id IS NOT NULL ORDER BY id LIMIT 1",
            persisted.id,
        )
        assert checkpoint is not None
        await pool.execute(
            """UPDATE jobs SET status='succeeded',locked_at=NULL,locked_by=NULL,
                lease_expires_at=NULL WHERE id=$1""",
            checkpoint["current_job_id"],
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='queued',
                last_real_progress_at=now()-interval '4 hours',lease_until=NULL WHERE id=$1""",
            checkpoint["id"],
        )
        await pool.execute(
            """INSERT INTO case_pipeline_runtime(case_id,research_run_id,enabled,
                wake_reason,next_tick_at,last_tick_summary)
                VALUES($1,$2,TRUE,'research_running',now()+interval '4 minutes',$3::jsonb)""",
            case_id,
            persisted.id,
            {"waha_status": "UNAVAILABLE"},
        )
        supplier_id = await pool.fetchval(
            "INSERT INTO suppliers(display_name) VALUES('Visibility supplier') RETURNING id"
        )
        query_id = await pool.fetchval(
            "SELECT id FROM research_queries WHERE research_run_id=$1 LIMIT 1", persisted.id
        )
        evidence_id = await pool.fetchval(
            """INSERT INTO source_evidence(research_query_id,source_type,source_locator,
                captured_at,content_sha256,raw_payload,idempotency_key)
                VALUES($1,'web','visibility-source',now(),$2,'{}',$3) RETURNING id""",
            query_id,
            "a" * 64,
            str(uuid4()),
        )
        contact_id = await pool.fetchval(
            """INSERT INTO supplier_contacts(supplier_id,contact_type,display_value,
                normalized_value,source_evidence_id)
                VALUES($1,'whatsapp','+77000000013','+77000000013',$2) RETURNING id""",
            supplier_id,
            evidence_id,
        )
        conversation_id = await pool.fetchval(
            """INSERT INTO supplier_conversations(case_id,supplier_id,supplier_contact_id,
                session_name,external_chat_id,state,idempotency_key)
                VALUES($1,$2,$3,'visibility-session','77000000013@c.us',
                    'waiting_availability',$4) RETURNING id""",
            case_id,
            supplier_id,
            contact_id,
            str(uuid4()),
        )
        await pool.execute(
            """INSERT INTO whatsapp_account_controls(session_name,operator_held,
                operator_hold_reason,provider_state,provider_observed_at,provider_source,
                provider_restriction_until,circuit_open,circuit_reason,next_probe_at)
                VALUES('visibility-session',TRUE,'operator_review','restricted',
                    now()-interval '40 minutes','session_get',now()+interval '1 hour',
                    TRUE,'provider_rate_limit',now()+interval '8 minutes')"""
        )
        approval_id = await pool.fetchval(
            """INSERT INTO action_approvals(case_id,action_type,payload,payload_sha256,
                requested_by) VALUES($1,'send_whatsapp',$2::jsonb,$3,$4) RETURNING id""",
            case_id,
            {"case_id": str(case_id)},
            "b" * 64,
            user_id,
        )
        event_id = await pool.fetchval(
            """INSERT INTO outbox_events(event_type,payload,idempotency_key,status,
                last_error_code,approval_id) VALUES('whatsapp.send_text',$1::jsonb,$2,'retry',
                    'provider_acceptance_unknown',$3) RETURNING id""",
            {"case_id": str(case_id), "conversation_id": str(conversation_id)},
            str(uuid4()),
            approval_id,
        )
        await pool.execute(
            """INSERT INTO whatsapp_account_send_reservations(outbox_event_id,session_name,
                conversation_id,supplier_contact_id,state,is_new_contact,reserved_at)
                VALUES($1,'visibility-session',$2,$3,'unknown',TRUE,now())""",
            event_id,
            conversation_id,
            contact_id,
        )
        future_session = "visibility-future-session"
        future_conversation = await pool.fetchval(
            """INSERT INTO supplier_conversations(case_id,supplier_id,supplier_contact_id,
                session_name,external_chat_id,state,idempotency_key)
                VALUES($1,$2,$3,$4,'77000000013@future.c.us',
                    'waiting_availability',$5) RETURNING id""",
            case_id,
            supplier_id,
            contact_id,
            future_session,
            str(uuid4()),
        )
        await pool.execute(
            """INSERT INTO whatsapp_account_controls(session_name,operator_held,
                provider_state,provider_observed_at,circuit_open)
                VALUES($1,FALSE,'clear',now()+interval '1 minute',FALSE)""",
            future_session,
        )
        future_event_id = uuid4()
        await pool.execute(
            """INSERT INTO outbox_events(id,event_type,payload,idempotency_key,status)
                VALUES($1,'test.reserve',$2::jsonb,$3,'pending')""",
            future_event_id,
            {"case_id": str(case_id), "conversation_id": str(future_conversation)},
            str(uuid4()),
        )
        await pool.execute(
            """INSERT INTO whatsapp_account_send_reservations(outbox_event_id,session_name,
                conversation_id,supplier_contact_id,state,is_new_contact,reserved_at)
                VALUES($1,$2,$3,$4,'unknown',TRUE,now())""",
            future_event_id,
            future_session,
            future_conversation,
            contact_id,
        )
        today = await pool.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")
        await pool.executemany(
            """INSERT INTO whatsapp_ingress_audit(utc_day,disposition,reason_code,
                event_kind,first_seen_at,last_seen_at,observation_count)
                VALUES($1,$2,$3,'message',now(),now(),$4)""",
            [
                (today, "accepted", "message_saved", 2),
                (today, "ignored", "unsupported_kind", 3),
                (today, "rejected", "invalid_signature", 1),
            ],
        )

        async with pool.acquire() as connection, connection.transaction():
            health = await load_pipeline_health(connection, case_id)
            metrics = await load_pipeline_metrics(connection, case_id)

        account = health["account"]
        assert account["accounts"] == 2
        assert account["held"] == 1
        assert account["circuits_open"] == 1
        assert account["restricted"] == 1
        assert account["unavailable"] == 0
        assert account["state_unknown"] == 1
        assert account["state_stale"] == 2
        assert account["outbound_allowed"] == 0
        assert account["unresolved_reservations"] == 2
        assert account["daily_message_limit"] == 60
        assert account["max_concurrent_posts"] == 2
        assert account["session_status"] == "UNAVAILABLE"
        assert health["ingress"]["accepted_7d"] == 2
        assert health["ingress"]["ignored_7d"] == 3
        assert health["ingress"]["rejected_7d"] == 1
        assert health["research"]["last_real_progress_at"]
        assert health["research"]["queued_with_terminal_job"] == 1
        assert health["research"]["queued_without_runnable_job"] >= 1
        assert health["research"]["wait_reason"] == "queued_checkpoint_terminal_job"
        assert health["research"]["stalled"] is True
        assert metrics["accepted"] == 0
        assert metrics["outbound_projected"] == 0
        assert metrics["acceptance_unknown"] == 1

        await pool.execute(
            """UPDATE jobs SET status='succeeded',locked_at=NULL,locked_by=NULL,
                lease_expires_at=NULL WHERE payload->>'research_run_id'=$1::uuid::text""",
            persisted.id,
        )
        await pool.execute(
            """UPDATE research_branch_checkpoints SET status='succeeded',
                current_job_id=NULL,owner_worker=NULL,lease_until=NULL,completed_at=now()
                WHERE research_run_id=$1""",
            persisted.id,
        )
        await pool.execute(
            "UPDATE research_runs SET status='partial',completed_at=now() WHERE id=$1",
            persisted.id,
        )
        await pool.execute(
            "UPDATE case_pipeline_runtime SET wake_reason='reliability_release_20261006' "
            "WHERE research_run_id=$1",
            persisted.id,
        )
        async with pool.acquire() as connection, connection.transaction():
            terminal_health = await load_pipeline_health(connection, case_id)
        assert terminal_health["research"]["run_status"] == "partial"
        assert terminal_health["research"]["wait_reason"] == "run_not_running"
        assert terminal_health["research"]["runtime_wake_reason"] == "reliability_release_20261006"
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
