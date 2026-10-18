from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from procurement_bot.chat_actors import ChatActorLease
from procurement_bot.db import run_migrations
from procurement_bot.errors import RetryableProviderError
from procurement_bot.outbox import (
    TelegramOutboxWorker,
    WhatsAppOutboxWorker,
    WhatsAppSendOutcomeUnknown,
)
from procurement_bot.providers.waha import WahaAccountIdentity, WahaSendResult
from procurement_bot.queue import ClaimedOutbox
from procurement_bot.supplier_dialogue import WhatsAppSendDeferred
from procurement_bot.whatsapp_account import (
    WhatsAppAccountIdentityPin,
    WhatsAppAccountReservation,
)
from procurement_bot.whatsapp_send_journal import (
    exact_recovered_message,
    load_acceptance,
    persist_acceptance,
)


class _FakePool:
    def acquire(self):
        @asynccontextmanager
        async def manager():
            class Connection:
                async def fetchrow(self, *_args: object, **_kwargs: object):
                    return None

                def transaction(self):
                    @asynccontextmanager
                    async def transaction_manager():
                        yield

                    return transaction_manager()

            yield Connection()

        return manager()


def test_exact_message_reconciliation_requires_id_chat_ownership_and_text() -> None:
    recovered = exact_recovered_message(
        {
            "id": "true_77000000003@c.us_reserved-42",
            "fromMe": True,
            "chatId": "77000000003@c.us",
            "body": "Здравствуйте",
        },
        expected_chat_id="77000000003@c.us",
        provider_request_id="reserved-42",
        expected_text="Здравствуйте",
    )
    assert recovered is not None
    assert recovered.external_message_id == "true_77000000003@c.us_reserved-42"
    canonical_full_id = exact_recovered_message(
        {
            "id": {"_serialized": "true_77000000003@c.us_reserved-42"},
            "fromMe": True,
            "body": "Здравствуйте",
        },
        expected_chat_id="77000000003@c.us",
        provider_request_id="reserved-42",
        expected_text="Здравствуйте",
    )
    assert canonical_full_id is not None

    bare_without_chat = exact_recovered_message(
        {"id": "reserved-42", "fromMe": True, "body": "Здравствуйте"},
        expected_chat_id="77000000003@c.us",
        provider_request_id="reserved-42",
        expected_text="Здравствуйте",
    )
    assert bare_without_chat is None

    for invalid in (
        {
            "id": "reserved-42",
            "fromMe": False,
            "chatId": "77000000003@c.us",
            "body": "Здравствуйте",
        },
        {"id": "reserved-42", "fromMe": True, "chatId": "77000000010@c.us", "body": "Здравствуйте"},
        {
            "id": "reserved-42",
            "fromMe": True,
            "chatId": "77000000003@c.us",
            "body": "different text",
        },
    ):
        assert (
            exact_recovered_message(
                invalid,
                expected_chat_id="77000000003@c.us",
                provider_request_id="reserved-42",
                expected_text="Здравствуйте",
            )
            is None
        )


