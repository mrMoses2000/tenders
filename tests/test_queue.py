from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from test_autonomous_pipeline import pipeline_db  # noqa: F401, F811

from procurement_bot.db import run_migrations
from procurement_bot.queue import (
    ClaimedJob,
    ClaimedOutbox,
    IncomingAttachment,
    bounded_backoff_seconds,
    claim_job,
    claim_outbox,
    complete_job,
    complete_outbox,
    defer_job,
    defer_outbox,
    fail_job,
    persist_telegram_message,
    renew_job_lease,
    stable_key,
)


def test_stable_key_is_canonical_and_hides_inputs() -> None:
    first = stable_key("job", {"b": 2, "a": 1}, "private phone")
    second = stable_key("job", {"a": 1, "b": 2}, "private phone")
    assert first == second
    assert first.startswith("job:")
    assert len(first.removeprefix("job:")) == 64
    assert "private phone" not in first


@pytest.mark.parametrize("namespace", ["", "bad:namespace"])
def test_stable_key_rejects_ambiguous_namespace(namespace: str) -> None:
    with pytest.raises(ValueError):
        stable_key(namespace, "x")


def test_bounded_backoff_grows_but_never_exceeds_cap() -> None:
    delays = [bounded_backoff_seconds(i, cap_seconds=60, random_value=1) for i in range(1, 20)]
    assert delays == sorted(delays)
    assert delays[0] == pytest.approx(2.4)
    assert delays[-1] == 60


@pytest.mark.asyncio
async def test_atomic_ingress_and_leased_job_roundtrip() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_procurement_{uuid4().hex}"
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
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        attachment = IncomingAttachment(
            external_file_id="telegram-file-id",
            external_file_unique_id="telegram-unique-id",
            kind="document",
            original_filename="request.pdf",
            mime_type="application/pdf",
            declared_size=1234,
        )
        accepted = await persist_telegram_message(
            pool,
            update_id=7001,
            telegram_user_id=42,
            chat_id=43,
            telegram_message_id=44,
            message_kind="document",
            raw_update={"update_id": 7001},
            attachment=attachment,
            acknowledgement_text="Принял файл",
            received_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        assert accepted is not None
        assert accepted.attachment_id is not None
        assert accepted.download_job_id is not None
        assert accepted.acknowledgement_event_id is not None

        # Telegram redelivery cannot create a second message, job, or outbox row.
        duplicate = await persist_telegram_message(
            pool,
            update_id=7001,
            telegram_user_id=42,
            chat_id=43,
            telegram_message_id=44,
            message_kind="document",
            raw_update={"update_id": 7001},
            attachment=attachment,
            acknowledgement_text="Принял файл",
        )
        assert duplicate is None
        assert await pool.fetchval("SELECT count(*) FROM messages") == 1
        assert await pool.fetchval("SELECT count(*) FROM incoming_attachments") == 1
        assert await pool.fetchval("SELECT count(*) FROM jobs") == 1
        assert await pool.fetchval("SELECT count(*) FROM outbox_events") == 1
        assert (
            await pool.fetchval("SELECT status FROM processed_updates WHERE update_id=7001")
            == "completed"
        )

        job = await claim_job(pool, worker_id="test-worker", lease_seconds=30)
        assert job is not None
        assert job.kind == "download_attachment"
        assert job.payload["attachment_id"] == str(accepted.attachment_id)
        assert job.attempts == 1
        assert await renew_job_lease(pool, job.id, "test-worker", lease_seconds=60)
        assert not await complete_job(pool, job.id, "wrong-worker")
        assert await complete_job(pool, job.id, "test-worker")
        assert await pool.fetchval("SELECT status FROM jobs WHERE id=$1", job.id) == "succeeded"

        event = await claim_outbox(pool, worker_id="outbox-worker", lease_seconds=30)
        assert event is not None
        assert event.event_type == "telegram.send_message"
        assert event.payload == {"chat_id": 43, "text": "Принял файл"}
        assert not await complete_outbox(pool, event.id, "wrong-worker")
        assert await complete_outbox(
            pool, event.id, "outbox-worker", external_id="telegram-message-99"
        )
        sent = await pool.fetchrow(
            "SELECT status,external_id FROM outbox_events WHERE id=$1", event.id
        )
        assert dict(sent) == {
            "status": "sent",
            "external_id": "telegram-message-99",
        }
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["job", "outbox"])
async def test_deferral_is_lease_fenced_and_does_not_consume_attempt(kind: str) -> None:
    class RecordingPool:
        def __init__(self) -> None:
            self.sql = ""
            self.args: tuple[object, ...] = ()
            self.result = "UPDATE 1"

        async def execute(self, sql: str, *args: object) -> str:
            self.sql = sql
            self.args = args
            return self.result

    pool = RecordingPool()
    identifier = uuid4()
    available_at = datetime(2026, 9, 23, 9, tzinfo=UTC)
    if kind == "job":
        claimed = ClaimedJob(
            id=identifier,
            kind="enqueue_whatsapp_send",
            payload={"approval_id": str(uuid4())},
            attempts=5,
            max_attempts=5,
            idempotency_key="job:test",
            locked_by="worker-a",
            lease_expires_at=datetime(2026, 9, 23, 8, tzinfo=UTC),
        )
        deferred = await defer_job(pool, claimed, available_at=available_at, reason="quiet hours")
        assert "UPDATE jobs" in pool.sql
        assert "status='running'" in pool.sql
    else:
        claimed = ClaimedOutbox(
            id=identifier,
            event_type="whatsapp.send_text",
            payload={"text": "hello"},
            attempts=3,
            max_attempts=3,
            idempotency_key="outbox:test",
            locked_by="worker-a",
            lease_expires_at=datetime(2026, 9, 23, 8, tzinfo=UTC),
            approval_id=uuid4(),
        )
        deferred = await defer_outbox(
            pool, claimed, available_at=available_at, reason="daily limit"
        )
        assert "UPDATE outbox_events" in pool.sql
        assert "status='sending'" in pool.sql
    assert deferred is True
    assert "attempts=GREATEST(attempts-1,0)" in pool.sql
    assert "locked_by=$2" in pool.sql
    assert "lease_expires_at>now()" in pool.sql
    assert pool.args[0] == identifier
    assert pool.args[1] == "worker-a"
    assert pool.args[2] == available_at
    assert pool.args[3] in {"quiet hours", "daily limit"}


