from __future__ import annotations

import itertools
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.providers.waha import WahaAccountIdentity
from procurement_bot.whatsapp_account import (
    pin_whatsapp_account_identity,
)
from scripts.postgres_test_safety import configured_postgres_target, verify_postgres_test_connection


class _IdentityClient:
    session_name = "identity-test-session"

    def __init__(self, account_jid: str) -> None:
        self.identity = WahaAccountIdentity(account_jid, datetime.now(UTC))

    async def get_account_identity(self) -> WahaAccountIdentity:
        return self.identity


@pytest.fixture
async def identity_pg(request):
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set TEST_POSTGRES_DSN to an isolated disposable PostgreSQL database")
    target = configured_postgres_target(dsn)
    if not target.host or target.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("account identity integration tests require local TCP PostgreSQL")
    await verify_postgres_test_connection(dsn, target)
    admin = await asyncpg.connect(dsn)
    schema = f"waha_identity_{uuid4().hex}"
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
        max_size=2,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        if getattr(request, "param", None) == "pre043":
            with TemporaryDirectory() as directory:
                for path in (Path(__file__).parents[1] / "migrations").glob("*.sql"):
                    if path.name[:3].isdigit() and int(path.name[:3]) < 43:
                        Path(directory, path.name).symlink_to(path)
                await run_migrations(pool, Path(directory))
        else:
            await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner_id = uuid4()
        case_id = uuid4()
        supplier_id = uuid4()
        evidence_id = uuid4()
        contact_id = uuid4()
        conversation_id = uuid4()
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
                owner_id,
                int(uuid4().int % 2_000_000_000) + 1,
                "Identity test owner",
            )
            await connection.execute(
                "INSERT INTO procurement_cases(id,owner_user_id,status) VALUES($1,$2,'draft')",
                case_id,
                owner_id,
            )
            await connection.execute(
                """INSERT INTO source_evidence(
                       id,source_type,source_locator,captured_at,content_sha256,
                       raw_payload,idempotency_key
                   ) VALUES($1,'manual','identity-test',now(),$2,'{}'::jsonb,$3)""",
                evidence_id,
                "a" * 64,
                f"identity-evidence:{evidence_id}",
            )
            await connection.execute(
                "INSERT INTO suppliers(id,display_name) VALUES($1,'Test supplier')",
                supplier_id,
            )
            await connection.execute(
                """INSERT INTO supplier_contacts(
                       id,supplier_id,contact_type,display_value,normalized_value,
                       source_evidence_id
                   ) VALUES($1,$2,'phone','+77000000003','77000000003',$3)""",
                contact_id,
                supplier_id,
                evidence_id,
            )
            await connection.execute(
                """INSERT INTO supplier_conversations(
                       id,case_id,supplier_id,supplier_contact_id,session_name,
                       external_chat_id,idempotency_key
                   ) VALUES($1,$2,$3,$4,$5,'77000000003@c.us',$6)""",
                conversation_id,
                case_id,
                supplier_id,
                contact_id,
                _IdentityClient.session_name,
                f"identity-conversation:{conversation_id}",
            )
            await connection.execute(
                "UPDATE supplier_conversations SET state='approved' WHERE id=$1",
                conversation_id,
            )
            await connection.execute(
                """INSERT INTO workflow_events(case_id,event_type,actor_type,data,idempotency_key)
                   VALUES($1,'supplier_contact_enrichment_result','system',$2::jsonb,'synthetic-provenance')""",
                case_id,
                {"supplier_id": str(supplier_id), "contact_evidence_ids": [str(evidence_id)]},
            )
        yield pool, owner_id, conversation_id, contact_id
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pin_preserves_global_gates_and_legacy_unknown_null_identity(identity_pg) -> None:
    pool, owner_id, conversation_id, contact_id = identity_pg
    from procurement_bot.whatsapp_account import ensure_account

    event_id = uuid4()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await ensure_account(connection, _IdentityClient.session_name)
            await connection.execute(
                """UPDATE whatsapp_account_controls SET operator_held=TRUE,
                       circuit_open=TRUE,circuit_reason='preserve_this_hold'
                   WHERE session_name=$1""",
                _IdentityClient.session_name,
            )
            await connection.execute(
                """INSERT INTO outbox_events(id,event_type,idempotency_key)
                   VALUES($1,'identity_test_action',$2)""",
                event_id,
                f"identity-outbox:{event_id}",
            )
            await connection.execute(
                """INSERT INTO whatsapp_account_send_reservations(
                       outbox_event_id,session_name,conversation_id,supplier_contact_id,
                       state,is_new_contact,reserved_at,outcome_source
                   ) VALUES($1,$2,$3,$4,'unknown',TRUE,now(),'migration')""",
                event_id,
                _IdentityClient.session_name,
                conversation_id,
                contact_id,
            )

    version = await pin_whatsapp_account_identity(
        pool,
        _IdentityClient("77000000003@c.us"),
        actor_user_id=owner_id,
        expected_account_jid="77000000003@c.us",
        reason="owner_confirmed_identity_test",
    )

    assert version == 1
    async with pool.acquire() as connection:
        account = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1",
            _IdentityClient.session_name,
        )
        legacy = await connection.fetchrow(
            "SELECT pinned_account_jid,account_pin_version,observed_account_jid "
            "FROM whatsapp_account_send_reservations WHERE outbox_event_id=$1",
            event_id,
        )
        assert account["pinned_account_jid"] == "77000000003@c.us"
        assert account["pin_version"] == 1
        assert account["operator_held"] is True
        assert account["circuit_open"] is True
        assert account["circuit_reason"] == "preserve_this_hold"
        assert legacy["pinned_account_jid"] is None
        assert legacy["account_pin_version"] is None
        assert legacy["observed_account_jid"] is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pin_requires_live_identity_match_and_owner_authorization(identity_pg) -> None:
    pool, owner_id, _conversation_id, _contact_id = identity_pg
    with pytest.raises(PermissionError, match="differs"):
        await pin_whatsapp_account_identity(
            pool,
            _IdentityClient("77000000003@c.us"),
            actor_user_id=owner_id,
            expected_account_jid="77000000004@c.us",
            reason="mismatched_owner_confirmation",
        )

    class UnownedClient(_IdentityClient):
        called = False

        async def get_account_identity(self) -> WahaAccountIdentity:
            self.called = True
            return self.identity

    client = UnownedClient("77000000003@c.us")
    with pytest.raises(PermissionError, match="own the active cases"):
        await pin_whatsapp_account_identity(
            pool,
            client,
            actor_user_id=uuid4(),
            expected_account_jid="77000000003@c.us",
            reason="unowned_actor_must_not_pin",
        )
    assert client.called is False


