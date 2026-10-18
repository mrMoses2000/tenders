from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot import chat_actors
from procurement_bot.chat_actors import (
    ChatActorBusy,
    assign_supplier_dialogue,
    claim_whatsapp_outbox,
    release_chat_actor,
)
from procurement_bot.db import run_migrations

ROOT = Path(__file__).parents[1]


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        active_count: int = 0,
        active_group_count: int = 0,
        group_is_active: bool = False,
    ) -> None:
        self.group_id = uuid4()
        self.lane_id = uuid4()
        self.item_id = uuid4()
        self.supplier_id = uuid4()
        self.conversation_id = uuid4()
        self.assignment_id = uuid4()
        self.active_count = active_count
        self.active_group_count = active_group_count
        self.group_is_active = group_is_active
        self.insert_args: tuple[object, ...] | None = None
        self.locked_lane = False

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *_args: object) -> str:
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *_args: object) -> dict[str, Any] | None:
        if "count(DISTINCT group_id)" in sql:
            return {
                "group_count": self.active_group_count,
                "group_is_active": self.group_is_active,
            }
        if "FROM research_lanes AS lane" in sql:
            self.locked_lane = "FOR UPDATE OF lane" in sql
            return {
                "id": self.lane_id,
                "group_id": self.group_id,
                "lane_no": 1,
                "request_item_id": self.item_id,
            }
        if "FROM supplier_dialogue_assignments" in sql:
            return None
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: object) -> object:
        if "SELECT supplier_id FROM supplier_conversations" in sql:
            return self.supplier_id
        if "SELECT EXISTS" in sql and "supplier_conversation_targets" in sql:
            return True
        if "SELECT count(*) FROM supplier_dialogue_assignments" in sql:
            return self.active_count
        if "INSERT INTO supplier_dialogue_assignments" in sql:
            self.insert_args = args
            return self.assignment_id
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_assignment_locks_stable_lane_and_persists_item_mapping() -> None:
    connection = _Connection()

    assignment_id = await assign_supplier_dialogue(
        connection,  # type: ignore[arg-type]
        group_id=connection.group_id,
        lane_id=connection.lane_id,
        item_id=connection.item_id,
        supplier_id=connection.supplier_id,
        conversation_id=connection.conversation_id,
    )

    assert assignment_id == connection.assignment_id
    assert connection.locked_lane is True
    assert connection.insert_args is not None
    assert connection.insert_args[:5] == (
        connection.group_id,
        connection.lane_id,
        connection.item_id,
        connection.supplier_id,
        connection.conversation_id,
    )
    assert connection.insert_args[5:] == (20, 4, 5)


@pytest.mark.asyncio
async def test_assignment_enforces_five_active_supplier_item_limit_per_lane() -> None:
    connection = _Connection(active_count=5, active_group_count=3, group_is_active=True)

    with pytest.raises(ChatActorBusy, match="maximum active assignments"):
        await assign_supplier_dialogue(
            connection,  # type: ignore[arg-type]
            group_id=connection.group_id,
            lane_id=connection.lane_id,
            item_id=connection.item_id,
            supplier_id=connection.supplier_id,
            conversation_id=connection.conversation_id,
        )
    assert connection.insert_args is None


@pytest.mark.asyncio
async def test_assignment_enforces_three_active_dialogue_groups() -> None:
    connection = _Connection(active_group_count=3, group_is_active=False)

    with pytest.raises(ChatActorBusy, match="maximum number of active dialogue groups"):
        await assign_supplier_dialogue(
            connection,  # type: ignore[arg-type]
            group_id=connection.group_id,
            lane_id=connection.lane_id,
            item_id=connection.item_id,
            supplier_id=connection.supplier_id,
            conversation_id=connection.conversation_id,
        )


def test_migration_uses_physical_chat_key_and_stable_research_lane_fk() -> None:
    migration = (ROOT / "migrations" / "026_chat_actors.sql").read_text()
    assert "PRIMARY KEY (session_name,external_chat_id)" in migration
    assert "REFERENCES research_lanes(id,group_id)" in migration
    assert "REFERENCES supplier_conversations(id)" in migration
    assert "status IN ('assigned','waiting_reply')" in migration


