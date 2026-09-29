-- AI technical checks are advisory snapshots; only offer_technical_reviews
-- contain operator decisions that can affect comparison or ranking.
CREATE UNIQUE INDEX IF NOT EXISTS request_items_id_case_idx
    ON request_items(id,case_id);

CREATE TABLE IF NOT EXISTS offer_technical_assessment_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    request_item_id UUID NOT NULL,
    offer_id UUID NOT NULL,
    requested_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','proposed','stale','failed')
    ),
    spec_sha256 TEXT CHECK (spec_sha256 IS NULL OR spec_sha256 ~ '^[0-9a-f]{64}$'),
    context_sha256 TEXT CHECK (context_sha256 IS NULL OR context_sha256 ~ '^[0-9a-f]{64}$'),
    claims JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(claims)='array'),
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (offer_id,request_item_id)
        REFERENCES offers(id,request_item_id) ON DELETE CASCADE,
    FOREIGN KEY (request_item_id,case_id)
        REFERENCES request_items(id,case_id) ON DELETE CASCADE,
    CHECK (
        (status='queued' AND completed_at IS NULL)
        OR (status IN ('proposed','stale','failed') AND completed_at IS NOT NULL)
    ),
    CHECK (
        status NOT IN ('proposed','stale')
        OR (spec_sha256 IS NOT NULL AND context_sha256 IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS offer_technical_assessment_latest_idx
    ON offer_technical_assessment_proposals(offer_id,created_at DESC,id DESC);

CREATE OR REPLACE FUNCTION guard_offer_technical_assessment_proposal_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'queued' OR NEW.status NOT IN ('proposed','stale','failed') THEN
        RAISE EXCEPTION 'technical assessment proposal transition is invalid';
    END IF;
    IF (NEW.id,NEW.case_id,NEW.request_item_id,NEW.offer_id,NEW.requested_by,
        NEW.idempotency_key,NEW.created_at)
       IS DISTINCT FROM
       (OLD.id,OLD.case_id,OLD.request_item_id,OLD.offer_id,OLD.requested_by,
        OLD.idempotency_key,OLD.created_at) THEN
        RAISE EXCEPTION 'technical assessment proposal identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER offer_technical_assessment_proposal_update_guard
BEFORE UPDATE ON offer_technical_assessment_proposals
FOR EACH ROW EXECUTE FUNCTION guard_offer_technical_assessment_proposal_update();