SELF_JID = "77000000003@c.us"  # Synthetic identity, never a customer contact.


async def _open_account(context):
    pool, owner, _conversation, _contact = context
    await pin_whatsapp_account_identity(
        pool,
        _IdentityClient(SELF_JID),
        actor_user_id=owner,
        expected_account_jid=SELF_JID,
        reason="synthetic_test_confirmation",
    )
    await pool.execute(
        """UPDATE whatsapp_account_controls SET operator_held=FALSE,
           provider_state='clear',provider_observed_at=now(),daily_message_limit=30,
           daily_new_contact_limit=10,minimum_interval_seconds=0 WHERE session_name=$1""",
        _IdentityClient.session_name,
    )


@pytest.fixture
async def daytime_policy(monkeypatch):
    from procurement_bot.supplier_dialogue import SendPolicy

    # Only move the quiet window away from the test clock; all native gates run.
    start = (datetime.now(UTC) + timedelta(hours=2)).time().replace(tzinfo=None)
    end = (datetime.now(UTC) + timedelta(hours=3)).time().replace(tzinfo=None)
    policy = SendPolicy(timezone="UTC", quiet_start=start, quiet_end=end)

    async def load_policy(*_args):
        return policy

    monkeypatch.setattr("procurement_bot.whatsapp_delivery.get_case_send_policy", load_policy)
    return policy


async def _seed_event(context, *, status="pending", provider_request_id=None):
    from procurement_bot.approvals import (
        approve_request,
        request_approval,
        require_and_consume_approval,
    )
    from procurement_bot.queue import ClaimedOutbox

    pool, owner, conversation, _contact = context
    event_id = uuid4()
    async with pool.acquire() as connection, connection.transaction():
        case_id = await connection.fetchval(
            "SELECT case_id FROM supplier_conversations WHERE id=$1", conversation
        )
        payload = {
            "case_id": str(case_id),
            "channel": "whatsapp",
            "recipient": "+77000000003",
            "text": "Synthetic availability request",
            "conversation_id": str(conversation),
            "logical_action_id": str(event_id),
            "is_followup": False,
        }
        approval = await request_approval(
            connection,
            case_id=case_id,
            action_type="send_whatsapp",
            payload=payload,
        )
        assert await approve_request(connection, approval_id=approval, approved_by=owner)
        await require_and_consume_approval(
            connection,
            case_id=case_id,
            action_type="send_whatsapp",
            payload=payload,
        )
        row = await connection.fetchrow(
            """INSERT INTO outbox_events(
                   id,event_type,payload,status,idempotency_key,approval_id,locked_by,
                   locked_at,lease_expires_at,provider_request_id
               ) VALUES($1,'whatsapp.send_text',$2,$3,$4,$5,'identity-worker',now(),
                        now()+interval '5 minutes',$6) RETURNING *""",
            event_id,
            payload,
            status,
            f"identity-action:{event_id}",
            approval,
            provider_request_id,
        )
    return ClaimedOutbox(**{key: row[key] for key in ClaimedOutbox.__dataclass_fields__})


