from pathlib import Path


def test_telegram_access_migration_pins_identity_immutably() -> None:
    source = Path("migrations/011_telegram_access_grants.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS telegram_access_grants" in source
    assert "bootstrap_username TEXT NOT NULL UNIQUE" in source
    assert "telegram_id BIGINT NOT NULL UNIQUE" in source
    assert "telegram_id > 0" in source
    assert "reject_telegram_access_identity_change" in source
    assert "BEFORE UPDATE ON telegram_access_grants" in source
