from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "007_conversation_targets.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_target_table_supports_multi_item_conversations_without_forcing_backfill() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS supplier_conversation_targets" in sql
    assert "ALTER TABLE supplier_conversations" not in sql
    assert "UNIQUE (conversation_id, offer_id)" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql
    assert "provenance_kind TEXT NOT NULL" in sql
    assert "provenance_ref TEXT NOT NULL" in sql
    assert "source_evidence_id UUID REFERENCES source_evidence(id)" in sql


def test_composite_foreign_keys_prove_case_supplier_item_and_offer_ownership() -> None:
    sql = " ".join(_sql().split())

    assert "FOREIGN KEY (conversation_id, case_id, supplier_id)" in sql
    assert "REFERENCES supplier_conversations(id, case_id, supplier_id)" in sql
    assert "FOREIGN KEY (offer_id, request_item_id, supplier_id)" in sql
    assert "REFERENCES offers(id, request_item_id, supplier_id)" in sql
    assert "FOREIGN KEY (request_item_id, case_id)" in sql
    assert "REFERENCES request_items(id, case_id)" in sql


def test_target_provenance_is_immutable_and_has_no_send_side_effect() -> None:
    sql = _sql().casefold()

    assert "before update or delete on supplier_conversation_targets" in sql
    assert "supplier conversation targets are immutable" in sql
    assert "insert into outbox_events" not in sql
    assert "whatsapp.send_text" not in sql