class _WorkerTransport:
    """No network: strict fake WAHA transport used at the real worker boundary."""

    def __init__(self, pool, event, *, me_status=200, me_body=None, hook=None):
        self.pool = pool
        self.event = event
        self.me_status = me_status
        self.me_body = me_body if me_body is not None else json.dumps({"id": SELF_JID}).encode()
        self.calls = []
        self.hook = hook

    async def request(self, *, method, url, **_kwargs):
        from procurement_bot.providers.waha import HttpResponse

        self.calls.append((method, url))
        if url.endswith("/me"):
            if isinstance(self.me_status, Exception):
                raise self.me_status
            return HttpResponse(self.me_status, self.me_body)
        if url.endswith("/new-message-id"):
            if self.hook:
                await self.hook()
            return HttpResponse(200, b'{"id":"synthetic-request"}')
        if url.endswith("/sendText"):
            assert method == "POST"
            row = await self.pool.fetchrow(
                "SELECT * FROM whatsapp_account_send_reservations WHERE outbox_event_id=$1",
                self.event.id,
            )
            assert row["state"] == "reserved"
            assert row["pinned_account_jid"] == row["observed_account_jid"] == SELF_JID
            assert row["account_pin_version"] == 1
            assert row["account_identity_observed_at"] is not None
            return HttpResponse(200, b'{"id":"synthetic-accepted"}')
        raise AssertionError(f"unexpected fake transport path: {method} {url}")

    @property
    def posts(self):
        return sum(method == "POST" for method, _url in self.calls)


def _worker(pool, transport, *, client=None):
    from procurement_bot.outbox import WhatsAppOutboxWorker
    from procurement_bot.providers.waha import WahaClient

    return WhatsAppOutboxWorker(
        pool=pool,
        worker_id="identity-worker",
        client=client
        or WahaClient(
            base_url="http://waha.invalid",
            api_key="synthetic",
            enabled=True,
            session=_IdentityClient.session_name,
            transport=transport,
        ),
    )


async def _due(pool, event):
    await pool.execute("UPDATE outbox_events SET available_at=now() WHERE id=$1", event.id)


@pytest.mark.postgres
@pytest.mark.parametrize("failure", [503, 429, TimeoutError("synthetic timeout")])
async def test_worker_identity_transient_defers_then_posts_once(
    identity_pg, daytime_policy, failure
):
    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    transport = _WorkerTransport(pool, event, me_status=failure)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    row = await pool.fetchrow("SELECT * FROM outbox_events WHERE id=$1", event.id)
    assert row["status"] == "retry" and row["provider_request_id"] is None
    assert row["attempts"] == 0
    assert not await pool.fetchval("SELECT count(*) FROM whatsapp_account_send_reservations")
    assert transport.posts == 0 and len(transport.calls) == 1
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM whatsapp_account_control_events "
            "WHERE event_type='account_identity_check_failed' AND outbox_event_id=$1",
            event.id,
        )
        == 1
    )
    transport.me_status = 200
    await _due(pool, event)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    assert transport.posts == 1
    assert not await _worker(pool, transport).run_once(event_id=event.id)
    journal = await pool.fetchrow("SELECT * FROM whatsapp_send_acceptance_journal")
    reserved = await pool.fetchrow("SELECT * FROM whatsapp_account_send_reservations")
    assert journal["account_jid"] == reserved["observed_account_jid"] == SELF_JID
    assert journal["account_pin_version"] == reserved["account_pin_version"] == 1
    assert journal["account_identity_observed_at"] == reserved["account_identity_observed_at"]
    assert reserved["state"] == "accepted"


@pytest.mark.postgres
@pytest.mark.parametrize(
    "body,mismatch",
    [
        (b"null", False),
        (b"{}", False),
        (b'{"id":"bad"}', False),
        (b'{"id":"77000000004@c.us"}', True),
    ],
)
async def test_worker_invalid_or_changed_self_identity_is_retryable(
    identity_pg,
    daytime_policy,
    body,
    mismatch,
):
    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    transport = _WorkerTransport(pool, event, me_body=body)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    row = await pool.fetchrow("SELECT * FROM outbox_events WHERE id=$1", event.id)
    assert row["status"] == "retry" and row["provider_request_id"] is None
    assert not await pool.fetchval("SELECT count(*) FROM whatsapp_account_send_reservations")
    assert len(transport.calls) == 1 and transport.posts == 0
    audit_type = "account_identity_mismatch" if mismatch else "account_identity_check_failed"
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM whatsapp_account_control_events "
            "WHERE event_type=$1 AND outbox_event_id=$2",
            audit_type,
            event.id,
        )
        == 1
    )
    # Neither denial changes holds/circuit (checked with set values below too).
    assert not await pool.fetchval("SELECT circuit_open FROM whatsapp_account_controls")


