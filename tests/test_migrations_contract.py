from __future__ import annotations

import re
from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "001_initial.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_initial_migration_contains_intake_and_procurement_boundaries() -> None:
    sql = _sql()
    expected_tables = {
        "users",
        "processed_updates",
        "messages",
        "incoming_attachments",
        "source_artifacts",
        "content_extractions",
        "jobs",
        "outbox_events",
        "procurement_cases",
        "request_items",
        "case_parse_versions",
        "item_requirements",
        "clarifications",
        "workflow_events",
        "audit_log",
    }
    actual_tables = set(
        re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", sql, re.IGNORECASE)
    )
    assert expected_tables <= actual_tables


def test_job_contract_has_explicit_states_leases_and_skip_locked_index() -> None:
    sql = _sql().lower()
    for state in ("pending", "running", "retry", "succeeded", "dead"):
        assert f"'{state}'" in sql
    for column in (
        "attempts",
        "max_attempts",
        "available_at",
        "locked_by",
        "lease_expires_at",
        "idempotency_key",
    ):
        assert column in sql
    assert "jobs_claim_idx" in sql
    assert "unique" in sql[sql.index("create table if not exists jobs") :]


def test_queue_code_claims_jobs_and_outbox_with_skip_locked() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "procurement_bot" / "queue.py"
    ).read_text(encoding="utf-8").lower()
    assert source.count("for update skip locked") >= 2
    for function_name in (
        "claim_job",
        "renew_job_lease",
        "claim_outbox",
        "renew_outbox_lease",
    ):
        assert f"def {function_name}" in source


def test_extracted_content_is_not_forced_into_job_payload() -> None:
    sql = _sql().lower()
    extraction = sql[
        sql.index("create table if not exists content_extractions") :
        sql.index("create index if not exists content_extractions_attachment_idx")
    ]
    assert "attachment_id" in extraction
    assert "content_text text" in extraction
    assert "content_json jsonb" in extraction
    assert "unique (attachment_id, extraction_kind, version)" in extraction


def test_transport_messages_are_exactly_addressable() -> None:
    sql = _sql().lower()
    messages = sql[
        sql.index("create table if not exists messages") :
        sql.index("create index if not exists messages_user_received_idx")
    ]
    assert "unique (channel, external_chat_id, external_message_id)" in messages
    attachment = sql[
        sql.index("create table if not exists incoming_attachments") :
        sql.index("create index if not exists incoming_attachments_status_idx")
    ]
    assert "message_id uuid not null references messages(id)" in attachment
    assert "unique (message_id, external_file_id)" in attachment
