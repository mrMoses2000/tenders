from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.queue import (
    IncomingAttachment,
    bounded_backoff_seconds,
    claim_job,
    claim_outbox,
    complete_job,
    complete_outbox,
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
    delays = [
        bounded_backoff_seconds(i, cap_seconds=60, random_value=1)
        for i in range(1, 20)
    ]
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
            await pool.fetchval(
                "SELECT status FROM processed_updates WHERE update_id=7001"
            )
            == "completed"
        )

        job = await claim_job(pool, worker_id="test-worker", lease_seconds=30)
        assert job is not None
        assert job.kind == "download_attachment"
        assert job.payload["attachment_id"] == str(accepted.attachment_id)
        assert job.attempts == 1
        assert await renew_job_lease(
            pool, job.id, "test-worker", lease_seconds=60
        )
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
