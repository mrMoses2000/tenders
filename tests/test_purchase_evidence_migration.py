from pathlib import Path


def test_close_claim_requires_durable_document_workflow() -> None:
    sql = (
        Path(__file__).parents[1] / "migrations/010_purchase_evidence_requests.sql"
    ).read_text()

    assert "status TEXT NOT NULL DEFAULT 'awaiting_document'" in sql
    assert "incoming_attachment_id UUID UNIQUE" in sql
    assert "source_artifact_id UUID UNIQUE" in sql
    assert "purchase_evidence_one_waiting_per_owner_idx" in sql
    assert "FOREIGN KEY (request_item_id, case_id)" in sql
