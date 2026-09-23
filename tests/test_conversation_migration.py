from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "003_supplier_conversations.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_conversation_migration_has_owned_identity_state_and_counters() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS supplier_conversations" in sql
    assert "FOREIGN KEY (supplier_contact_id, supplier_id)" in sql
    assert "REFERENCES supplier_contacts(id, supplier_id)" in sql
    assert "inbound_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "outbound_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "followup_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "opted_out AND state='opted_out'" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql


def test_message_ledger_is_immutable_and_exactly_deduplicated() -> None:
    sql = _sql()
    block = sql.split(
        "CREATE TABLE IF NOT EXISTS supplier_conversation_messages", maxsplit=1
    )[1]

    assert "UNIQUE (session_name, external_chat_id, external_message_id)" in block
    assert "webhook_event_id UUID UNIQUE" in block
    assert "outbox_event_id UUID UNIQUE" in block
    assert "direction='inbound' AND webhook_event_id IS NOT NULL" in block
    assert "direction='outbound' AND webhook_event_id IS NULL" in block
    assert "BEFORE UPDATE OR DELETE ON supplier_conversation_messages" in block
    assert "raw_payload JSONB NOT NULL" in block
    assert "payload_sha256 TEXT NOT NULL" in block


def test_webhook_inbox_preserves_payload_and_has_process_lifecycle() -> None:
    sql = _sql()
    block = sql.split("CREATE TABLE IF NOT EXISTS waha_webhook_events", maxsplit=1)[1]

    for status in ("accepted", "processing", "processed", "failed", "ignored"):
        assert f"'{status}'" in block
    assert "payload_sha256 TEXT NOT NULL" in block
    assert "raw_payload JSONB NOT NULL" in block
    assert "waha_webhook_events_external_idx" in block
    assert "idempotency_key TEXT NOT NULL UNIQUE" in block
    assert "BEFORE UPDATE OR DELETE ON waha_webhook_events" in block
    assert "OLD.raw_payload IS DISTINCT FROM NEW.raw_payload" in block


def test_migration_never_authorises_or_sends_an_external_message() -> None:
    sql = _sql().casefold()

    assert "insert into outbox_events" not in sql
    assert "action_approvals" not in sql
    assert "http://" not in sql
    assert "https://" not in sql
