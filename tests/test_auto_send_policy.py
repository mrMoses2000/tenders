from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from procurement_bot.approvals import payload_sha256
from procurement_bot.auto_send_policy import (
    AutoSendPolicyConflict,
    AutoSendPolicyDenied,
    assert_auto_send_authorization_active,
    authorize_and_enqueue_auto_whatsapp,
    auto_send_authorization_is_active,
    get_case_send_policy,
    next_send_eligible_at,
    set_case_auto_send_policy,
)
from procurement_bot.db import run_migrations
from procurement_bot.errors import ValidationBlocked
from procurement_bot.queue import stable_key
from procurement_bot.supplier_dialogue import SendPolicy


class FakeConnection:
    def __init__(self) -> None:
        self.case_id = uuid4()
        self.owner_id = uuid4()
        self.other_owner_id = uuid4()
        self.policy_id = uuid4()
        self.approval_id = uuid4()
        self.job_id = uuid4()
        self.payload = {
            "case_id": str(self.case_id),
            "channel": "whatsapp",
            "recipient": "+7 700 000-00-03",
            "text": "Здравствуйте! Есть ли товар в наличии?",
            "conversation_id": str(uuid4()),
            "logical_action_id": "availability-v1",
            "is_followup": False,
        }
        self.approval = {
            "id": self.approval_id,
            "case_id": self.case_id,
            "action_type": "send_whatsapp",
            "payload": self.payload,
            "payload_sha256": payload_sha256(self.payload),
            "status": "requested",
            "requested_by": self.owner_id,
            "approved_by": None,
            "is_live": True,
        }
        self.policy = {
            "id": self.policy_id,
            "owner_user_id": self.owner_id,
            "case_owner_id": self.owner_id,
            "consent_reference": "codex-thread:consent-1",
            "consent_text": "explicit owner consent",
        }
        self.authorization = None
        self.case_exists = True
        self.active_policy = True
        self.case_status = "ready"
        self.owner_active = True
        self.policy_rows = []
        self.job = None
        self.sql_calls = []

    async def fetchrow(self, query: str, *args: object):
        self.sql_calls.append((query, args))
        if "FROM procurement_cases AS case_row" in query:
            return (
                {
                    "owner_user_id": self.owner_id,
                    "status": self.case_status,
                    "active": self.owner_active,
                }
                if self.case_exists
                else None
            )
        if "FROM case_auto_send_policies" in query and "revoked_at IS NULL FOR UPDATE" in query:
            return self.policy if self.active_policy else None
        if (
            "JOIN procurement_cases AS case_row" in query
            and "FROM case_auto_send_policies AS policy" in query
        ):
            if not self.active_policy:
                return None
            return {
                **self.policy,
                "case_status": self.case_status,
                "owner_active": self.owner_active,
            }
        if "FROM action_approvals WHERE id=$1 FOR UPDATE" in query:
            return self.approval if args[0] == self.approval_id else None
        if "FROM auto_send_approval_authorizations" in query:
            return self.authorization
        if "FROM jobs WHERE idempotency_key=$1" in query:
            return self.job
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetchval(self, query: str, *args: object):
        self.sql_calls.append((query, args))
        if "INSERT INTO case_auto_send_policies" in query:
            self.policy_id = uuid4()
            self.policy = {
                "id": self.policy_id,
                "owner_user_id": args[1],
                "case_owner_id": self.owner_id,
                "consent_reference": args[2],
                "consent_text": args[3],
            }
            self.active_policy = True
            return self.policy_id
        if "INSERT INTO jobs" in query:
            self.job = {
                "id": self.job_id,
                "kind": args[1],
                "payload": args[2],
                "idempotency_key": args[3],
            }
            return self.job_id
        if "SELECT CASE" in query and "auto_send_approval_authorizations" in query:
            return (not self.authorization) or (
                self.active_policy
                and self.owner_active
                and self.case_status not in {"closed", "cancelled"}
            )
        if "SELECT EXISTS" in query and "auto_send_approval_authorizations" in query:
            return bool(self.authorization and self.active_policy)
        if "SELECT EXISTS" in query and "case_auto_send_policies" in query:
            return (
                self.active_policy
                and self.owner_active
                and self.case_status not in {"closed", "cancelled"}
            )
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query: str, *args: object) -> str:
        self.sql_calls.append((query, args))
        if "UPDATE case_auto_send_policies" in query:
            self.active_policy = False
            self.policy_rows.append({"id": args[0], "revoked_by": args[1]})
            return "UPDATE 1"
        if "UPDATE action_approvals" in query:
            if self.approval["status"] != "requested":
                return "UPDATE 0"
            self.approval["status"] = "approved"
            self.approval["approved_by"] = args[1]
            return "UPDATE 1"
        if "INSERT INTO auto_send_approval_authorizations" in query:
            self.authorization = {
                "approval_id": args[0],
                "policy_id": args[1],
                "case_id": args[2],
                "authorized_by": args[3],
                "payload_sha256": args[4],
            }
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {query}")


