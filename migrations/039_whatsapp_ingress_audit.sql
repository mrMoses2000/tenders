-- Bounded, redacted visibility for authenticated webhook dispositions which
-- return before the durable supplier-message ledger. Rows aggregate by UTC day
-- and fixed disposition dimensions, so request volume cannot create rows.
CREATE TABLE IF NOT EXISTS whatsapp_ingress_audit (
    utc_day DATE NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted','ignored','rejected')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    event_kind TEXT NOT NULL CHECK (
        event_kind IN ('message','message.ack','session.status','other','unknown')
    ),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    observation_count BIGINT NOT NULL DEFAULT 1 CHECK (observation_count > 0),
    sample_provider_id_fingerprint TEXT CHECK (
        sample_provider_id_fingerprint IS NULL
        OR sample_provider_id_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    PRIMARY KEY (utc_day, disposition, reason_code, event_kind),
    CHECK (first_seen_at <= last_seen_at)
);

CREATE INDEX IF NOT EXISTS whatsapp_ingress_audit_last_seen_idx
    ON whatsapp_ingress_audit(last_seen_at DESC);

-- Provider-authenticated @lid replies can contain useful supplier answers but
-- cannot yet be matched to a verified contact. Keep a bounded private holding
-- area for later proof-based resolution. Callers insert an allowlisted object;
-- this table is deliberately separate from the redacted aggregate above. The
-- writer caps open rows at 5,000, total rows at 10,000, and expires closed rows
-- after 60 days before accepting another message.
CREATE TABLE IF NOT EXISTS whatsapp_inbound_quarantine (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_name TEXT NOT NULL CHECK (length(session_name) BETWEEN 1 AND 200),
    provider_id_fingerprint TEXT NOT NULL CHECK (provider_id_fingerprint ~ '^[0-9a-f]{64}$'),
    provider_event_id TEXT NOT NULL CHECK (length(provider_event_id) BETWEEN 1 AND 500),
    sender_jid TEXT NOT NULL CHECK (
        length(sender_jid) BETWEEN 1 AND 500 AND sender_jid LIKE '%@lid'
    ),
    received_at TIMESTAMPTZ NOT NULL,
    message_kind TEXT NOT NULL CHECK (message_kind IN ('text','image','audio','other')),
    text_content TEXT NOT NULL CHECK (length(text_content) <= 100000),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','replayed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_contact_id UUID REFERENCES supplier_contacts(id) ON DELETE RESTRICT,
    resolution_source TEXT CHECK (resolution_source IS NULL OR resolution_source='waha_lids_api'),
    mapping_fingerprint TEXT CHECK (
        mapping_fingerprint IS NULL OR mapping_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    mapping_verified_at TIMESTAMPTZ,
    UNIQUE (session_name, provider_id_fingerprint),
    CHECK (
        (status='open' AND resolved_at IS NULL AND resolved_contact_id IS NULL
         AND resolution_source IS NULL AND mapping_fingerprint IS NULL
         AND mapping_verified_at IS NULL)
        OR (status<>'open' AND resolved_at IS NOT NULL AND resolved_contact_id IS NOT NULL
            AND resolution_source IS NOT NULL AND mapping_fingerprint IS NOT NULL
            AND mapping_verified_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS whatsapp_inbound_quarantine_open_idx
    ON whatsapp_inbound_quarantine(created_at,id) WHERE status='open';