@pytest.mark.postgres
async def test_worker_missing_pin_blocks_before_any_provider_call(identity_pg, daytime_policy):
    from procurement_bot.whatsapp_account import ensure_account

    pool = identity_pg[0]
    async with pool.acquire() as connection:
        await ensure_account(connection, _IdentityClient.session_name)
    await pool.execute(
        "UPDATE whatsapp_account_controls SET operator_held=FALSE,"
        "provider_state='clear',provider_observed_at=now()"
    )
    event = await _seed_event(identity_pg)
    transport = _WorkerTransport(pool, event)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    row = await pool.fetchrow("SELECT * FROM outbox_events WHERE id=$1", event.id)
    assert row["status"] == "retry" and row["provider_request_id"] is None
    assert row["last_error_code"] == "whatsapp_account_identity_not_pinned"
    assert not transport.calls


@pytest.mark.postgres
@pytest.mark.parametrize("age_seconds", [31, -1])
async def test_worker_rejects_stale_or_future_observation(identity_pg, daytime_policy, age_seconds):
    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    transport = _WorkerTransport(pool, event)
    client = _worker(pool, transport).client

    async def identity():
        return WahaAccountIdentity(SELF_JID, datetime.now(UTC) - timedelta(seconds=age_seconds))

    client.get_account_identity = identity
    assert await _worker(pool, transport, client=client).run_once(event_id=event.id)
    row = await pool.fetchrow("SELECT * FROM outbox_events WHERE id=$1", event.id)
    assert row["status"] == "retry" and row["provider_request_id"] is None
    assert row["last_error_code"] == "whatsapp_account_identity_observation_stale"
    assert not await pool.fetchval("SELECT count(*) FROM whatsapp_account_send_reservations")
    assert transport.posts == 0


@pytest.mark.postgres
@pytest.mark.parametrize("change", ["pin", "lease", "session"])
async def test_reservation_revalidates_pin_session_and_outbox_lease(
    identity_pg, daytime_policy, change
):
    pool, owner, conversation, _contact = identity_pg
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)

    async def change_before_reservation():
        if change == "pin":
            await pin_whatsapp_account_identity(
                pool,
                _IdentityClient(SELF_JID),
                actor_user_id=owner,
                expected_account_jid=SELF_JID,
                reason="synthetic_pin_version_change",
            )
        elif change == "session":
            await pool.execute(
                "UPDATE supplier_conversations SET session_name='other-test-session' WHERE id=$1",
                conversation,
            )
        else:
            await pool.execute(
                "UPDATE outbox_events SET locked_by='replacement-worker' WHERE id=$1",
                event.id,
            )

    transport = _WorkerTransport(pool, event, hook=change_before_reservation)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    assert transport.posts == 0
    row = await pool.fetchrow("SELECT * FROM outbox_events WHERE id=$1", event.id)
    assert row["provider_request_id"] is None
    assert not await pool.fetchval("SELECT count(*) FROM whatsapp_account_send_reservations")


@pytest.mark.postgres
async def test_durable_acceptance_survives_projection_failure_and_process_restart(
    identity_pg,
    daytime_policy,
    monkeypatch,
):
    from procurement_bot.whatsapp_delivery import complete_whatsapp_delivery

    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    transport = _WorkerTransport(pool, event)

    async def broken_projection(*_args, **_kwargs):
        raise RuntimeError("synthetic projection failure after durable acceptance")

    monkeypatch.setattr("procurement_bot.outbox.complete_whatsapp_delivery", broken_projection)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    journal = await pool.fetchrow("SELECT * FROM whatsapp_send_acceptance_journal")
    assert journal["account_jid"] == SELF_JID
    assert await pool.fetchval("SELECT state FROM whatsapp_account_send_reservations") == "accepted"
    assert await pool.fetchval("SELECT circuit_open FROM whatsapp_account_controls")
    assert not await pool.fetchval("SELECT count(*) FROM supplier_conversation_messages")
    before = list(transport.calls)
    monkeypatch.setattr(
        "procurement_bot.outbox.complete_whatsapp_delivery", complete_whatsapp_delivery
    )
    await _due(pool, event)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    assert transport.calls == before and transport.posts == 1
    assert await pool.fetchval("SELECT count(*) FROM supplier_conversation_messages") == 1
    assert await pool.fetchval("SELECT status FROM outbox_events WHERE id=$1", event.id) == "sent"
    assert not await _worker(pool, transport).run_once(event_id=event.id)