@pytest.mark.asyncio
async def test_set_policy_requires_owner_and_persists_immutable_consent() -> None:
    connection = FakeConnection()
    policy_id = await set_case_auto_send_policy(
        connection,
        case_id=connection.case_id,
        owner_user_id=connection.owner_id,
        enabled=True,
        consent_reference="codex-thread:consent-1",
        consent_text="explicit owner consent",
    )
    assert policy_id == connection.policy_id

    again = await set_case_auto_send_policy(
        connection,
        case_id=connection.case_id,
        owner_user_id=connection.owner_id,
        enabled=True,
        consent_reference="codex-thread:consent-1",
        consent_text="explicit owner consent",
    )
    assert again == policy_id

    with pytest.raises(AutoSendPolicyDenied, match="acting owner"):
        await set_case_auto_send_policy(
            connection,
            case_id=connection.case_id,
            owner_user_id=connection.other_owner_id,
            enabled=True,
            consent_reference="codex-thread:consent-1",
            consent_text="explicit owner consent",
        )
    with pytest.raises(AutoSendPolicyConflict, match="immutable"):
        await set_case_auto_send_policy(
            connection,
            case_id=connection.case_id,
            owner_user_id=connection.owner_id,
            enabled=True,
            consent_reference="codex-thread:other",
            consent_text="changed consent",
        )

    revoked = await set_case_auto_send_policy(
        connection,
        case_id=connection.case_id,
        owner_user_id=connection.owner_id,
        enabled=False,
    )
    assert revoked == policy_id
    assert connection.active_policy is False


@pytest.mark.asyncio
async def test_exact_requested_approval_is_auto_authorized_and_job_is_idempotent() -> None:
    connection = FakeConnection()
    job_id = await authorize_and_enqueue_auto_whatsapp(
        connection,
        approval_id=connection.approval_id,
        case_id=connection.case_id,
        payload=connection.payload,
        requested_by=connection.owner_id,
    )
    assert job_id == connection.job_id
    assert connection.approval["status"] == "approved"
    assert connection.approval["approved_by"] == connection.owner_id
    assert connection.authorization == {
        "approval_id": connection.approval_id,
        "policy_id": connection.policy_id,
        "case_id": connection.case_id,
        "authorized_by": connection.owner_id,
        "payload_sha256": payload_sha256(connection.payload),
    }
    job_call = next(args for sql, args in connection.sql_calls if "INSERT INTO jobs" in sql)
    assert job_call[1] == "enqueue_whatsapp_send"
    assert job_call[2] == {"approval_id": str(connection.approval_id)}
    assert job_call[3] == stable_key("auto-whatsapp-send", connection.approval_id)

    job_id_again = await authorize_and_enqueue_auto_whatsapp(
        connection,
        approval_id=connection.approval_id,
        case_id=connection.case_id,
        payload=connection.payload,
        requested_by=connection.owner_id,
    )
    assert job_id_again == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "revoked", "expired", "consumed"])
async def test_handled_approval_is_never_rewritten_or_enqueued(status: str) -> None:
    connection = FakeConnection()
    connection.approval["status"] = status
    result = await authorize_and_enqueue_auto_whatsapp(
        connection,
        approval_id=connection.approval_id,
        case_id=connection.case_id,
        payload=connection.payload,
        requested_by=connection.owner_id,
    )
    assert result is None
    assert connection.approval["status"] == status
    assert not any("UPDATE action_approvals" in sql for sql, _ in connection.sql_calls)


@pytest.mark.asyncio
async def test_manual_approval_is_not_replaced_or_auto_enqueued() -> None:
    connection = FakeConnection()
    connection.approval["status"] = "approved"
    connection.approval["approved_by"] = connection.other_owner_id
    result = await authorize_and_enqueue_auto_whatsapp(
        connection,
        approval_id=connection.approval_id,
        case_id=connection.case_id,
        payload=connection.payload,
        requested_by=connection.owner_id,
    )
    assert result is None
    assert connection.approval["approved_by"] == connection.other_owner_id
    assert not any("INSERT INTO jobs" in sql for sql, _ in connection.sql_calls)


