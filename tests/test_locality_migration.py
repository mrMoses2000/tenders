from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "006_locality_resolutions.sql"


def test_locality_migration_keeps_versioned_immutable_evidence() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS locality_resolutions" in sql
    assert "case_parse_version_id UUID NOT NULL REFERENCES case_parse_versions(id)" in sql
    assert "UNIQUE (case_id, intake_version, lookup_sha256, version)" in sql
    assert "candidates_sha256" in sql
    assert "result_sha256" in sql
    assert "selected_candidate JSONB" in sql
    assert "selected_candidate_sha256" in sql
    assert "context_token" in sql
    assert "locality_resolutions_immutable_update" in sql
    assert "BEFORE UPDATE ON locality_resolutions" in sql


def test_locality_migration_does_not_embed_transport_side_effects() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").casefold()

    assert "insert into jobs" not in sql
    assert "insert into outbox_events" not in sql
    assert "whatsapp" not in sql
