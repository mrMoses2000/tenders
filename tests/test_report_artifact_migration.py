from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "005_report_artifacts.sql"


def test_report_artifacts_are_versioned_and_have_one_current_projection() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS report_artifacts" in sql
    assert "research_run_id UUID REFERENCES research_runs" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql
    assert "UNIQUE (case_id, version)" in sql
    assert "report_artifacts_one_current_idx" in sql
    assert "WHERE is_current" in sql
