-- Operator-reviewed technical conformity is separate from an imported offer
-- status or a seller's unverified claim. Existing offers remain unreviewed.
CREATE UNIQUE INDEX IF NOT EXISTS item_requirements_id_item_idx
    ON item_requirements(id, request_item_id);
CREATE UNIQUE INDEX IF NOT EXISTS offers_id_item_idx
    ON offers(id,request_item_id);

CREATE TABLE IF NOT EXISTS offer_technical_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    requirement_id UUID,
    verdict TEXT NOT NULL CHECK (verdict IN ('match','mismatch')),
    note TEXT NOT NULL CHECK (length(btrim(note)) BETWEEN 3 AND 2000),
    source_evidence_id UUID REFERENCES source_evidence(id) ON DELETE RESTRICT,
    reviewed_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (offer_id, request_item_id)
        REFERENCES offers(id, request_item_id) ON DELETE CASCADE,
    FOREIGN KEY (requirement_id, request_item_id)
        REFERENCES item_requirements(id, request_item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS offer_technical_reviews_latest_idx
    ON offer_technical_reviews(offer_id,requirement_id,created_at DESC,id DESC);

CREATE OR REPLACE FUNCTION reject_offer_technical_review_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'offer technical reviews are immutable';
END;
$$;
CREATE TRIGGER offer_technical_reviews_immutable
BEFORE UPDATE ON offer_technical_reviews
FOR EACH ROW EXECUTE FUNCTION reject_offer_technical_review_mutation();
