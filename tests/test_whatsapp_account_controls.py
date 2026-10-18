from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.whatsapp_account import (
    WhatsAppAccountDenied,
    assert_account_send_allowed,
    ensure_account,
    get_account_state,
    observe_provider_state,
)


@pytest.mark.asyncio
async def test_new_account_stays_held_after_clear_provider_observation() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_waha_account_{uuid4().hex}"
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
        async with pool.acquire() as connection:
            account = await ensure_account(connection, "test-session")
            assert account["operator_held"] is True
            assert account["provider_state"] == "unknown"

            await observe_provider_state(
                connection,
                "test-session",
                state="clear",
                observed_at=datetime.now(UTC),
                source="test_probe",
                reason="provider_probe_clear",
            )
            state = await get_account_state(connection, "test-session")
            assert state["provider_state"] == "clear"
            assert state["operator_held"] is True
            assert state["outbound_allowed"] is False
            assert state["daily_message_limit"] == 30
            assert state["daily_new_contact_limit"] == 0
            assert state["minimum_interval_seconds"] == 120
            with pytest.raises(WhatsAppAccountDenied, match="operator_hold"):
                await assert_account_send_allowed(connection, "test-session")

            await connection.execute(
                """UPDATE whatsapp_account_controls SET operator_held=FALSE,
                       provider_observed_at=now()-interval '31 minutes'
                   WHERE session_name='test-session'"""
            )
            stale = await get_account_state(connection, "test-session")
            assert stale["provider_clear_fresh"] is False
            assert stale["outbound_allowed"] is False
            with pytest.raises(WhatsAppAccountDenied, match="not_fresh_clear"):
                await assert_account_send_allowed(connection, "test-session")

            await observe_provider_state(
                connection,
                "test-session",
                state="restricted",
                observed_at=datetime.now(UTC),
                source="test_probe",
                reason="provider_restriction_seen",
            )
            with pytest.raises(WhatsAppAccountDenied, match="not_fresh_clear"):
                await assert_account_send_allowed(connection, "test-session")
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