async def _insert_legacy_reservation(context, event, *, state="unknown"):
    pool, _owner, conversation, contact = context
    from procurement_bot.whatsapp_account import ensure_account

    async with pool.acquire() as connection:
        await ensure_account(connection, _IdentityClient.session_name)
        await connection.execute(
            """INSERT INTO whatsapp_account_send_reservations(
                   outbox_event_id,session_name,conversation_id,supplier_contact_id,
                   state,is_new_contact,reserved_at,accepted_at,outcome_source
               ) VALUES($1,$2,$3,$4,$5,TRUE,now(),$6,'migration')""",
            event.id,
            _IdentityClient.session_name,
            conversation,
            contact,
            state,
            datetime.now(UTC) if state == "accepted" else None,
        )


@pytest.mark.postgres
@pytest.mark.parametrize("identity_pg", ["pre043"], indirect=True)
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_real_pre043_null_identity_rows_migrate_and_replay_without_provider(
    identity_pg,
    outcome,
):
    from procurement_bot.whatsapp_send_journal import load_acceptance, payload_sha256

    pool = identity_pg[0]
    event = await _seed_event(identity_pg, provider_request_id="old-physical-request")
    await _insert_legacy_reservation(identity_pg, event, state=outcome)
    if outcome == "accepted":
        digest = payload_sha256(
            session_name=_IdentityClient.session_name,
            external_chat_id=SELF_JID,
            text=event.payload["text"],
            idempotency_key=event.idempotency_key,
            provider_request_id=event.provider_request_id,
        )
        await pool.execute(
            """INSERT INTO whatsapp_send_acceptance_journal(
                   outbox_event_id,idempotency_key,session_name,external_chat_id,
                   provider_request_id,payload_sha256,external_message_id,source,
                   result_identity,accepted_at
               ) VALUES($1,$2,$3,$4,$5,$6,'old-accepted','http',$7,now())""",
            event.id,
            event.idempotency_key,
            _IdentityClient.session_name,
            SELF_JID,
            event.provider_request_id,
            digest,
            {"external_message_id": "old-accepted", "accepted": True, "provider_result": {}},
        )
    await pool.execute(
        "UPDATE whatsapp_account_controls SET operator_held=TRUE,circuit_open=TRUE,"
        "circuit_reason='unresolved_historical_provider_outcome'"
    )
    assert await run_migrations(pool, Path(__file__).parents[1] / "migrations") == [
        "043_whatsapp_account_identity_binding.sql"
    ]
    # A migrated worker starts with fresh connection/statement caches.
    await pool.expire_connections()
    reservation = await pool.fetchrow("SELECT * FROM whatsapp_account_send_reservations")
    assert all(
        reservation[key] is None
        for key in (
            "pinned_account_jid",
            "account_pin_version",
            "observed_account_jid",
            "account_identity_observed_at",
        )
    )
    async with pool.acquire() as connection:
        loaded = await load_acceptance(connection, event)
    if outcome == "accepted":
        assert loaded is not None
        assert all(
            loaded[0][key] is None
            for key in (
                "account_jid",
                "account_pin_version",
                "account_identity_observed_at",
            )
        )
    else:
        assert loaded is None
    # Initial pin for a different currently linked account never backfills old actions.
    await pin_whatsapp_account_identity(
        pool,
        _IdentityClient("77000000004@c.us"),
        actor_user_id=identity_pg[1],
        expected_account_jid="77000000004@c.us",
        reason="synthetic_new_account",
    )
    transport = _WorkerTransport(pool, event)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    assert not transport.calls
    reservation = await pool.fetchrow("SELECT * FROM whatsapp_account_send_reservations")
    assert all(
        reservation[key] is None
        for key in (
            "pinned_account_jid",
            "account_pin_version",
            "observed_account_jid",
            "account_identity_observed_at",
        )
    )
    assert await pool.fetchval("SELECT operator_held FROM whatsapp_account_controls")
    row = await pool.fetchrow("SELECT * FROM outbox_events WHERE id=$1", event.id)
    if outcome == "accepted":
        assert row["status"] == "sent"
        assert await pool.fetchval("SELECT count(*) FROM supplier_conversation_messages") == 1
        journal = await pool.fetchrow("SELECT * FROM whatsapp_send_acceptance_journal")
        assert all(
            journal[key] is None
            for key in (
                "account_jid",
                "account_pin_version",
                "account_identity_observed_at",
            )
        )
    else:
        assert row["status"] == "retry"
        assert row["provider_request_id"] == "old-physical-request"
        assert await pool.fetchval("SELECT circuit_open FROM whatsapp_account_controls")
        assert reservation["state"] == "unknown"
        assert not await pool.fetchval("SELECT count(*) FROM supplier_conversation_messages")


def _partial_tuples(values):
    for mask in itertools.product((False, True), repeat=len(values)):
        if any(mask) and not all(mask):
            yield tuple(
                value if included else None for included, value in zip(mask, values, strict=True)
            )


