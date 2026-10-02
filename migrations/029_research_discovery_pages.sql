-- Additive storage; approved plans, provider evidence and offers are unchanged.
CREATE TABLE research_discovery_pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id UUID NOT NULL REFERENCES research_queries(id) ON DELETE CASCADE,
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(query_id,request_sha256)
);
CREATE TABLE research_discovery_cards (
    page_id UUID NOT NULL REFERENCES research_discovery_pages(id) ON DELETE CASCADE,
    ordinal SMALLINT NOT NULL CHECK (ordinal BETWEEN 0 AND 19),
    external_id TEXT NOT NULL,
    card_url TEXT NOT NULL,
    card_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','extracted','failed')),
    result_payload JSONB,
    card_facts_payload JSONB,
    contact_attempts INTEGER NOT NULL DEFAULT 0 CHECK (contact_attempts >= 0),
    contact_failure_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    failure_code TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(page_id,ordinal),
    UNIQUE(page_id,external_id)
);

ALTER TABLE research_branch_checkpoints ADD COLUMN source_progress_token TEXT
    CHECK (source_progress_token IS NULL OR source_progress_token ~ '^[a-f0-9]{64}$');

ALTER TABLE research_branch_checkpoints ADD COLUMN semantic_intent_ordinal SMALLINT
    NOT NULL DEFAULT 0 CHECK (semantic_intent_ordinal BETWEEN 0 AND 23);
