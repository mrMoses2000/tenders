from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "004_action_approvals.sql"


def test_approval_grants_are_append_only_and_required_for_whatsapp() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS action_approvals" in sql
    assert "UNIQUE (case_id, action_type, payload_sha256)" not in sql
    assert "action_approvals_one_live_payload_idx" in sql
    assert "WHERE status IN ('requested','approved')" in sql
    assert "ADD COLUMN IF NOT EXISTS approval_id" in sql
    assert "ADD COLUMN IF NOT EXISTS provider_request_id" in sql
    assert "outbox_whatsapp_requires_approval" in sql
    assert "outbox_events_approval_idx" in sql