@pytest.mark.postgres
async def test_043_rejects_every_partial_null_tuple_and_invalid_identity(identity_pg):
    from procurement_bot.whatsapp_account import ensure_account

    pool, owner, _conversation, _contact = identity_pg
    event = await _seed_event(identity_pg)
    await _insert_legacy_reservation(identity_pg, event)
    stamp = datetime.now(UTC)
    async with pool.acquire() as connection:
        await ensure_account(connection, _IdentityClient.session_name)
        control_sql = (
            "UPDATE whatsapp_account_controls SET pinned_account_jid=$1,pinned_by=$2,"
            "pinned_at=$3,pin_observed_at=$4,pin_version=$5"
        )
        controls = (SELF_JID, owner, stamp, stamp, 1)
        for values in _partial_tuples(controls):
            # The legacy pin version is zero rather than null; all other null holes
            # still have to fail. Test a null version separately via NOT NULL too.
            values = (*values[:4], values[4] if values[4] is not None else 0)
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(control_sql, *values)
        with pytest.raises(asyncpg.NotNullViolationError):
            await connection.execute(control_sql, *controls[:4], None)
        for values in [("bad-jid", owner, stamp, stamp, 1), (*controls[:4], 0)]:
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(control_sql, *values)
        await connection.execute(control_sql, *controls)

        reservation_sql = (
            "UPDATE whatsapp_account_send_reservations SET pinned_account_jid=$1,"
            "account_pin_version=$2,observed_account_jid=$3,account_identity_observed_at=$4"
        )
        action = (SELF_JID, 1, SELF_JID, stamp)
        for values in _partial_tuples(action):
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(reservation_sql, *values)
        for values in [
            ("bad", 1, SELF_JID, stamp),
            (SELF_JID, 0, SELF_JID, stamp),
            (SELF_JID, 1, "bad", stamp),
            (SELF_JID, 1, "77000000004@c.us", stamp),
        ]:
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(reservation_sql, *values)
        await connection.execute(reservation_sql, *action)

        journal_sql = """INSERT INTO whatsapp_send_acceptance_journal(
            outbox_event_id,idempotency_key,session_name,external_chat_id,provider_request_id,
            payload_sha256,external_message_id,source,result_identity,accepted_at,
            account_jid,account_pin_version,account_identity_observed_at
        ) VALUES($1,$2,$3,$4,'synthetic',repeat('a',64),
                 'synthetic','http','{}',now(),$5,$6,$7)"""
        base = (event.id, event.idempotency_key, _IdentityClient.session_name, SELF_JID)
        journal = (SELF_JID, 1, stamp)
        for values in [*_partial_tuples(journal), ("bad", 1, stamp), (SELF_JID, 0, stamp)]:
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(journal_sql, *base, *values)
        await connection.execute(journal_sql, *base, *journal)


@pytest.mark.postgres
async def test_http_acceptance_requires_exact_reserved_identity_and_pin_update_is_serialized(
    identity_pg,
    daytime_policy,
):
    from procurement_bot.chat_actors import claim_whatsapp_outbox
    from procurement_bot.errors import PermanentProviderError
    from procurement_bot.providers.waha import WahaSendResult
    from procurement_bot.whatsapp_account import mark_outcome, reserve_outbound
    from procurement_bot.whatsapp_send_journal import persist_acceptance

    pool, owner, _conversation, _contact = identity_pg
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    event, _actor = await claim_whatsapp_outbox(pool, worker_id="identity-worker", lease_seconds=60)
    observation = WahaAccountIdentity(SELF_JID, datetime.now(UTC))
    reservation = await reserve_outbound(
        pool,
        event,
        provider_request_id="physical-id",
        worker_id="identity-worker",
        identity_observation=observation,
        expected_account_jid=SELF_JID,
        expected_pin_version=1,
        expected_session_name=_IdentityClient.session_name,
    )
    assert reservation is not None
    with pytest.raises(PermissionError, match="reservation is active"):
        await pin_whatsapp_account_identity(
            pool,
            _IdentityClient(SELF_JID),
            actor_user_id=owner,
            expected_account_jid=SELF_JID,
            reason="must_serialize_with_action",
        )
    async with pool.acquire() as connection:
        for jid, version, stamp in [
            ("77000000004@c.us", 1, observation.observed_at),
            (SELF_JID, 2, observation.observed_at),
            (SELF_JID, 1, observation.observed_at + timedelta(microseconds=1)),
        ]:
            with pytest.raises(PermanentProviderError, match="reserved physical action"):
                async with connection.transaction():
                    await persist_acceptance(
                        connection,
                        event,
                        session_name=_IdentityClient.session_name,
                        external_chat_id=SELF_JID,
                        provider_request_id="physical-id",
                        result=WahaSendResult("physical-id", {}),
                        source="http",
                        account_jid=jid,
                        account_pin_version=version,
                        account_identity_observed_at=stamp,
                    )
            assert not await connection.fetchval(
                "SELECT count(*) FROM whatsapp_send_acceptance_journal"
            )
        # An unknown network result keeps its exact physical identity and hold/circuit.
        await connection.execute(
            "UPDATE whatsapp_account_controls SET operator_held=TRUE,circuit_open=TRUE,"
            "circuit_reason='synthetic_preserved_circuit'"
        )
        await mark_outcome(connection, event.id, state="unknown", reason="synthetic_timeout")
        row = await connection.fetchrow("SELECT * FROM whatsapp_account_send_reservations")
        assert row["state"] == "unknown"
        assert row["observed_account_jid"] == row["pinned_account_jid"] == SELF_JID
        assert row["account_pin_version"] == 1
        assert row["account_identity_observed_at"] == observation.observed_at
        assert await connection.fetchval("SELECT operator_held FROM whatsapp_account_controls")
        assert await connection.fetchval("SELECT circuit_open FROM whatsapp_account_controls")


