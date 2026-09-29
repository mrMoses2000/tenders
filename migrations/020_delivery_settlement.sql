-- Settlement and loader terms are separate facts from cargo movement and cost
-- allocation. Existing runs retain unknown terms and no inferred payments.
CREATE TABLE IF NOT EXISTS delivery_run_terms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL,
    delivery_run_id UUID NOT NULL,
    loading_mode TEXT NOT NULL CHECK (loading_mode IN
        ('unknown','included','separate','not_required')),
    included_loading_value NUMERIC(16,2) CHECK (
        included_loading_value IS NULL OR included_loading_value >= 0),
    note TEXT NOT NULL DEFAULT '',
    source_artifact_id UUID REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (delivery_run_id,case_id)
        REFERENCES delivery_runs(id,case_id) ON DELETE RESTRICT,
    CHECK (loading_mode='included' OR included_loading_value IS NULL)
);
CREATE INDEX IF NOT EXISTS delivery_run_terms_latest_idx
    ON delivery_run_terms(delivery_run_id,created_at DESC,id DESC);

CREATE TABLE IF NOT EXISTS delivery_payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL,
    delivery_run_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('payment','refund')),
    amount NUMERIC(16,2) NOT NULL CHECK (amount > 0),
    currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    paid_at TIMESTAMPTZ NOT NULL,
    payee TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source_artifact_id UUID REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (delivery_run_id,case_id)
        REFERENCES delivery_runs(id,case_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS delivery_payments_run_idx
    ON delivery_payments(delivery_run_id,paid_at,id);

CREATE OR REPLACE FUNCTION reject_delivery_settlement_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'delivery settlement facts are immutable';
END;
$$;
CREATE TRIGGER delivery_run_terms_immutable
BEFORE UPDATE OR DELETE ON delivery_run_terms
FOR EACH ROW EXECUTE FUNCTION reject_delivery_settlement_mutation();
CREATE TRIGGER delivery_payments_immutable
BEFORE UPDATE OR DELETE ON delivery_payments
FOR EACH ROW EXECUTE FUNCTION reject_delivery_settlement_mutation();
