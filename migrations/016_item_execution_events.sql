-- Orthogonal quantity facts for fulfilment, rejection, return and cancellation.
-- Purchase and customer acceptance retain their existing authoritative tables.
CREATE TABLE IF NOT EXISTS request_item_execution_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'warehouse_received','customer_delivered','customer_rejected',
        'returned','cancelled'
    )),
    quantity_delta NUMERIC(18,6) NOT NULL CHECK (quantity_delta<>0),
    reason TEXT NOT NULL DEFAULT '',
    replaces_event_id UUID,
    source_artifact_id UUID REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    provenance TEXT NOT NULL CHECK (provenance IN ('user_reported','documented')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key<>''),
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id,request_item_id),
    FOREIGN KEY (request_item_id,case_id)
        REFERENCES request_items(id,case_id) ON DELETE RESTRICT,
    FOREIGN KEY (replaces_event_id,request_item_id)
        REFERENCES request_item_execution_events(id,request_item_id) ON DELETE RESTRICT,
    CHECK (provenance<>'documented' OR source_artifact_id IS NOT NULL),
    CHECK (quantity_delta>0 OR btrim(reason)<>''),
    CHECK (event_kind NOT IN ('customer_rejected','returned','cancelled')
           OR btrim(reason)<>''),
    CHECK (replaces_event_id IS NULL OR event_kind='customer_delivered')
);
CREATE INDEX IF NOT EXISTS request_item_execution_events_item_idx
    ON request_item_execution_events(request_item_id,occurred_at,id);
CREATE INDEX IF NOT EXISTS request_item_execution_events_case_idx
    ON request_item_execution_events(case_id,event_kind,occurred_at);

CREATE OR REPLACE VIEW request_item_execution_totals AS
SELECT request_item_id,
       count(*)::int AS event_count,
       COALESCE(sum(quantity_delta) FILTER (
           WHERE event_kind='warehouse_received'),0) AS warehouse_received_quantity,
       COALESCE(sum(quantity_delta) FILTER (
           WHERE event_kind='customer_delivered'),0) AS delivered_quantity,
       COALESCE(sum(quantity_delta) FILTER (
           WHERE event_kind='customer_rejected'),0) AS rejected_quantity,
       COALESCE(sum(quantity_delta) FILTER (
           WHERE event_kind='returned'),0) AS returned_quantity,
       COALESCE(sum(quantity_delta) FILTER (
           WHERE event_kind='cancelled'),0) AS cancelled_quantity
FROM request_item_execution_events
GROUP BY request_item_id;
