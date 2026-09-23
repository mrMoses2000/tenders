-- A supplier conversation can discuss several request items, but every item
-- must be identified by an exact offer.  This additive link table deliberately
-- leaves pre-existing conversations targetless so it can be deployed without a
-- lossy backfill.  Application workflows must require at least one target before
-- interpreting a supplier reply or authorising a new outbound conversation.

-- Composite keys let PostgreSQL prove the cross-table ownership invariants
-- instead of relying only on application-side checks.
CREATE UNIQUE INDEX IF NOT EXISTS request_items_id_case_idx
    ON request_items(id, case_id);
CREATE UNIQUE INDEX IF NOT EXISTS offers_id_item_supplier_idx
    ON offers(id, request_item_id, supplier_id);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_conversations_id_case_supplier_idx
    ON supplier_conversations(id, case_id, supplier_id);

CREATE TABLE IF NOT EXISTS supplier_conversation_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL,
    case_id UUID NOT NULL,
    supplier_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    offer_id UUID NOT NULL,
    provenance_kind TEXT NOT NULL CHECK (
        provenance_kind IN ('research','approval','operator','automation','import')
    ),
    provenance_ref TEXT NOT NULL CHECK (provenance_ref <> ''),
    source_evidence_id UUID REFERENCES source_evidence(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id, case_id, supplier_id)
        REFERENCES supplier_conversations(id, case_id, supplier_id) ON DELETE RESTRICT,
    FOREIGN KEY (offer_id, request_item_id, supplier_id)
        REFERENCES offers(id, request_item_id, supplier_id) ON DELETE RESTRICT,
    FOREIGN KEY (request_item_id, case_id)
        REFERENCES request_items(id, case_id) ON DELETE RESTRICT,
    UNIQUE (conversation_id, offer_id)
);
CREATE INDEX IF NOT EXISTS supplier_conversation_targets_conversation_idx
    ON supplier_conversation_targets(conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS supplier_conversation_targets_item_idx
    ON supplier_conversation_targets(request_item_id, conversation_id);

-- Target selection and its provenance are forensic workflow facts.  Corrections
-- are represented by a new conversation, never by rewriting the original link.
CREATE OR REPLACE FUNCTION reject_supplier_conversation_target_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'supplier conversation targets are immutable';
END;
$$;

DROP TRIGGER IF EXISTS supplier_conversation_targets_immutable
    ON supplier_conversation_targets;
CREATE TRIGGER supplier_conversation_targets_immutable
BEFORE UPDATE OR DELETE ON supplier_conversation_targets
FOR EACH ROW EXECUTE FUNCTION reject_supplier_conversation_target_mutation();