@pytest.mark.asyncio
async def test_authorization_fails_closed_for_wrong_owner_or_payload() -> None:
    connection = FakeConnection()
    with pytest.raises(AutoSendPolicyDenied, match="case owner"):
        await authorize_and_enqueue_auto_whatsapp(
            connection,
            approval_id=connection.approval_id,
            case_id=connection.case_id,
            payload=connection.payload,
            requested_by=connection.other_owner_id,
        )
    assert connection.approval["status"] == "requested"

    changed_payload = {**connection.payload, "text": "Different exact text"}
    with pytest.raises(AutoSendPolicyDenied, match="hash"):
        await authorize_and_enqueue_auto_whatsapp(
            connection,
            approval_id=connection.approval_id,
            case_id=connection.case_id,
            payload=changed_payload,
            requested_by=connection.owner_id,
        )


@pytest.mark.asyncio
async def test_inactive_or_revoked_policy_does_not_auto_authorize() -> None:
    connection = FakeConnection()
    connection.active_policy = False
    assert (
        await authorize_and_enqueue_auto_whatsapp(
            connection,
            approval_id=connection.approval_id,
            case_id=connection.case_id,
            payload=connection.payload,
            requested_by=connection.owner_id,
        )
        is None
    )
    assert connection.approval["status"] == "requested"


@pytest.mark.asyncio
async def test_case_send_policy_changes_only_active_cases() -> None:
    connection = FakeConnection()
    default = SendPolicy(daily_limit=10, conversation_limit=4, followup_limit=1)
    policy = await get_case_send_policy(connection, connection.case_id, default)
    assert policy.daily_limit == 30
    assert policy.conversation_limit == 60
    assert policy.followup_limit == 1
    assert policy.minimum_interval == default.minimum_interval
    assert policy.quiet_start == default.quiet_start

    connection.active_policy = False
    assert await get_case_send_policy(connection, connection.case_id, default) is default


@pytest.mark.asyncio
async def test_send_time_authorization_follows_policy_revocation() -> None:
    connection = FakeConnection()
    # Manual approvals have no auto-policy record and keep their old behavior.
    assert (
        await auto_send_authorization_is_active(connection, approval_id=connection.approval_id)
        is True
    )
    connection.authorization = {"approval_id": connection.approval_id}
    assert (
        await auto_send_authorization_is_active(connection, approval_id=connection.approval_id)
        is True
    )
    connection.active_policy = False
    assert (
        await auto_send_authorization_is_active(connection, approval_id=connection.approval_id)
        is False
    )
    with pytest.raises(ValidationBlocked, match="revoked autonomous"):
        await assert_auto_send_authorization_active(connection, approval_id=connection.approval_id)


def test_next_send_time_observes_quiet_hours_and_minimum_interval() -> None:
    timezone = ZoneInfo("Asia/Almaty")
    policy = SendPolicy(minimum_interval=timedelta(minutes=2))
    during_quiet = datetime(2026, 10, 1, 21, 0, tzinfo=timezone)
    assert next_send_eligible_at(during_quiet, policy=policy) == datetime(
        2026, 10, 2, 9, 0, tzinfo=timezone
    ).astimezone(UTC)

    daytime = datetime(2026, 10, 1, 12, 0, tzinfo=timezone)
    last_sent = daytime - timedelta(seconds=30)
    assert next_send_eligible_at(
        daytime,
        policy=policy,
        last_sent_at=last_sent,
    ) == daytime.astimezone(UTC) + timedelta(seconds=90)

    with pytest.raises(ValueError, match="timezone-aware"):
        next_send_eligible_at(datetime(2026, 10, 1, 12), policy=policy)


def test_migration_is_additive_and_preserves_approval_history() -> None:
    from pathlib import Path

    migration = Path(__file__).parents[1] / "migrations" / "034_case_auto_send_policy.sql"
    sql = migration.read_text()
    assert "CREATE TABLE IF NOT EXISTS case_auto_send_policies" in sql
    assert "case_auto_send_policies_one_active_idx" in sql
    assert "consent_reference TEXT NOT NULL" in sql
    assert "consent_text TEXT NOT NULL" in sql
    assert "CREATE TABLE IF NOT EXISTS auto_send_approval_authorizations" in sql
    assert "UPDATE action_approvals" not in sql
    assert "approve_all" not in sql


