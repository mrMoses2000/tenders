from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "012_dialogue_turns.sql"


def test_dialogue_migration_persists_explainable_deterministic_routes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table if not exists dialogue_turns" in sql
    assert "message_id uuid primary key references messages" in sql
    assert "decision_source='deterministic_rule'" in sql
    for intent in (
        "create_request",
        "update_request",
        "case_query",
        "close_position",
        "ordinary",
        "active_case_conflict",
    ):
        assert f"'{intent}'" in sql
    assert "open_clarification_id" in sql
    assert "context_version" in sql
