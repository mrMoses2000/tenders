from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.pipeline_runtime import wake_pipeline


@pytest.mark.asyncio
async def test_owner_wake_advances_only_retrying_transport_waits() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_transport_wake_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init_connection(connection: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=lambda value: json.dumps(value, ensure_ascii=False),
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
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner_id, case_id, run_id = uuid4(), uuid4(), uuid4()
        supplier_id, contact_id, evidence_id, conversation_id = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO users(id,telegram_id) VALUES($1,$2)",
                owner_id,
                int(uuid4().int % 2_000_000_000) + 1,
            )
            await connection.execute(
                "INSERT INTO procurement_cases(id,owner_user_id,status) VALUES($1,$2,'contacting')",
                case_id,
                owner_id,
            )
            await connection.execute(
                """INSERT INTO research_runs(id,case_id,idempotency_key,status)
                   VALUES($1,$2,$3,'partial')""",
                run_id,
                case_id,
                f"run:{run_id}",
            )
            await connection.execute(
                "INSERT INTO case_pipeline_runtime(case_id,research_run_id,enabled) "
                "VALUES($1,$2,true)",
                case_id,
                run_id,
            )
            await connection.execute(
                "INSERT INTO suppliers(id,display_name) VALUES($1,'Test Supplier')",
                supplier_id,
            )
            await connection.execute(
                """INSERT INTO source_evidence(
                    id,source_type,source_locator,captured_at,content_sha256,raw_payload,idempotency_key
                ) VALUES($1,'manual','manual:test',now(),$2,'{}',$3)""",
                evidence_id,
                "a" * 64,
                f"evidence:{evidence_id}",
            )
            await connection.execute(
                """INSERT INTO supplier_contacts(
                    id,supplier_id,contact_type,display_value,normalized_value,source_evidence_id
                ) VALUES($1,$2,'phone','+77000000003','+77000000003',$3)""",
                contact_id,
                supplier_id,
                evidence_id,
            )
            await connection.execute(
                """INSERT INTO supplier_conversations(
                    id,case_id,supplier_id,supplier_contact_id,session_name,external_chat_id,idempotency_key
                ) VALUES($1,$2,$3,$4,'default','77000000003@c.us',$5)""",
                conversation_id,
                case_id,
                supplier_id,
                contact_id,
                f"conversation:{conversation_id}",
            )

        due = datetime.now(UTC) + timedelta(hours=2)
        rows: dict[str, tuple[object, str | None, str, str]] = {
            "unknown": (uuid4(), "reserved-full-id", "provider_acceptance_unknown", "retry"),
            "session": (uuid4(), None, "waha_session_unavailable", "retry"),
            "mismatch": (uuid4(), None, "waha_session_mismatch", "retry"),
            "pending": (uuid4(), None, "provider_acceptance_unknown", "pending"),
        }
        async with pool.acquire() as connection:
            for _name, (approval_id, _provider_id, _reason, _status) in rows.items():
                await connection.execute(
                    """INSERT INTO action_approvals(
                        id,case_id,action_type,payload,payload_sha256
                    ) VALUES($1,$2,'send_whatsapp','{}',$3)""",
                    approval_id,
                    case_id,
                    hashlib.sha256(_name.encode()).hexdigest(),
                )
            for name, (approval_id, provider_id, reason, status) in rows.items():
                payload = {
                    "case_id": str(case_id),
                    "channel": "whatsapp",
                    "recipient": "+77000000003",
                    "text": "Здравствуйте",
                    "conversation_id": str(conversation_id),
                    "logical_action_id": f"wake-{name}",
                    "is_followup": False,
                }
                await connection.execute(
                    """INSERT INTO outbox_events(
                        id,event_type,payload,status,attempts,max_attempts,available_at,
                        idempotency_key,approval_id,provider_request_id,last_error_code
                    ) VALUES($1,'whatsapp.send_text',$2,$3,2,5,$4,$5,$6,$7,$8)""",
                    uuid4(),
                    payload,
                    status,
                    due,
                    f"outbox:{name}",
                    approval_id,
                    provider_id,
                    reason,
                )
            await connection.execute(
                """INSERT INTO jobs(
                    kind,payload,status,attempts,max_attempts,available_at,last_error_code,idempotency_key
                ) VALUES('prepare_supplier_outreach',$1,'pending',2,5,$2,
                         'waha_session_unavailable','prepare:wake-case')""",
                {"case_id": str(case_id)},
                due,
            )

            other_case_id, other_conversation_id = uuid4(), uuid4()
            await connection.execute(
                "INSERT INTO procurement_cases(id,owner_user_id,status) VALUES($1,$2,'contacting')",
                other_case_id, owner_id,
            )
            await connection.execute(
                """INSERT INTO supplier_conversations(
                    id,case_id,supplier_id,supplier_contact_id,session_name,external_chat_id,idempotency_key
                ) VALUES($1,$2,$3,$4,'default','77000000015@c.us',$5)""",
                other_conversation_id, other_case_id, supplier_id, contact_id,
                f"conversation:{other_conversation_id}",
            )
            for name, target, status, error in [
                ("quota", conversation_id, "pending", "provider_quota_sleep"),
                ("other-case", other_conversation_id, "pending", "provider_quota_sleep"),
                ("running", conversation_id, "running", "provider_quota_sleep"),
                ("dead", conversation_id, "dead", "provider_quota_sleep"),
                ("other-error", conversation_id, "pending", "AgyCliStatusError"),
            ]:
                await connection.execute(
                    """INSERT INTO jobs(
                        kind,payload,status,attempts,max_attempts,available_at,last_error_code,
                        idempotency_key,locked_at,locked_by,lease_expires_at
                    ) VALUES('process_supplier_reply',$1,$2,2,5,$3,$4,$5,
                        CASE WHEN $2='running' THEN now() ELSE NULL END,
                        CASE WHEN $2='running' THEN 'test-worker' ELSE NULL END,
                        CASE WHEN $2='running' THEN now()+interval '30 seconds' ELSE NULL END)""",
                    {"conversation_id": str(target), "message_id": str(uuid4())},
                    status, due, error, f"reply:{name}",
                )
            await connection.execute(
                """INSERT INTO jobs(
                    kind,payload,status,attempts,max_attempts,available_at,last_error_code,idempotency_key
                ) VALUES('prepare_supplier_outreach',$1,'pending',2,5,$2,
                         'provider_quota_sleep','prepare:other-reason')""",
                {"case_id": str(case_id)},
                due,
            )

        async with pool.acquire() as connection:
            assert await wake_pipeline(
                connection, case_id=case_id, owner_id=owner_id, reason="telegram_continue"
            )
        async with pool.acquire() as connection:
            woken = await connection.fetch(
                """SELECT event.id,event.idempotency_key,event.status,event.attempts,
                          event.provider_request_id,
                          event.last_error_code,event.available_at<=now() AS due
                   FROM outbox_events event WHERE event.idempotency_key LIKE 'outbox:%'"""
            )
            jobs = await connection.fetch(
                "SELECT idempotency_key,status,attempts,last_error_code,available_at<=now() AS due "
                "FROM jobs WHERE kind='prepare_supplier_outreach'"
            )
            reply_jobs = await connection.fetch(
                "SELECT idempotency_key,status,attempts,last_error_code,available_at<=now() AS due "
                "FROM jobs WHERE kind='process_supplier_reply'"
            )
        by_key = {row["idempotency_key"]: row for row in woken}
        assert by_key["outbox:unknown"]["due"] is True
        assert by_key["outbox:unknown"]["last_error_code"] == "provider_acceptance_unknown"
        assert by_key["outbox:unknown"]["provider_request_id"] == "reserved-full-id"
        assert by_key["outbox:unknown"]["attempts"] == 2
        assert by_key["outbox:session"]["due"] is True
        assert by_key["outbox:session"]["last_error_code"] == "waha_session_unavailable"
        assert by_key["outbox:session"]["provider_request_id"] is None
        assert by_key["outbox:session"]["attempts"] == 2
        assert by_key["outbox:mismatch"]["due"] is False
        assert any(row["status"] == "pending" and row["due"] is False for row in woken)
        by_job_key = {row["idempotency_key"]: row for row in jobs}
        assert by_job_key["prepare:wake-case"]["due"] is True
        assert by_job_key["prepare:wake-case"]["status"] == "pending"
        assert by_job_key["prepare:wake-case"]["attempts"] == 2
        assert by_job_key["prepare:wake-case"]["last_error_code"] == "waha_session_unavailable"
        assert by_job_key["prepare:other-reason"]["due"] is False
        by_reply_key = {row["idempotency_key"]: row for row in reply_jobs}
        assert by_reply_key["reply:quota"]["due"] is True
        assert by_reply_key["reply:quota"]["status"] == "pending"
        assert by_reply_key["reply:quota"]["attempts"] == 2
        assert by_reply_key["reply:quota"]["last_error_code"] == "provider_quota_sleep"
        for name in ["other-case", "running", "dead", "other-error"]:
            assert by_reply_key[f"reply:{name}"]["due"] is False
            assert by_reply_key[f"reply:{name}"]["attempts"] == 2
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