@pytest.mark.asyncio
async def test_closed_case_or_inactive_owner_disables_auto_case_policy() -> None:
    connection = FakeConnection()
    default = SendPolicy(daily_limit=10, conversation_limit=4, followup_limit=1)
    connection.case_status = "closed"
    assert await get_case_send_policy(connection, connection.case_id, default) is default

    connection.case_status = "ready"
    connection.owner_active = False
    assert await get_case_send_policy(connection, connection.case_id, default) is default


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_auto_send_policy_owner_exact_payload_and_revocation() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_auto_send_policy_{uuid4().hex}"
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
        owner_id, case_id = uuid4(), uuid4()
        conversation_id, approval_id = uuid4(), uuid4()
        telegram_id = 780_000_000 + int(owner_id.hex[:8], 16)
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            owner_id,
            telegram_id,
            "Auto-send policy postgres test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,'Policy test','Алматы','evaluating')",
            case_id,
            owner_id,
        )
        payload = {
            "case_id": str(case_id),
            "channel": "whatsapp",
            "recipient": "+77000000003",
            "text": "Здравствуйте! Есть ли товар в наличии?",
            "conversation_id": str(conversation_id),
            "logical_action_id": "postgres-policy-test",
            "is_followup": False,
        }
        await pool.execute(
            """INSERT INTO action_approvals(
                   id,case_id,action_type,payload,payload_sha256,status,requested_by
               ) VALUES ($1,$2,'send_whatsapp',$3::jsonb,$4,'requested',$5)""",
            approval_id,
            case_id,
            payload,
            payload_sha256(payload),
            owner_id,
        )

        async with pool.acquire() as connection, connection.transaction():
            policy_id = await set_case_auto_send_policy(
                connection,
                case_id=case_id,
                owner_user_id=owner_id,
                enabled=True,
                consent_reference="test:auto-send-policy-postgres",
                consent_text="Explicit test owner authorization",
            )
        assert policy_id is not None
        async with pool.acquire() as connection:
            send_policy = await get_case_send_policy(connection, case_id, SendPolicy())
            assert send_policy.daily_limit == 30
            assert send_policy.conversation_limit == 60
            job_id = await authorize_and_enqueue_auto_whatsapp(
                connection,
                approval_id=approval_id,
                case_id=case_id,
                payload=payload,
                requested_by=owner_id,
            )
            assert job_id is not None
            await assert_auto_send_authorization_active(connection, approval_id=approval_id)
        assert (
            await pool.fetchval("SELECT status FROM action_approvals WHERE id=$1", approval_id)
            == "approved"
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM auto_send_approval_authorizations "
                "WHERE approval_id=$1 AND policy_id=$2 AND payload_sha256=$3",
                approval_id,
                policy_id,
                payload_sha256(payload),
            )
            == 1
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM jobs WHERE id=$1 AND kind='enqueue_whatsapp_send' "
                "AND payload=$2::jsonb",
                job_id,
                {"approval_id": str(approval_id)},
            )
            == 1
        )

        await pool.execute("UPDATE procurement_cases SET status='closed' WHERE id=$1", case_id)
        async with pool.acquire() as connection:
            assert await get_case_send_policy(connection, case_id, SendPolicy()) == SendPolicy()
            assert not await auto_send_authorization_is_active(connection, approval_id=approval_id)
            with pytest.raises(ValidationBlocked, match="revoked autonomous"):
                await assert_auto_send_authorization_active(connection, approval_id=approval_id)

        await pool.execute("UPDATE procurement_cases SET status='evaluating' WHERE id=$1", case_id)
        await pool.execute("UPDATE users SET active=FALSE WHERE id=$1", owner_id)
        async with pool.acquire() as connection:
            assert await get_case_send_policy(connection, case_id, SendPolicy()) == SendPolicy()
            assert not await auto_send_authorization_is_active(connection, approval_id=approval_id)

        await pool.execute("UPDATE users SET active=TRUE WHERE id=$1", owner_id)
        async with pool.acquire() as connection, connection.transaction():
            assert (
                await set_case_auto_send_policy(
                    connection,
                    case_id=case_id,
                    owner_user_id=owner_id,
                    enabled=False,
                )
                == policy_id
            )
        async with pool.acquire() as connection:
            assert await get_case_send_policy(connection, case_id, SendPolicy()) == SendPolicy()
            assert not await auto_send_authorization_is_active(connection, approval_id=approval_id)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
