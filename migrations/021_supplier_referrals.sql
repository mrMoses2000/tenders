-- A supplier can refer a different person or business. Preserve the suggested
-- number without asserting supplier ownership or WhatsApp registration.
CREATE TABLE IF NOT EXISTS supplier_contact_referrals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES supplier_conversations(id) ON DELETE RESTRICT,
    message_id UUID NOT NULL REFERENCES supplier_conversation_messages(id) ON DELETE RESTRICT,
    source_evidence_id UUID NOT NULL REFERENCES source_evidence(id) ON DELETE RESTRICT,
    normalized_phone TEXT NOT NULL CHECK (normalized_phone ~ '^\+[0-9]{10,15}$'),
    status TEXT NOT NULL DEFAULT 'needs_review' CHECK (
        status IN ('needs_review','attached','rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id,normalized_phone)
);
CREATE INDEX IF NOT EXISTS supplier_contact_referrals_conversation_idx
    ON supplier_contact_referrals(conversation_id,created_at DESC);
