from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations" / "002_supplier_research.sql"


def test_supplier_migration_has_provenance_and_exact_identity_constraints() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS source_evidence" in sql
    assert "'own_database'" in sql
    assert "CREATE TABLE IF NOT EXISTS supplier_aliases" in sql
    assert "supplier_aliases_strong_identity_idx" in sql
    assert "supplier_contacts_active_identity_idx" in sql
    assert "contact_type IN ('phone','whatsapp') THEN 'phone'" in sql
    assert "supplier_locations_provider_place_idx" in sql
    assert "content_sha256" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql


def test_offer_observations_are_append_only_facts_with_unknowns() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    observation_block = sql.split(
        "CREATE TABLE IF NOT EXISTS offer_observations", maxsplit=1
    )[1]

    assert "price_amount NUMERIC(14,2)" in observation_block
    assert "price_amount NUMERIC(14,2) NOT NULL" not in observation_block
    assert "availability_status TEXT NOT NULL DEFAULT 'unknown'" in observation_block
    assert "CHECK (price_amount IS NULL OR currency IS NOT NULL)" in observation_block
    quantity_check = "CHECK (availability_status <> 'in_stock' OR available_qty IS NOT NULL)"
    assert quantity_check in observation_block


def test_supplier_migration_does_not_authorise_external_sends() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").casefold()

    assert "outbox_events" not in sql
    assert "action_approvals" in sql  # explanatory boundary in the migration header
    assert "send_whatsapp" not in sql