@pytest.mark.asyncio
async def test_whatsapp_acceptance_journal_persists_and_is_immutable() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_waha_journal_{uuid4().hex}"
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
        max_size=2,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        event_id = uuid4()
        approval_id = uuid4()
        owner_id = uuid4()
        case_id = uuid4()
        supplier_id = uuid4()
        evidence_id = uuid4()
        contact_id = uuid4()
        conversation_id = uuid4()
        request_id = "reserved-42"
        key = f"event:{uuid4().hex}"
        accepted_at = datetime.now(UTC)
        payload = {
            "case_id": str(uuid4()),
            "channel": "whatsapp",
            "recipient": "+77000000003",
            "text": "Здравствуйте",
            "conversation_id": str(uuid4()),
            "logical_action_id": "availability-v1",
            "is_followup": False,
        }
        payload["conversation_id"] = str(conversation_id)
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO users(id,telegram_id) VALUES($1,$2)",
                owner_id,
                int(uuid4().int % 2_000_000_000) + 1,
            )
            await connection.execute(
                "INSERT INTO procurement_cases(id,owner_user_id) VALUES($1,$2)",
                case_id,
                owner_id,
            )
            await connection.execute(
                """INSERT INTO source_evidence(
                       source_type,source_locator,captured_at,content_sha256,raw_payload,
                       idempotency_key
                   ) VALUES('manual','journal-test',now(),$1,'{}'::jsonb,$2)""",
                "a" * 64,
                f"journal-evidence:{evidence_id}",
            )
            await connection.execute(
                "INSERT INTO suppliers(id,display_name) VALUES($1,'Journal test supplier')",
                supplier_id,
            )
            await connection.execute(
                """INSERT INTO supplier_contacts(
                       id,supplier_id,contact_type,display_value,normalized_value,
                       source_evidence_id
                   ) SELECT $1,$2,'phone','+77000000003','77000000003',id
                     FROM source_evidence WHERE idempotency_key=$3""",
                contact_id,
                supplier_id,
                f"journal-evidence:{evidence_id}",
            )
            await connection.execute(
                """INSERT INTO supplier_conversations(
                       id,case_id,supplier_id,supplier_contact_id,session_name,
                       external_chat_id,idempotency_key
                   ) VALUES($1,$2,$3,$4,'default','77000000003@c.us',$5)""",
                conversation_id,
                case_id,
                supplier_id,
                contact_id,
                f"journal-conversation:{conversation_id}",
            )
            payload["case_id"] = str(case_id)
            await connection.execute(
                """INSERT INTO action_approvals(
                    id,case_id,action_type,payload,payload_sha256
                ) VALUES($1,$2,'send_whatsapp',$3,$4)""",
                approval_id,
                case_id,
                payload,
                "a" * 64,
            )
            await connection.execute(
                """INSERT INTO outbox_events(
                    id,event_type,payload,status,attempts,max_attempts,idempotency_key,
                    locked_at,locked_by,lease_expires_at,provider_request_id,approval_id
                ) VALUES($1,'whatsapp.send_text',$2,'sending',1,3,$3,now(),'journal-test',
                         now()+interval '5 minutes',$4,$5)""",
                event_id,
                payload,
                key,
                request_id,
                approval_id,
            )
            await connection.execute(
                """INSERT INTO whatsapp_account_controls(
                       session_name,pinned_account_jid,pinned_by,pinned_at,
                       pin_observed_at,pin_version
                   ) VALUES('default','77000000003@c.us',$1,$2,$2,1)""",
                owner_id,
                accepted_at,
            )
            await connection.execute(
                """INSERT INTO whatsapp_account_send_reservations(
                       outbox_event_id,session_name,conversation_id,supplier_contact_id,
                       state,is_new_contact,reserved_at,post_worker,post_generation,
                       post_lease_expires_at,pinned_account_jid,account_pin_version,
                       observed_account_jid,account_identity_observed_at
                   ) VALUES($1,'default',$2,$3,'reserved',TRUE,now(),'journal-test',1,
                            now()+interval '5 minutes','77000000003@c.us',1,
                            '77000000003@c.us',$4)""",
                event_id,
                conversation_id,
                contact_id,
                accepted_at,
            )
        event = ClaimedOutbox(
            id=event_id,
            event_type="whatsapp.send_text",
            payload=payload,
            attempts=1,
            max_attempts=3,
            idempotency_key=key,
            locked_by="journal-test",
            lease_expires_at=datetime.now(UTC),
            approval_id=uuid4(),
            provider_request_id=request_id,
        )
        async with pool.acquire() as connection, connection.transaction():
            await persist_acceptance(
                connection,
                event,
                session_name="default",
                external_chat_id="77000000003@c.us",
                provider_request_id=request_id,
                result=WahaSendResult(
                    external_message_id="true_77000000003@c.us_reserved-42",
                    raw={"id": "true_77000000003@c.us_reserved-42"},
                ),
                source="http",
                accepted_at=accepted_at,
                account_jid="77000000003@c.us",
                account_pin_version=1,
                account_identity_observed_at=accepted_at,
            )
        async with pool.acquire() as connection:
            stored = await load_acceptance(connection, event)
            assert stored is not None
            row, result = stored
            assert row["accepted_at"] == accepted_at
            assert row["account_jid"] == "77000000003@c.us"
            assert row["account_pin_version"] == 1
            assert row["account_identity_observed_at"] == accepted_at
            assert result.external_message_id == "true_77000000003@c.us_reserved-42"
            with pytest.raises(
                asyncpg.PostgresError, match="supplier conversation messages are immutable"
            ):
                await connection.execute(
                    "UPDATE whatsapp_send_acceptance_journal SET source='reconciliation' "
                    "WHERE outbox_event_id=$1",
                    event_id,
                )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _event(event_type: str, payload: dict[str, Any]) -> ClaimedOutbox:
    return ClaimedOutbox(
        id=uuid4(),
        event_type=event_type,
        payload=payload,
        attempts=1,
        max_attempts=3,
        idempotency_key=f"event:{uuid4().hex}",
        locked_by="test-worker",
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        approval_id=uuid4() if event_type == "whatsapp.send_text" else None,
        provider_request_id="reserved-42" if event_type == "whatsapp.send_text" else None,
    )


