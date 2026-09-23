-- A user's claim that an item is closed never changes item status directly.
-- It creates a durable request for a receipt/waybill first.

CREATE TABLE IF NOT EXISTS purchase_evidence_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE RESTRICT,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    requested_in_message_id UUID NOT NULL UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
    incoming_attachment_id UUID UNIQUE REFERENCES incoming_attachments(id) ON DELETE RESTRICT,
    source_artifact_id UUID UNIQUE REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'awaiting_document' CHECK (
        status IN ('awaiting_document','document_received','awaiting_details','completed','cancelled')
    ),
    raw_statement TEXT NOT NULL CHECK (btrim(raw_statement) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (request_item_id, case_id)
        REFERENCES request_items(id, case_id) ON DELETE RESTRICT,
    CHECK ((status='completed' AND completed_at IS NOT NULL) OR status<>'completed')
);
CREATE UNIQUE INDEX IF NOT EXISTS purchase_evidence_one_waiting_per_owner_idx
    ON purchase_evidence_requests(owner_user_id)
    WHERE status='awaiting_document';
CREATE INDEX IF NOT EXISTS purchase_evidence_case_status_idx
    ON purchase_evidence_requests(case_id,status,created_at);
