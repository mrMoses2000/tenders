from pathlib import Path


def test_accounting_schema_requires_evidence_and_exact_delivery_allocations() -> None:
    sql = (Path(__file__).parents[1] / "migrations/008_case_accounting.sql").read_text()

    assert "source_artifact_id UUID NOT NULL" in sql
    assert "CREATE TABLE IF NOT EXISTS purchase_items" in sql
    assert "CREATE TABLE IF NOT EXISTS customer_acceptance_events" in sql
    assert "CREATE TABLE IF NOT EXISTS delivery_carriers" in sql
    assert "CREATE TABLE IF NOT EXISTS delivery_allocations" in sql
    assert "tender_revenue_private" in sql
    assert "FOREIGN KEY (request_item_id, case_id)" in sql
