-- Media is stored separately from the immutable inbound message evidence.
CREATE TABLE IF NOT EXISTS supplier_message_media (
    message_id UUID PRIMARY KEY REFERENCES supplier_conversation_messages(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('image','audio')),
    remote_url TEXT,
    mime_type TEXT,
    filename TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','downloaded','submitted','ready','failed')),
    object_key TEXT,
    sha256 TEXT CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT CHECK (byte_size IS NULL OR byte_size >= 0),
    provider_request_id TEXT,
    extracted_text TEXT,
    provider TEXT,
    language TEXT,
    confidence DOUBLE PRECISION CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    needs_clarification BOOLEAN NOT NULL DEFAULT FALSE,
    clarification_reason TEXT,
    analysis_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(analysis_json) = 'object'),
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS supplier_message_media_pending_idx
    ON supplier_message_media(state, updated_at)
    WHERE state IN ('pending','downloaded','submitted');