@pytest.mark.asyncio
async def test_deferral_rejects_lost_lease_and_naive_deadline() -> None:
    class RecordingPool:
        async def execute(self, _sql: str, *_args: object) -> str:
            return "UPDATE 0"

    job = ClaimedJob(
        id=uuid4(),
        kind="enqueue_whatsapp_send",
        payload={},
        attempts=1,
        max_attempts=5,
        idempotency_key="job:test",
        locked_by="worker-a",
        lease_expires_at=datetime(2026, 9, 23, 8, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        await defer_job(
            RecordingPool(),  # type: ignore[arg-type]
            job,
            available_at=datetime(2026, 9, 23, 9),
            reason="quiet hours",
        )
    with pytest.raises(RuntimeError, match="lease was lost"):
        await defer_job(
            RecordingPool(),  # type: ignore[arg-type]
            job,
            available_at=datetime(2026, 9, 23, 9, tzinfo=UTC),
            reason="quiet hours",
        )


@pytest.mark.postgres
async def test_fail_job_is_fenced_by_live_lease_and_claim_attempt(
    pipeline_db,  # noqa: F811
):
    pool, _case, _owner, _run, _executor = pipeline_db
    await pool.execute(
        "UPDATE jobs SET status='succeeded',locked_at=NULL,locked_by=NULL,"
        "lease_expires_at=NULL,completed_at=now()"
    )
    job_id = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,max_attempts) "
        "VALUES('process_supplier_reply',$1,'pending',$2,3) RETURNING id",
        {"conversation_id": "test", "message_id": "test"}, str(uuid4()),
    )
    claimed = await claim_job(pool, worker_id="shared-worker-id", lease_seconds=60)
    assert claimed is not None and claimed.id == job_id and claimed.attempts == 1
    wrong_owner = ClaimedJob(
        id=claimed.id, kind=claimed.kind, payload=claimed.payload,
        attempts=claimed.attempts, max_attempts=claimed.max_attempts,
        idempotency_key=claimed.idempotency_key, locked_by="other-worker",
        lease_expires_at=claimed.lease_expires_at,
    )
    with pytest.raises(RuntimeError, match="lease was lost"):
        await fail_job(pool, wrong_owner, "wrong_owner", retryable=True)
    await pool.execute(
        "UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=$1", job_id,
    )
    with pytest.raises(RuntimeError, match="lease was lost"):
        await fail_job(pool, claimed, "expired_owner", retryable=True)

    reclaimed = await claim_job(pool, worker_id="shared-worker-id", lease_seconds=60)
    assert reclaimed is not None and reclaimed.id == job_id and reclaimed.attempts == 2
    with pytest.raises(RuntimeError, match="lease was lost"):
        await fail_job(pool, claimed, "stale_attempt", retryable=True)

    assert not await fail_job(pool, reclaimed, "current_attempt", retryable=True)
    result = await pool.fetchrow(
        "SELECT status,attempts,locked_by,last_error_code FROM jobs WHERE id=$1", job_id,
    )
    assert result["status"] == "retry"
    assert result["attempts"] == 2
    assert result["locked_by"] is None
    assert result["last_error_code"] == "current_attempt"
