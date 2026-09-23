-- External actions are authorised by append-only grants. Historical approvals
-- are never rewritten into new requests; only one live request may exist for an
-- exact payload at a time.

CREATE TABLE IF NOT EXISTS action_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL CHECK (
        action_type IN ('start_research','contact_suppliers','send_whatsapp','select_offer')
    ),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'requested' CHECK (
        status IN ('requested','approved','consumed','rejected','revoked','expired')
    ),
    requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    CHECK ((status IN ('approved','consumed') AND approved_at IS NOT NULL)
        OR status NOT IN ('approved','consumed')),
    CHECK ((status='consumed' AND consumed_at IS NOT NULL) OR status<>'consumed')
);
CREATE UNIQUE INDEX IF NOT EXISTS action_approvals_one_live_payload_idx
    ON action_approvals(case_id, action_type, payload_sha256)
    WHERE status IN ('requested','approved');
CREATE INDEX IF NOT EXISTS action_approvals_case_status_idx
    ON action_approvals(case_id, status, requested_at DESC);

ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS approval_id UUID REFERENCES action_approvals(id) ON DELETE RESTRICT;
ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS provider_request_id TEXT;

ALTER TABLE outbox_events
    DROP CONSTRAINT IF EXISTS outbox_whatsapp_requires_approval;
ALTER TABLE outbox_events
    ADD CONSTRAINT outbox_whatsapp_requires_approval CHECK (
        event_type <> 'whatsapp.send_text' OR approval_id IS NOT NULL
    );

CREATE UNIQUE INDEX IF NOT EXISTS outbox_events_approval_idx
    ON outbox_events(approval_id) WHERE approval_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_events_provider_request_idx
    ON outbox_events(event_type, provider_request_id)
    WHERE provider_request_id IS NOT NULL;
