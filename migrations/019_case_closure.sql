-- Operator-reported purchases and an auditable case close/reopen decision.
-- Existing evidenced purchases keep their documented provenance.
ALTER TABLE purchase_documents
    ALTER COLUMN source_artifact_id DROP NOT NULL,
    ADD COLUMN provenance TEXT NOT NULL DEFAULT 'documented'
        CHECK (provenance IN ('documented','user_reported')),
    ADD COLUMN note TEXT NOT NULL DEFAULT '',
    ADD COLUMN request_hash TEXT CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT purchase_documents_source_provenance_check CHECK (
        (provenance='documented' AND source_artifact_id IS NOT NULL)
        OR (provenance='user_reported' AND btrim(note)<>'')
    );

ALTER TABLE request_item_execution_events
    ADD COLUMN delivery_run_id UUID,
    ADD CONSTRAINT request_item_execution_delivery_item_fk
        FOREIGN KEY (delivery_run_id,request_item_id)
        REFERENCES delivery_run_items(delivery_run_id,request_item_id) ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS request_item_execution_delivery_idx
    ON request_item_execution_events(delivery_run_id,request_item_id)
    WHERE delivery_run_id IS NOT NULL;

ALTER TABLE customer_acceptance_events
    ADD COLUMN delivery_run_id UUID,
    ADD COLUMN reason TEXT NOT NULL DEFAULT '',
    ADD CONSTRAINT customer_acceptance_delivery_item_fk
        FOREIGN KEY (delivery_run_id,request_item_id)
        REFERENCES delivery_run_items(delivery_run_id,request_item_id) ON DELETE RESTRICT;

CREATE TABLE IF NOT EXISTS case_closure_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('closed','reopened')),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL CHECK (to_status IN ('closed','selected')),
    reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key<>''),
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (event_kind<>'reopened' OR btrim(reason)<>'')
);
CREATE INDEX IF NOT EXISTS case_closure_events_case_idx
    ON case_closure_events(case_id,created_at,id);