@pytest.mark.postgres
@pytest.mark.parametrize(
    "gate",
    [
        "quiet",
        "opt_out",
        "exact_approval",
        "session",
        "budget",
        "new_contacts",
        "hold",
        "circuit",
    ],
)
async def test_worker_keeps_existing_delivery_and_account_gates(
    identity_pg,
    daytime_policy,
    monkeypatch,
    gate,
):
    from datetime import time

    from procurement_bot.supplier_dialogue import SendPolicy

    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    if gate == "quiet":

        async def quiet(*_args):
            return SendPolicy(quiet_start=time(0), quiet_end=time(0))

        monkeypatch.setattr("procurement_bot.whatsapp_delivery.get_case_send_policy", quiet)
    elif gate == "opt_out":
        await pool.execute(
            "UPDATE supplier_conversations SET opted_out=TRUE,state='opted_out',opted_out_at=now()"
        )
    elif gate == "exact_approval":
        await pool.execute(
            "UPDATE outbox_events SET payload=jsonb_set(payload,'{text}',to_jsonb($2::text)) "
            "WHERE id=$1",
            event.id,
            "Changed after approval",
        )
    elif gate == "budget":
        await pool.execute("UPDATE whatsapp_account_controls SET daily_message_limit=0")
    elif gate == "new_contacts":
        await pool.execute("UPDATE whatsapp_account_controls SET daily_new_contact_limit=0")
    elif gate == "hold":
        await pool.execute("UPDATE whatsapp_account_controls SET operator_held=TRUE")
    elif gate == "circuit":
        await pool.execute("UPDATE whatsapp_account_controls SET circuit_open=TRUE")
    transport = _WorkerTransport(pool, event)
    client = _worker(pool, transport).client
    if gate == "session":
        from procurement_bot.providers.waha import WahaClient

        client = WahaClient(
            base_url="http://waha.invalid",
            api_key="synthetic",
            enabled=True,
            session="wrong-test-session",
            transport=transport,
        )
    assert await _worker(pool, transport, client=client).run_once(event_id=event.id)
    assert transport.posts == 0
    assert not await pool.fetchval("SELECT count(*) FROM whatsapp_account_send_reservations")
    assert (
        await pool.fetchval(
            "SELECT provider_request_id FROM outbox_events WHERE id=$1",
            event.id,
        )
        is None
    )


@pytest.mark.postgres
async def test_mismatch_audit_never_clears_concurrently_added_hold_or_circuit(
    identity_pg, daytime_policy
):
    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    transport = _WorkerTransport(pool, event)
    client = _worker(pool, transport).client

    async def identity():
        await pool.execute(
            "UPDATE whatsapp_account_controls SET operator_held=TRUE,circuit_open=TRUE,"
            "circuit_reason='synthetic_race_hold'"
        )
        return WahaAccountIdentity("77000000004@c.us", datetime.now(UTC))

    client.get_account_identity = identity
    assert await _worker(pool, transport, client=client).run_once(event_id=event.id)
    account = await pool.fetchrow("SELECT * FROM whatsapp_account_controls")
    assert account["operator_held"] and account["circuit_open"]
    assert account["circuit_reason"] == "synthetic_race_hold"
    assert not transport.calls
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM whatsapp_account_control_events "
            "WHERE event_type='account_identity_mismatch' AND outbox_event_id=$1",
            event.id,
        )
        == 1
    )