class FakeBot:
    async def send_message(self, **_kwargs: object) -> object:
        return type("Sent", (), {"message_id": 12})()

    async def answer_callback_query(self, **_kwargs: object) -> bool:
        return True

    async def send_document(self, **_kwargs: object) -> object:
        return type("Sent", (), {"message_id": 13})()


class FakeWahaClient:
    session_name = "default"

    async def new_message_id(self) -> str:
        return "reserved-42"

    async def send_text(self, **_kwargs: object) -> WahaSendResult:
        return WahaSendResult(external_message_id="wa-12", raw={"id": "wa-12"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_kind", "expected_types"),
    [
        (
            "telegram",
            (
                "telegram.send_message",
                "telegram.answer_callback_query",
                "telegram.send_document",
            ),
        ),
        ("whatsapp", ("whatsapp.send_text",)),
    ],
)
async def test_outbox_workers_claim_only_their_own_channel(
    monkeypatch: pytest.MonkeyPatch,
    worker_kind: str,
    expected_types: tuple[str, ...],
) -> None:
    captured: dict[str, object] = {}

    async def fake_claim(_pool: object, **kwargs: object) -> None:
        captured.update(kwargs)
        return None

    if worker_kind == "telegram":
        monkeypatch.setattr("procurement_bot.outbox.claim_outbox", fake_claim)
        worker = TelegramOutboxWorker(
            pool=object(),  # type: ignore[arg-type]
            bot=FakeBot(),  # type: ignore[arg-type]
            worker_id="telegram-worker",
        )
    else:

        async def fake_whatsapp_claim(_pool: object, **kwargs: object) -> None:
            captured.update(kwargs)
            return None

        monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", fake_whatsapp_claim)
        worker = WhatsAppOutboxWorker(
            pool=object(),  # type: ignore[arg-type]
            client=FakeWahaClient(),  # type: ignore[arg-type]
            worker_id="whatsapp-worker",
        )

    assert not await worker.run_once()
    if worker_kind == "telegram":
        assert captured["event_types"] == expected_types
    else:
        assert captured["worker_id"] == "whatsapp-worker"
        assert (
            "event.event_type='whatsapp.send_text'"
            in Path("src/procurement_bot/chat_actors.py").read_text()
        )


@pytest.mark.asyncio
async def test_whatsapp_worker_passes_outbox_key_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event(
            "whatsapp.send_text",
            {
                "case_id": str(uuid4()),
                "channel": "whatsapp",
                "recipient": "+77000000003",
                "text": "Здравствуйте",
                "conversation_id": str(uuid4()),
                "logical_action_id": "availability-v1",
                "is_followup": False,
            },
        ),
        provider_request_id=None,
    )
    calls: dict[str, object] = {}

    class RecordingClient:
        session_name = "default"

        async def new_message_id(self) -> str:
            return "reserved-42"

        async def get_account_identity(self) -> WahaAccountIdentity:
            calls["identity_checked"] = True
            return WahaAccountIdentity("77000000003@c.us", datetime.now(UTC))

        async def send_text(self, **kwargs: object) -> WahaSendResult:
            calls.update(kwargs)
            return WahaSendResult(external_message_id="wa-42", raw={"id": "wa-42"})

    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=1,
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )

    async def fake_claim(*_args: object, **_kwargs: object) -> tuple[ClaimedOutbox, ChatActorLease]:
        return event, actor

    async def fake_allowed(*_args: object, **_kwargs: object) -> object:
        return type(
            "Delivery",
            (),
            {"session_name": "default", "external_chat_id": "77000000003@c.us"},
        )()

    async def fake_acceptance(*_args: object, **_kwargs: object):
        return None

    async def fake_reserve(_pool, reserved_event, **kwargs):
        identity = kwargs["identity_observation"]
        calls["identity_observation"] = (identity.account_jid, kwargs["worker_id"])
        assert kwargs["expected_account_jid"] == identity.account_jid
        assert kwargs["expected_pin_version"] == 1
        return WhatsAppAccountReservation(
            outbox_event_id=reserved_event.id,
            session_name="default",
            supplier_contact_id=uuid4(),
            is_new_contact=True,
            generation=1,
            lease_expires_at=datetime.now(UTC),
            pinned_account_jid="77000000003@c.us",
            account_pin_version=1,
        )

    async def fake_pin(*_args):
        return WhatsAppAccountIdentityPin("77000000003@c.us", 1)

    async def fake_mark_outcome(*_args: object, **_kwargs: object) -> None:
        return None

    async def fake_persist(*_args: object, **kwargs: object) -> None:
        calls["journal_source"] = kwargs["source"]

    async def fake_complete(*args: object, **_kwargs: object) -> bool:
        result = args[3]
        calls["external_id"] = result.external_message_id
        calls["completion_after_journal"] = calls.get("journal_source") == "http"
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", fake_claim)
    monkeypatch.setattr("procurement_bot.outbox.load_acceptance", fake_acceptance)
    monkeypatch.setattr("procurement_bot.outbox.reserve_outbound", fake_reserve)
    monkeypatch.setattr("procurement_bot.outbox.load_account_identity_pin", fake_pin)
    monkeypatch.setattr("procurement_bot.outbox.mark_outcome", fake_mark_outcome)
    monkeypatch.setattr("procurement_bot.outbox.persist_acceptance", fake_persist)

    async def fake_release(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", fake_release)
    monkeypatch.setattr(
        "procurement_bot.outbox.assert_whatsapp_delivery_allowed",
        fake_allowed,
    )
    monkeypatch.setattr(
        "procurement_bot.outbox.complete_whatsapp_delivery",
        fake_complete,
    )

    async def fake_validate(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", fake_validate)
    worker = WhatsAppOutboxWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=RecordingClient(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )

    assert await worker.run_once()
    assert calls == {
        "recipient": "+77000000003",
        "text": "Здравствуйте",
        "idempotency_key": event.idempotency_key,
        "provider_message_id": "reserved-42",
        "external_id": "wa-42",
        "journal_source": "http",
        "completion_after_journal": True,
        "identity_checked": True,
        "identity_observation": ("77000000003@c.us", "whatsapp-worker"),
    }


@pytest.mark.asyncio
async def test_pinned_account_mismatch_defers_before_request_id_or_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event(
            "whatsapp.send_text",
            {
                "case_id": str(uuid4()),
                "channel": "whatsapp",
                "recipient": "+77000000003",
                "text": "Здравствуйте",
                "conversation_id": str(uuid4()),
                "logical_action_id": "availability-v1",
                "is_followup": False,
            },
        ),
        provider_request_id=None,
    )
    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    calls: dict[str, object] = {}

    class Client:
        session_name = "default"

        async def new_message_id(self) -> str:
            raise AssertionError("mismatch must not allocate a request ID")

        async def get_account_identity(self) -> WahaAccountIdentity:
            calls["identity_read"] = True
            return WahaAccountIdentity("77000000004@c.us", datetime.now(UTC))

        async def send_text(self, **_kwargs: object) -> WahaSendResult:
            calls["post"] = True
            raise AssertionError("identity mismatch must block POST")

    async def claim(*_args: object, **_kwargs: object):
        return event, actor

    async def reserve(*_args, **_kwargs):
        raise AssertionError("mismatch must not reserve")

    async def pin(*_args):
        return WhatsAppAccountIdentityPin("77000000003@c.us", 7)

    async def record(_pool, **kwargs):
        calls["audit"] = kwargs["event_type"]
        calls["observed"] = kwargs["observed_identity"].account_jid

    async def defer(_pool, _event, **kwargs):
        calls["deferred"] = kwargs["reason"]
        return True

    async def empty(*_args: object, **_kwargs: object):
        return None

    async def allowed(*_args: object, **_kwargs: object):
        return type(
            "Delivery",
            (),
            {"session_name": "default", "external_chat_id": "77000000003@c.us"},
        )()

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def mark(_connection, _event_id, **kwargs: object) -> None:
        calls["outcome"] = kwargs

    async def fail(_pool, _event, _code: str, *, retryable: bool) -> bool:
        calls["failed_retryable"] = retryable
        return True

    async def release(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.reserve_outbound", reserve)
    monkeypatch.setattr("procurement_bot.outbox.load_account_identity_pin", pin)
    monkeypatch.setattr("procurement_bot.outbox.record_account_identity_preflight_event", record)
    monkeypatch.setattr("procurement_bot.outbox.defer_outbox", defer)
    monkeypatch.setattr("procurement_bot.outbox.load_acceptance", empty)
    monkeypatch.setattr("procurement_bot.outbox.assert_whatsapp_delivery_allowed", allowed)
    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", validate)
    monkeypatch.setattr("procurement_bot.outbox.mark_outcome", mark)
    monkeypatch.setattr("procurement_bot.outbox.fail_outbox", fail)
    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", release)

    worker = WhatsAppOutboxWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=Client(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )
    assert await worker.run_once()
    assert calls["identity_read"] is True
    assert calls["observed"] == "77000000004@c.us"
    assert "post" not in calls
    assert "outcome" not in calls
    assert "failed_retryable" not in calls
    assert calls["audit"] == "account_identity_mismatch"
    assert calls["deferred"] == "whatsapp_account_identity_mismatch"


@pytest.mark.asyncio
async def test_reserved_request_without_journal_defers_unknown_and_never_reposts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(
        "whatsapp.send_text",
        {
            "case_id": str(uuid4()),
            "channel": "whatsapp",
            "recipient": "+77000000003",
            "text": "Здравствуйте",
            "conversation_id": str(uuid4()),
            "logical_action_id": "availability-v1",
            "is_followup": False,
        },
    )
    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=1,
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )
    calls: dict[str, object] = {}

    class Client:
        session_name = "default"

        async def get_account_identity(self):
            raise AssertionError("NULL-identity legacy action must not query current account")

        async def fetch_message(self, chat_id: str, message_id: str):
            raise AssertionError(
                "NULL-identity legacy action must not query message under new owner"
            )

    async def claim(*_args: object, **kwargs: object):
        calls["selected_event_id"] = kwargs.get("event_id")
        return event, actor

    async def no_journal(*_args: object, **_kwargs: object):
        return None

    async def identity(*_args: object, **_kwargs: object):
        return {
            "session_name": "default",
            "external_chat_id": "77000000003@c.us",
            "pinned_account_jid": None,
            "observed_account_jid": None,
            "account_pin_version": None,
            "account_identity_observed_at": None,
            "state": "unknown",
        }

    async def mark_unknown(_connection, _event, *, conversation_id):
        calls["unknown_conversation"] = conversation_id
        return True

    async def allowed(*_args: object, **_kwargs: object):
        raise AssertionError("unknown reconciliation must not reacquire a send slot")

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def defer(_pool, deferred_event, *, available_at, reason):
        calls["deferred"] = (deferred_event.id, available_at, reason)
        return True

    async def fail(*_args: object, **_kwargs: object):
        calls["failed"] = True
        return True

    async def release(*_args: object, **_kwargs: object):
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.load_acceptance", no_journal)
    monkeypatch.setattr("procurement_bot.outbox.load_reconciliation_identity", identity)
    monkeypatch.setattr("procurement_bot.outbox.mark_acceptance_unknown", mark_unknown)

    async def ignore_account_outcome(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("procurement_bot.outbox.mark_outcome", ignore_account_outcome)
    monkeypatch.setattr("procurement_bot.outbox.assert_whatsapp_delivery_allowed", allowed)
    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", validate)
    monkeypatch.setattr("procurement_bot.outbox.defer_outbox", defer)
    monkeypatch.setattr("procurement_bot.outbox.fail_outbox", fail)
    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", release)

    worker = WhatsAppOutboxWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=Client(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )
    assert await worker.run_once(event_id=event.id)
    assert calls["selected_event_id"] == event.id
    assert "lookup" not in calls
    assert calls["unknown_conversation"] == UUID(event.payload["conversation_id"])
    deferred_id, available_at, reason = calls["deferred"]
    assert deferred_id == event.id
    assert 29 * 60 <= (available_at - datetime.now(UTC)).total_seconds() <= 30 * 60
    assert reason == "provider_acceptance_unknown"
    assert "failed" not in calls


@pytest.mark.asyncio
async def test_durable_acceptance_reuses_result_without_provider_or_send_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(
        "whatsapp.send_text",
        {
            "case_id": str(uuid4()),
            "channel": "whatsapp",
            "recipient": "+77000000003",
            "text": "Здравствуйте",
            "conversation_id": str(uuid4()),
            "logical_action_id": "availability-v1",
            "is_followup": False,
        },
    )
    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=1,
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )
    accepted_at = datetime(2026, 10, 3, 10, tzinfo=UTC)
    calls: dict[str, object] = {}

    class Client:
        session_name = "default"

    async def claim(*_args: object, **_kwargs: object):
        return event, actor

    async def acceptance(*_args: object, **_kwargs: object):
        return (
            {
                "session_name": "default",
                "external_chat_id": "77000000003@c.us",
                "accepted_at": accepted_at,
            },
            WahaSendResult(external_message_id="wa-accepted", raw={"id": "wa-accepted"}),
        )

    async def allowed(*_args: object, **_kwargs: object):
        raise AssertionError("already accepted send needs no send-policy admission")

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def complete(_pool, _event, delivery, result, *, received_at):
        calls["completion"] = (
            delivery.session_name,
            delivery.external_chat_id,
            result.external_message_id,
            received_at,
        )
        return True

    async def ignore_account_outcome(*_args: object, **_kwargs: object) -> None:
        return None

    async def release(*_args: object, **_kwargs: object):
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.load_acceptance", acceptance)
    monkeypatch.setattr("procurement_bot.outbox.mark_outcome", ignore_account_outcome)
    monkeypatch.setattr("procurement_bot.outbox.assert_whatsapp_delivery_allowed", allowed)
    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", validate)
    monkeypatch.setattr("procurement_bot.outbox.complete_whatsapp_delivery", complete)
    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", release)

    worker = WhatsAppOutboxWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=Client(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )
    assert await worker.run_once()
    assert calls["completion"] == (
        "default",
        "77000000003@c.us",
        "wa-accepted",
        accepted_at,
    )


@pytest.mark.asyncio
async def test_lost_chat_actor_cancels_and_reaps_send_and_blocks_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event(
            "whatsapp.send_text",
            {
                "case_id": str(uuid4()),
                "channel": "whatsapp",
                "recipient": "+77000000003",
                "text": "Здравствуйте",
                "conversation_id": str(uuid4()),
                "logical_action_id": "availability-v1",
                "is_followup": False,
            },
        ),
        provider_request_id=None,
    )
    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=4,
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()
    outcomes: dict[str, object] = {}

    class BlockingClient:
        session_name = "default"

        async def new_message_id(self) -> str:
            return "reserved-before-blocked-send"

        async def get_account_identity(self) -> WahaAccountIdentity:
            return WahaAccountIdentity("77000000003@c.us", datetime.now(UTC))

        async def send_text(self, **_kwargs: object) -> WahaSendResult:
            provider_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise

    async def claim(*_args: object, **_kwargs: object):
        return event, actor

    async def allowed(*_args: object, **_kwargs: object) -> object:
        return type(
            "Delivery",
            (),
            {"session_name": "default", "external_chat_id": "77000000003@c.us"},
        )()

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def complete(*_args: object, **_kwargs: object) -> bool:
        outcomes["completed"] = True
        return True

    async def fail(_pool: object, _event: object, code: str, *, retryable: bool) -> bool:
        outcomes["failure"] = (code, retryable)
        return True

    async def release(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", claim)

    async def empty_acceptance(*_args: object, **_kwargs: object):
        return None

    async def reserve_request(_pool, reserved_event, **_kwargs):
        return WhatsAppAccountReservation(
            outbox_event_id=reserved_event.id,
            session_name="default",
            supplier_contact_id=uuid4(),
            is_new_contact=True,
            generation=1,
            lease_expires_at=datetime.now(UTC),
            pinned_account_jid="77000000003@c.us",
            account_pin_version=1,
        )

    async def ignore_account_outcome(*_args: object, **_kwargs: object) -> None:
        return None

    async def ignore_acceptance_unknown(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("procurement_bot.outbox.load_acceptance", empty_acceptance)
    monkeypatch.setattr("procurement_bot.outbox.mark_acceptance_unknown", ignore_acceptance_unknown)
    monkeypatch.setattr("procurement_bot.outbox.reserve_outbound", reserve_request)

    async def pin(*_args):
        return WhatsAppAccountIdentityPin("77000000003@c.us", 1)

    monkeypatch.setattr("procurement_bot.outbox.load_account_identity_pin", pin)
    monkeypatch.setattr("procurement_bot.outbox.mark_outcome", ignore_account_outcome)
    monkeypatch.setattr("procurement_bot.outbox.assert_whatsapp_delivery_allowed", allowed)
    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", validate)
    monkeypatch.setattr("procurement_bot.outbox.complete_whatsapp_delivery", complete)
    monkeypatch.setattr("procurement_bot.outbox.fail_outbox", fail)
    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", release)

    class LeaseLostWorker(WhatsAppOutboxWorker):
        async def _heartbeat(
            self,
            _event: object,
            _actor: object,
            lease_lost: asyncio.Event,
            _account_reservation: object,
        ) -> None:
            await provider_started.wait()
            lease_lost.set()

    worker = LeaseLostWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=BlockingClient(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )

    assert await worker.run_once()
    assert provider_cancelled.is_set()
    assert "completed" not in outcomes
    assert outcomes["failure"] == ("WhatsAppSendOutcomeUnknown", False)
    assert issubclass(WhatsAppSendOutcomeUnknown, RuntimeError)


@pytest.mark.asyncio
async def test_auto_send_deferral_releases_outbox_without_provider_id_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event(
            "whatsapp.send_text",
            {
                "case_id": str(uuid4()),
                "channel": "whatsapp",
                "recipient": "+77000000003",
                "text": "Здравствуйте",
                "conversation_id": str(uuid4()),
                "logical_action_id": "availability-v1",
                "is_followup": False,
            },
        ),
        provider_request_id=None,
    )
    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=1,
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )
    available_at = datetime(2026, 9, 23, 3, tzinfo=UTC)
    outcome: dict[str, object] = {}

    class Client:
        async def new_message_id(self) -> str:
            outcome["reserved"] = True
            return "must-not-reserve"

        async def send_text(self, **_kwargs: object) -> WahaSendResult:
            outcome["sent"] = True
            return WahaSendResult(external_message_id="unexpected", raw={})

    async def claim(*_args: object, **_kwargs: object):
        return event, actor

    async def allowed(*_args: object, **_kwargs: object) -> object:
        raise WhatsAppSendDeferred(available_at=available_at, reason="quiet hours")

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def defer(
        _pool: object,
        deferred_event: ClaimedOutbox,
        *,
        available_at: datetime,
        reason: str,
    ) -> bool:
        outcome["deferred"] = (deferred_event.id, available_at, reason)
        return True

    async def fail(*_args: object, **_kwargs: object) -> bool:
        outcome["failed"] = True
        return True

    async def release(*_args: object, **_kwargs: object) -> bool:
        outcome["released"] = True
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.assert_whatsapp_delivery_allowed", allowed)
    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", validate)
    monkeypatch.setattr("procurement_bot.outbox.defer_outbox", defer)
    monkeypatch.setattr("procurement_bot.outbox.fail_outbox", fail)
    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", release)
    worker = WhatsAppOutboxWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=Client(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )

    assert await worker.run_once()
    assert outcome["deferred"] == (event.id, available_at, "quiet hours")
    assert "reserved" not in outcome
    assert "sent" not in outcome
    assert "failed" not in outcome
    assert outcome["released"] is True