def test_outbound_claim_serializes_the_physical_chat_and_fences_leases() -> None:
    source = Path(chat_actors.__file__).read_text()
    assert "FOR UPDATE OF event SKIP LOCKED" in source
    assert "prior_conversation.session_name=conversation.session_name" in source
    assert "prior_conversation.external_chat_id=conversation.external_chat_id" in source
    assert "prior.status IN ('pending','retry','sending')" in source
    assert "prior.status='dead'" in source
    assert "prior.provider_request_id IS NOT NULL" in source
    assert "prior.external_id IS NULL" in source
    assert "last_error_code='provider_outcome_unknown'" in source
    assert "AND event.provider_request_id IS NULL" in source
    assert "generation=generation+1" in source
    assert "AND owner_worker_id=$4 AND generation=$6" in source


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_concurrent_claims_serialize_same_physical_chat() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_chat_actors_{uuid4().hex}"
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
        max_size=4,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, ROOT / "migrations")
        user_id, case_id, supplier_id = uuid4(), uuid4(), uuid4()
        contact_evidence_id, contact_id, conversation_id = uuid4(), uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            user_id,
            800_000_000 + int(user_id.hex[:8], 16),
            "Actor test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title) VALUES ($1,$2,$3)",
            case_id,
            user_id,
            "Chat actor test",
        )
        await pool.execute(
            "INSERT INTO suppliers(id,display_name) VALUES ($1,$2)",
            supplier_id,
            "Actor supplier",
        )
        await pool.execute(
            """INSERT INTO source_evidence
            (id,source_type,source_locator,captured_at,content_sha256,raw_payload,idempotency_key)
            VALUES ($1,'manual','actor-test',now(),$2,'{}'::jsonb,$3)""",
            contact_evidence_id,
            "a" * 64,
            f"actor-test:{contact_evidence_id}",
        )
        await pool.execute(
            """INSERT INTO supplier_contacts
            (id,supplier_id,contact_type,display_value,normalized_value,source_evidence_id)
            VALUES ($1,$2,'whatsapp','+77000000003','+77000000003',$3)""",
            contact_id,
            supplier_id,
            contact_evidence_id,
        )
        await pool.execute(
            """INSERT INTO supplier_conversations
            (id,case_id,supplier_id,supplier_contact_id,session_name,external_chat_id,
             state,idempotency_key)
            VALUES ($1,$2,$3,$4,'default','77000000003@c.us','approved',$5)""",
            conversation_id,
            case_id,
            supplier_id,
            contact_id,
            f"actor-conversation:{conversation_id}",
        )
        event_ids = [uuid4(), uuid4()]
        for index, event_id in enumerate(event_ids):
            approval_id = uuid4()
            await pool.execute(
                """INSERT INTO action_approvals
                (id,case_id,action_type,payload,payload_sha256,requested_by)
                VALUES ($1,$2,'send_whatsapp','{}'::jsonb,$3,$4)""",
                approval_id, case_id, f"{index + 1:064x}", user_id,
            )
            await pool.execute(
                """INSERT INTO outbox_events
                (id,event_type,payload,idempotency_key,created_at,approval_id)
                VALUES ($1,'whatsapp.send_text',$2,$3,now()+$4::interval,$5)""",
                event_id,
                {
                    "case_id": str(case_id),
                    "channel": "whatsapp",
                    "recipient": "+77000000003",
                    "text": f"message {index}",
                    "conversation_id": str(conversation_id),
                    "logical_action_id": f"actor-test-{index}",
                    "is_followup": False,
                },
                f"actor-event:{event_id}",
                timedelta(seconds=index),
                approval_id,
            )

        claims = await asyncio.gather(
            claim_whatsapp_outbox(pool, worker_id="actor-worker-a", lease_seconds=30),
            claim_whatsapp_outbox(pool, worker_id="actor-worker-b", lease_seconds=30),
        )
        claimed = [claim for claim in claims if claim is not None]
        assert len(claimed) == 1
        assert claimed[0][0].id == event_ids[0]
        assert await pool.fetchval(
            "SELECT count(*) FROM chat_actor_leases "
            "WHERE session_name='default' AND external_chat_id='77000000003@c.us' "
            "AND owner_worker_id IS NOT NULL"
        ) == 1
        await release_chat_actor(pool, claimed[0][1])
        await pool.execute(
            """UPDATE outbox_events
            SET provider_request_id='reserved-but-unknown',
                lease_expires_at=now()-interval '1 second'
            WHERE id=$1""",
            event_ids[0],
        )
        assert await claim_whatsapp_outbox(
            pool, worker_id="actor-worker-after-crash", lease_seconds=30
        ) is None
        assert await pool.fetchval(
            "SELECT status FROM outbox_events WHERE id=$1", event_ids[0]
        ) == "dead"
        assert await pool.fetchval(
            "SELECT status FROM outbox_events WHERE id=$1", event_ids[1]
        ) == "pending"
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