@pytest.mark.postgres
async def test_reserved_crash_is_unknown_on_restart_and_never_reposts(identity_pg, daytime_policy):
    from procurement_bot.chat_actors import claim_whatsapp_outbox
    from procurement_bot.whatsapp_account import reserve_outbound

    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)
    event, _actor = await claim_whatsapp_outbox(pool, worker_id="identity-worker", lease_seconds=60)
    observed = WahaAccountIdentity(SELF_JID, datetime.now(UTC))
    await reserve_outbound(
        pool,
        event,
        provider_request_id="crashed-action",
        worker_id="identity-worker",
        identity_observation=observed,
        expected_account_jid=SELF_JID,
        expected_pin_version=1,
        expected_session_name=_IdentityClient.session_name,
    )
    # Recover a due, already-reserved action after process loss. The external
    # account differs: there is no history GET under that account and no POST.
    await pool.execute(
        "UPDATE outbox_events SET status='retry',locked_by=NULL,locked_at=NULL,"
        "lease_expires_at=NULL,available_at=now() WHERE id=$1",
        event.id,
    )
    await pool.execute("UPDATE chat_actor_leases SET owner_worker_id=NULL,lease_expires_at=NULL")
    transport = _WorkerTransport(pool, event, me_body=b'{"id":"77000000004@c.us"}')
    assert await _worker(pool, transport).run_once(event_id=event.id)
    row = await pool.fetchrow("SELECT * FROM whatsapp_account_send_reservations")
    assert row["state"] == "unknown"
    assert row["observed_account_jid"] == row["pinned_account_jid"] == SELF_JID
    assert row["account_identity_observed_at"] == observed.observed_at
    assert row["account_pin_version"] == 1
    assert transport.posts == 0 and len(transport.calls) == 1
    assert transport.calls[0][1].endswith("/me")
    assert await pool.fetchval("SELECT circuit_open FROM whatsapp_account_controls")


@pytest.mark.postgres
async def test_worker_post_timeout_keeps_durable_identity_unknown(identity_pg, daytime_policy):
    pool = identity_pg[0]
    await _open_account(identity_pg)
    event = await _seed_event(identity_pg)

    class TimedOutTransport(_WorkerTransport):
        async def request(self, *, method, url, **kwargs):
            result = await super().request(method=method, url=url, **kwargs)
            if method == "POST":
                raise TimeoutError("synthetic timeout after possible provider acceptance")
            return result

    transport = TimedOutTransport(pool, event)
    assert await _worker(pool, transport).run_once(event_id=event.id)
    row = await pool.fetchrow("SELECT * FROM whatsapp_account_send_reservations")
    assert row["state"] == "unknown"
    assert row["pinned_account_jid"] == row["observed_account_jid"] == SELF_JID
    assert row["account_pin_version"] == 1
    assert row["account_identity_observed_at"] is not None
    assert transport.posts == 1
    assert await pool.fetchval("SELECT circuit_open FROM whatsapp_account_controls")
    assert not await pool.fetchval("SELECT count(*) FROM whatsapp_send_acceptance_journal")
    assert (
        await pool.fetchval(
            "SELECT provider_request_id FROM outbox_events WHERE id=$1",
            event.id,
        )
        == "synthetic-request"
    )


@pytest.mark.postgres
async def test_outcome_waits_for_account_before_locking_action_reservation(
    identity_pg, daytime_policy
):
    import asyncio

    from procurement_bot.chat_actors import claim_whatsapp_outbox
    from procurement_bot.whatsapp_account import mark_outcome, reserve_outbound

    pool = identity_pg[0]
    await _open_account(identity_pg)
    await _seed_event(identity_pg)
    event, _actor = await claim_whatsapp_outbox(pool, worker_id="identity-worker", lease_seconds=60)
    await reserve_outbound(
        pool,
        event,
        provider_request_id="lock-order-test",
        worker_id="identity-worker",
        identity_observation=WahaAccountIdentity(SELF_JID, datetime.now(UTC)),
        expected_account_jid=SELF_JID,
        expected_pin_version=1,
        expected_session_name=_IdentityClient.session_name,
    )
    started = asyncio.Event()

    async def outcome():
        async with pool.acquire() as connection:
            await connection.execute("SET application_name='identity-lock-order-test'")
            started.set()
            await mark_outcome(connection, event.id, state="unknown", reason="synthetic_crash")

    pending = None
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            await blocker.fetchrow("SELECT * FROM whatsapp_account_controls FOR UPDATE")
            pending = asyncio.create_task(outcome())
            await started.wait()
            for _attempt in range(200):
                waiting = await blocker.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name='identity-lock-order-test' AND wait_event_type='Lock')"
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting, "outcome must actually be waiting for the account lock"
            # With the old action->account order this NOWAIT would fail; releasing
            # the account here also proves the two writers can complete.
            await blocker.fetchrow(
                "SELECT * FROM whatsapp_account_send_reservations "
                "WHERE outbox_event_id=$1 FOR UPDATE NOWAIT",
                event.id,
            )
        await asyncio.wait_for(pending, timeout=5)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