@pytest.mark.asyncio
async def test_waha_unready_session_defers_without_reserving_id_or_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event(
            "whatsapp.send_text",
            {
                "case_id": str(uuid4()),
                "channel": "whatsapp",
                "recipient": "+77000000003",
                "text": "Здравствуйте",
                "conversation_id": str(uuid4()),
                "logical_action_id": "availability-v1",
                "is_followup": False,
            },
        ),
        provider_request_id=None,
    )
    actor = ChatActorLease(
        actor_id=uuid4(),
        session_name="default",
        external_chat_id="77000000003@c.us",
        worker_id="whatsapp-worker",
        generation=1,
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )
    calls: dict[str, object] = {}

    class Client:
        session_name = "default"

        async def get_account_identity(self):
            return WahaAccountIdentity("77000000003@c.us", datetime.now(UTC))

        async def new_message_id(self) -> str:
            calls["reservation_attempt"] = True
            raise RetryableProviderError("WAHA session is not ready to reserve a message id")

        async def send_text(self, **_kwargs: object) -> WahaSendResult:
            calls["post"] = True
            raise AssertionError("WAHA not-ready response must prevent the POST")

    async def claim(*_args: object, **_kwargs: object):
        return event, actor

    async def allowed(*_args: object, **_kwargs: object) -> object:
        return type(
            "Delivery",
            (),
            {"session_name": "default", "external_chat_id": "77000000003@c.us"},
        )()

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def defer(_pool, deferred_event, *, available_at, reason):
        calls["deferred"] = (deferred_event.id, available_at, reason)
        return True

    async def release(*_args: object, **_kwargs: object):
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_whatsapp_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.assert_whatsapp_delivery_allowed", allowed)
    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", validate)
    monkeypatch.setattr("procurement_bot.outbox.defer_outbox", defer)
    monkeypatch.setattr("procurement_bot.outbox.release_chat_actor", release)

    async def pin(*_args):
        return WhatsAppAccountIdentityPin("77000000003@c.us", 1)

    monkeypatch.setattr("procurement_bot.outbox.load_account_identity_pin", pin)
    worker = WhatsAppOutboxWorker(
        pool=_FakePool(),  # type: ignore[arg-type]
        client=Client(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )

    assert await worker.run_once()
    assert calls["reservation_attempt"] is True
    assert "post" not in calls
    assert calls["deferred"][0] == event.id
    assert calls["deferred"][2] == "waha_session_unavailable"


@pytest.mark.asyncio
async def test_telegram_worker_resolves_and_sends_private_report_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = tmp_path / "reports" / "aa" / "report.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html>report</html>", encoding="utf-8")
    event = _event(
        "telegram.send_document",
        {
            "chat_id": 123,
            "storage_path": "reports/aa/report.html",
            "filename": "procurement-report.html",
            "caption": "Отчёт готов",
        },
    )
    captured: dict[str, object] = {}

    class RecordingBot(FakeBot):
        async def send_document(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return type("Sent", (), {"message_id": 44})()

    async def claim(*_args: object, **_kwargs: object) -> ClaimedOutbox:
        return event

    async def complete(*_args: object, **kwargs: object) -> bool:
        captured["external_id"] = kwargs["external_id"]
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.complete_outbox", complete)
    worker = TelegramOutboxWorker(
        pool=object(),  # type: ignore[arg-type]
        bot=RecordingBot(),  # type: ignore[arg-type]
        worker_id="telegram-worker",
        results_root=tmp_path,
    )

    assert await worker.run_once()
    assert captured["chat_id"] == 123
    assert captured["caption"] == "Отчёт готов"
    assert Path(captured["document"].path) == report  # type: ignore[union-attr]
    assert captured["external_id"] == "44"
