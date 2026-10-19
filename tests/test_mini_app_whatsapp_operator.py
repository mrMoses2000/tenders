from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import asyncpg
import pytest
from aiohttp.test_utils import TestClient, TestServer

from procurement_bot.config import Settings
from procurement_bot.db import run_migrations
from procurement_bot.mini_app import create_mini_app
from procurement_bot.providers.waha import WahaAccountIdentity, WahaClient


def _signed_data(token: str, telegram_id: int) -> str:
    values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": telegram_id})}
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_whatsapp_operator_api_auth_owner_origin_and_pin_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"miniapp_whatsapp_operator_{uuid4().hex}"
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
        max_size=4,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    client: TestClient | None = None
    signing_key = "mini-app-operator-test-key"
    owner_telegram_id = 1_410_000_000 + int(uuid4().hex[:7], 16)
    other_telegram_id = owner_telegram_id + 1
    session_name = "operator-test-session"
    owner_id = uuid4()
    case_id, supplier_id, evidence_id, contact_id, conversation_id = (uuid4() for _ in range(5))
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        async with pool.acquire() as connection:
            await connection.executemany(
                "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
                [(owner_id, owner_telegram_id, "Owner"), (uuid4(), other_telegram_id, "Other")],
            )
            await connection.execute(
                "INSERT INTO procurement_cases(id,owner_user_id,status) "
                "VALUES($1,$2,'draft')",
                case_id, owner_id,
            )
            await connection.execute(
                """INSERT INTO source_evidence(
                       id,source_type,source_locator,captured_at,content_sha256,
                       raw_payload,idempotency_key
                   ) VALUES($1,'manual','miniapp-operator-test',now(),$2,'{}'::jsonb,$3)""",
                evidence_id, "b" * 64, f"miniapp-operator-evidence:{evidence_id}",
            )
            await connection.execute(
                "INSERT INTO suppliers(id,display_name) VALUES($1,'Test supplier')",
                supplier_id,
            )
            await connection.execute(
                """INSERT INTO supplier_contacts(
                       id,supplier_id,contact_type,display_value,normalized_value,source_evidence_id
                   ) VALUES($1,$2,'phone','+77000000004','77000000004',$3)""",
                contact_id, supplier_id, evidence_id,
            )
            await connection.execute(
                """INSERT INTO supplier_conversations(
                       id,case_id,supplier_id,supplier_contact_id,session_name,
                       external_chat_id,idempotency_key
                   ) VALUES($1,$2,$3,$4,$5,'77000000004@c.us',$6)""",
                conversation_id, case_id, supplier_id, contact_id, session_name,
                f"miniapp-operator-conversation:{conversation_id}",
            )

        settings = Settings(
            telegram_bot_token=signing_key,
            telegram_allowed_user_ids=f"{owner_telegram_id},{other_telegram_id}",
            mini_app_public_url="https://example.test/miniapp/",
            waha_enabled=True,
            waha_api_key="synthetic-test-key",
            waha_session=session_name,
        )
        client = TestClient(TestServer(create_mini_app(pool, settings)))
        await client.start_server()
        owner_headers = {"Authorization": f"tma {_signed_data(signing_key, owner_telegram_id)}"}
        other_headers = {"Authorization": f"tma {_signed_data(signing_key, other_telegram_id)}"}

        unauthorized = await client.get("/api/whatsapp/accounts")
        assert unauthorized.status == 401
        mismatch = await client.get(
            f"/api/whatsapp/legacy-unknown-inventory?session_name={session_name}",
            headers=other_headers,
        )
        assert mismatch.status == 403

        calls = 0

        async def synthetic_identity(self: WahaClient) -> WahaAccountIdentity:
            nonlocal calls
            calls += 1
            return WahaAccountIdentity("777777777@c.us", datetime.now(UTC))

        monkeypatch.setattr(WahaClient, "get_account_identity", synthetic_identity)
        body = {
            "session_name": session_name,
            "expected_account_jid": "777777777@c.us",
            "reason": "operator confirmed synthetic test identity",
            "idempotency_key": f"pin-test-{uuid4().hex}",
        }
        same_origin = {**owner_headers, "Origin": "https://example.test"}
        spoofed = await client.post(
            "/api/whatsapp/account-pin",
            json={**body, "actor_user_id": str(owner_id)},
            headers=same_origin,
        )
        assert spoofed.status == 400
        missing_origin = await client.post(
            "/api/whatsapp/account-pin", json=body, headers=owner_headers
        )
        assert missing_origin.status == 403
        assert calls == 0

        first = await client.post("/api/whatsapp/account-pin", json=body, headers=same_origin)
        second = await client.post("/api/whatsapp/account-pin", json=body, headers=same_origin)
        assert first.status == second.status == 200
        first_payload = await first.json()
        assert await second.json() == first_payload
        assert first_payload == {
            "account_pin_version": 1,
            "masked_account_jid": "••••7777@c.us",
        }
        assert calls == 1

        accounts = await client.get("/api/whatsapp/accounts", headers=owner_headers)
        assert accounts.status == 200
        account_payload = await accounts.json()
        assert account_payload["accounts"][0]["masked_account_jid"] == "••••7777@c.us"
        assert "777777777@c.us" not in json.dumps(account_payload)
        async with pool.acquire() as connection:
            event_count = await connection.fetchval(
                """SELECT count(*) FROM whatsapp_account_control_events
                   WHERE session_name=$1 AND operator_idempotency_key=$2""",
                session_name, body["idempotency_key"],
            )
        assert event_count == 1
        legacy_event_id = uuid4()
        cutoff = await pool.fetchval(
            """SELECT applied_at FROM schema_migrations
               WHERE name='043_whatsapp_account_identity_binding.sql'"""
        )
        legacy_time = cutoff - timedelta(days=1)
        async with pool.acquire() as connection:
            await connection.execute(
                """INSERT INTO outbox_events(id,event_type,idempotency_key)
                   VALUES($1,'synthetic_legacy_unknown',$2)""",
                legacy_event_id,
                f"legacy-operator-test:{legacy_event_id}",
            )
            await connection.execute(
                """INSERT INTO whatsapp_account_send_reservations(
                       outbox_event_id,session_name,conversation_id,supplier_contact_id,state,
                       is_new_contact,reserved_at,created_at,updated_at,outcome_source
                   ) VALUES($1,$2,$3,$4,'unknown',TRUE,$5,$5,$5,'migration')""",
                legacy_event_id, session_name, conversation_id, contact_id, legacy_time,
            )
        inventory = await client.get(
            "/api/whatsapp/legacy-unknown-inventory?session_name=" + session_name,
            headers=owner_headers,
        )
        assert inventory.status == 200
        snapshot = await inventory.json()
        assert snapshot["reservation_ids"] == [str(legacy_event_id)]
        assert snapshot["reservation_count"] == 1
        quarantine_body = {
            "session_name": session_name,
            "reservation_ids": snapshot["reservation_ids"],
            "inventory_sha256": snapshot["inventory_sha256"],
            "source_reference": "handoff:synthetic-legacy-inventory",
            "reason": "synthetic exact inventory review",
            "idempotency_key": "legacy-test-" + uuid4().hex,
        }
        quarantine_headers = {**owner_headers, "Origin": "https://example.test"}
        spoofed_quarantine = await client.post(
            "/api/whatsapp/legacy-unknown-quarantine",
            json={**quarantine_body, "actor_user_id": str(owner_id)},
            headers=quarantine_headers,
        )
        assert spoofed_quarantine.status == 400
        first_quarantine = await client.post(
            "/api/whatsapp/legacy-unknown-quarantine",
            json=quarantine_body,
            headers=quarantine_headers,
        )
        second_quarantine = await client.post(
            "/api/whatsapp/legacy-unknown-quarantine",
            json=quarantine_body,
            headers=quarantine_headers,
        )
        assert first_quarantine.status == second_quarantine.status == 200
        first_quarantine_payload = await first_quarantine.json()
        assert first_quarantine_payload["replayed"] is False
        assert (await second_quarantine.json()) == {
            **first_quarantine_payload,
            "replayed": True,
        }
        refreshed_inventory = await client.get(
            "/api/whatsapp/legacy-unknown-inventory?session_name=" + session_name,
            headers=owner_headers,
        )
        refreshed_payload = await refreshed_inventory.json()
        assert refreshed_payload["reservation_ids"] == []
        assert refreshed_payload["reservation_count"] == 0
        state_after_quarantine = await client.get(
            "/api/whatsapp/accounts", headers=owner_headers
        )
        account_after_quarantine = (await state_after_quarantine.json())["accounts"][0]
        assert account_after_quarantine["unknown_reservations"] == 1
        assert account_after_quarantine["quarantined_unknown_reservations"] == 1
    finally:
        if client is not None:
            await client.close()
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
