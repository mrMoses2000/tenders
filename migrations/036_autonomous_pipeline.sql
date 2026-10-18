-- Additive execution metadata; immutable plans, targets and evidence are untouched.
CREATE TABLE case_pipeline_runtime (
    case_id UUID PRIMARY KEY REFERENCES procurement_cases(id) ON DELETE CASCADE,
    research_run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE RESTRICT,
    enabled BOOLEAN NOT NULL DEFAULT false,
    paused BOOLEAN NOT NULL DEFAULT false,
    next_tick_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tick_generation BIGINT NOT NULL DEFAULT 0,
    wake_reason TEXT NOT NULL DEFAULT 'enabled',
    last_tick_at TIMESTAMPTZ,
    last_tick_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX case_pipeline_due_idx ON case_pipeline_runtime(next_tick_at)
    WHERE enabled AND NOT paused;
CREATE TABLE research_provider_waits (
    research_run_id UUID PRIMARY KEY REFERENCES research_runs(id) ON DELETE CASCADE,
    next_probe_at TIMESTAMPTZ NOT NULL,
    reason TEXT NOT NULL,
    probe_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE research_branch_checkpoints
    ADD COLUMN recovery_base_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN recovery_cycles INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN recovery_due_at TIMESTAMPTZ,
    ADD COLUMN quota_waits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE supplier_conversations
    ADD COLUMN reply_due_at TIMESTAMPTZ,
    ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN waiting_reason TEXT NOT NULL DEFAULT '';
CREATE TABLE whatsapp_delivery_states (
    message_id UUID PRIMARY KEY REFERENCES supplier_conversation_messages(id) ON DELETE CASCADE,
    ack INTEGER CHECK (ack BETWEEN -1 AND 4),
    state TEXT NOT NULL DEFAULT 'accepted' CHECK (
        state IN ('unknown','accepted','server','delivered','read','played','failed')),
    last_checked_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error_code TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE whatsapp_delivery_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_name TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    ack INTEGER NOT NULL CHECK (ack BETWEEN -1 AND 4),
    source TEXT NOT NULL CHECK (source IN ('webhook','reconciliation')),
    received_at TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX whatsapp_delivery_receipt_message_idx
    ON whatsapp_delivery_receipts(session_name,external_message_id,ack);
INSERT INTO whatsapp_delivery_states(message_id,state)
SELECT id,'accepted' FROM supplier_conversation_messages WHERE direction='outbound';

ALTER TABLE research_discovery_cards
    ADD COLUMN recovery_base_attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX whatsapp_delivery_due_idx ON whatsapp_delivery_states(next_check_at);
CREATE TRIGGER whatsapp_delivery_receipts_immutable BEFORE UPDATE OR DELETE
    ON whatsapp_delivery_receipts FOR EACH ROW EXECUTE FUNCTION reject_supplier_message_mutation();

ALTER TABLE research_search_intents
    ADD COLUMN recovery_cycles INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN recovery_stop_reason TEXT,
    ADD COLUMN recovery_due_at TIMESTAMPTZ;

ALTER TABLE supplier_conversations
    ADD COLUMN history_check_at TIMESTAMPTZ,
    ADD COLUMN history_error_code TEXT NOT NULL DEFAULT '';
