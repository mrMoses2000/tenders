-- Durable operator visibility, without changing send evidence or job history.
CREATE TABLE pipeline_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('unknown_send','blocked_reply','reply_processing_failed')),
    subject_id UUID NOT NULL,
    conversation_id UUID REFERENCES supplier_conversations(id) ON DELETE CASCADE,
    reason_code TEXT NOT NULL CHECK (char_length(reason_code) BETWEEN 1 AND 200),
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (case_id,kind,subject_id),
    CHECK ((state='resolved') = (resolved_at IS NOT NULL))
);
CREATE INDEX pipeline_reviews_open_idx ON pipeline_reviews(case_id,created_at) WHERE state='open';
-- One owner notification per category per case, even when many contacts fail.
CREATE TABLE pipeline_review_alerts (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(case_id,kind)
);
