-- Milestone 2: durable WhatsApp webhook inbox and supplier conversation ledger.
-- This migration records transport facts only. It does not create an HTTP
-- endpoint, approve an action, enqueue an outbox event, or send a message.

-- Needed for a composite foreign key which proves that the selected contact
-- belongs to the supplier attached to a conversation.
CREATE UNIQUE INDEX IF NOT EXISTS supplier_contacts_id_supplier_idx
    ON supplier_contacts(id, supplier_id);

CREATE TABLE IF NOT EXISTS supplier_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    supplier_contact_id UUID NOT NULL,
    channel TEXT NOT NULL DEFAULT 'whatsapp' CHECK (channel='whatsapp'),
    session_name TEXT NOT NULL CHECK (session_name <> ''),
    external_chat_id TEXT NOT NULL CHECK (external_chat_id <> ''),
    state TEXT NOT NULL DEFAULT 'draft' CHECK (
        state IN (
            'draft','approved','waiting_availability','waiting_spec','waiting_price',
            'complete','escalated','closed_no_reply','opted_out'
        )
    ),
    opted_out BOOLEAN NOT NULL DEFAULT FALSE,
    opt_out_reason TEXT NOT NULL DEFAULT '',
    opted_out_at TIMESTAMPTZ,
    inbound_count INTEGER NOT NULL DEFAULT 0 CHECK (inbound_count >= 0),
    outbound_count INTEGER NOT NULL DEFAULT 0 CHECK (outbound_count >= 0),
    followup_count INTEGER NOT NULL DEFAULT 0 CHECK (followup_count >= 0),
    last_inbound_at TIMESTAMPTZ,
    last_outbound_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (supplier_contact_id, supplier_id)
        REFERENCES supplier_contacts(id, supplier_id) ON DELETE RESTRICT,
    UNIQUE (case_id, supplier_id, supplier_contact_id, session_name, external_chat_id),
    CHECK (
        (opted_out AND state='opted_out' AND opted_out_at IS NOT NULL)
        OR (NOT opted_out AND state<>'opted_out' AND opted_out_at IS NULL)
    ),
    CHECK (opted_out OR opt_out_reason='')
);
CREATE INDEX IF NOT EXISTS supplier_conversations_case_idx
    ON supplier_conversations(case_id, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS supplier_conversations_supplier_idx
    ON supplier_conversations(supplier_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS supplier_conversations_chat_idx
    ON supplier_conversations(session_name, external_chat_id);

-- Durable inbox. A handler first accepts the exact raw object here, then
-- transitions it through processing. Retries cannot silently replace evidence.
CREATE TABLE IF NOT EXISTS waha_webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_name TEXT NOT NULL CHECK (session_name <> ''),
    event_name TEXT NOT NULL CHECK (event_name <> ''),
    external_event_id TEXT NOT NULL DEFAULT '',
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    raw_payload JSONB NOT NULL CHECK (jsonb_typeof(raw_payload)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    status TEXT NOT NULL DEFAULT 'accepted' CHECK (
        status IN ('accepted','processing','processed','failed','ignored')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 50),
    error_code TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ NOT NULL,
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status='processing' AND locked_at IS NOT NULL AND locked_by IS NOT NULL)
        OR status<>'processing'
    ),
    CHECK (
        (status IN ('processed','ignored') AND processed_at IS NOT NULL)
        OR status NOT IN ('processed','ignored')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS waha_webhook_events_external_idx
    ON waha_webhook_events(session_name, event_name, external_event_id)
    WHERE external_event_id <> '';
CREATE INDEX IF NOT EXISTS waha_webhook_events_process_idx
    ON waha_webhook_events(status, received_at)
    WHERE status IN ('accepted','failed');

CREATE OR REPLACE FUNCTION protect_waha_webhook_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'WAHA webhook evidence is immutable';
    END IF;
    IF OLD.session_name IS DISTINCT FROM NEW.session_name
        OR OLD.event_name IS DISTINCT FROM NEW.event_name
        OR OLD.external_event_id IS DISTINCT FROM NEW.external_event_id
        OR OLD.payload_sha256 IS DISTINCT FROM NEW.payload_sha256
        OR OLD.raw_payload IS DISTINCT FROM NEW.raw_payload
        OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
        OR OLD.received_at IS DISTINCT FROM NEW.received_at
        OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts THEN
        RAISE EXCEPTION 'WAHA webhook evidence fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS waha_webhook_events_evidence_immutable
    ON waha_webhook_events;
CREATE TRIGGER waha_webhook_events_evidence_immutable
BEFORE UPDATE OR DELETE ON waha_webhook_events
FOR EACH ROW EXECUTE FUNCTION protect_waha_webhook_evidence();

-- Immutable transport evidence. Business projections such as availability and
-- price belong in offer_observations, derived from these original facts.
CREATE TABLE IF NOT EXISTS supplier_conversation_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES supplier_conversations(id) ON DELETE RESTRICT,
    webhook_event_id UUID UNIQUE REFERENCES waha_webhook_events(id) ON DELETE RESTRICT,
    outbox_event_id UUID UNIQUE REFERENCES outbox_events(id) ON DELETE RESTRICT,
    direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
    session_name TEXT NOT NULL CHECK (session_name <> ''),
    external_chat_id TEXT NOT NULL CHECK (external_chat_id <> ''),
    external_message_id TEXT NOT NULL CHECK (external_message_id <> ''),
    message_kind TEXT NOT NULL DEFAULT 'text' CHECK (
        message_kind IN ('text','image','document','audio','voice','video','location','contact','unknown')
    ),
    text_content TEXT NOT NULL DEFAULT '',
    raw_payload JSONB NOT NULL CHECK (jsonb_typeof(raw_payload)='object'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    is_opt_out BOOLEAN NOT NULL DEFAULT FALSE,
    is_followup BOOLEAN NOT NULL DEFAULT FALSE,
    received_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_name, external_chat_id, external_message_id),
    CHECK (
        (direction='inbound' AND webhook_event_id IS NOT NULL AND outbox_event_id IS NULL)
        OR (direction='outbound' AND webhook_event_id IS NULL AND outbox_event_id IS NOT NULL)
    ),
    CHECK (direction='outbound' OR NOT is_followup),
    CHECK (direction='inbound' OR NOT is_opt_out)
);
CREATE INDEX IF NOT EXISTS supplier_conversation_messages_timeline_idx
    ON supplier_conversation_messages(conversation_id, received_at, id);

CREATE OR REPLACE FUNCTION reject_supplier_message_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'supplier conversation messages are immutable';
END;
$$;

DROP TRIGGER IF EXISTS supplier_conversation_messages_immutable
    ON supplier_conversation_messages;
CREATE TRIGGER supplier_conversation_messages_immutable
BEFORE UPDATE OR DELETE ON supplier_conversation_messages
FOR EACH ROW EXECUTE FUNCTION reject_supplier_message_mutation();
