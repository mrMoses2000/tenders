-- Record a WAHA 2xx acceptance before projecting the supplier conversation.
-- This closes the retry window where the provider accepted a message but the
-- outbox/conversation transaction failed, which must never trigger another POST.
CREATE TABLE whatsapp_send_acceptance_journal (
    outbox_event_id UUID PRIMARY KEY REFERENCES outbox_events(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    session_name TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    provider_request_id TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    external_message_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (
        source IN ('http', 'reconciliation', 'reconstructed_native_trace')
    ),
    result_identity JSONB NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX whatsapp_send_acceptance_provider_request_idx
    ON whatsapp_send_acceptance_journal(session_name, provider_request_id);
CREATE TRIGGER whatsapp_send_acceptance_journal_immutable
    BEFORE UPDATE OR DELETE ON whatsapp_send_acceptance_journal
    FOR EACH ROW EXECUTE FUNCTION reject_supplier_message_mutation();
