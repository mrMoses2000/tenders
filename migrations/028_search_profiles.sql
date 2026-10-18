-- Versioned semantic discovery guidance. Existing approved source plans and
-- research_queries remain immutable; an intent may be linked to a query later.

CREATE TABLE IF NOT EXISTS research_search_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    profile_version INTEGER NOT NULL CHECK (profile_version > 0),
    snapshot_sha256 TEXT NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    context_sha256 TEXT NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
    plan_sha256 TEXT NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
    model_version TEXT NOT NULL CHECK (model_version <> ''),
    prompt_version TEXT NOT NULL CHECK (prompt_version <> ''),
    profile JSONB NOT NULL CHECK (jsonb_typeof(profile)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (research_run_id, request_item_id, profile_version),
    UNIQUE (id, research_run_id, request_item_id),
    FOREIGN KEY (research_run_id, request_item_id)
        REFERENCES research_item_groups(research_run_id, request_item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS research_search_profiles_item_idx
    ON research_search_profiles(research_run_id, request_item_id, profile_version DESC);

CREATE TABLE IF NOT EXISTS research_search_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID NOT NULL,
    research_run_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    research_query_id UUID REFERENCES research_queries(id) ON DELETE SET NULL,
    intent_version INTEGER NOT NULL DEFAULT 1 CHECK (intent_version > 0),
    ordinal SMALLINT NOT NULL CHECK (ordinal BETWEEN 0 AND 23),
    provider TEXT NOT NULL CHECK (
        provider IN ('own_database','2gis_web','web','marketplace')
    ),
    intent_type TEXT NOT NULL CHECK (intent_type IN ('product','business')),
    category TEXT NOT NULL CHECK (category <> '' AND length(category) <= 160),
    query_text TEXT NOT NULL CHECK (query_text <> '' AND length(query_text) <= 500),
    rationale TEXT NOT NULL CHECK (rationale <> '' AND length(rationale) <= 500),
    uncertainty NUMERIC(4,3) NOT NULL CHECK (uncertainty BETWEEN 0 AND 1),
    scope_revision INTEGER NOT NULL CHECK (scope_revision > 0),
    intent_sha256 TEXT NOT NULL CHECK (intent_sha256 ~ '^[0-9a-f]{64}$'),
    limitations JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(limitations)='array'),
    cursor_state JSONB NOT NULL DEFAULT '{"current":null,"seen":[]}'::jsonb
        CHECK (
            jsonb_typeof(cursor_state)='object'
            AND jsonb_typeof(cursor_state->'current') IN ('string','null')
            AND jsonb_typeof(cursor_state->'seen')='array'
        ),
    results JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(results)='object'),
    stop_reason TEXT NOT NULL DEFAULT '' CHECK (length(stop_reason) <= 500),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_id, ordinal),
    FOREIGN KEY (profile_id, research_run_id, request_item_id)
        REFERENCES research_search_profiles(id, research_run_id, request_item_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS research_search_intents_ready_idx
    ON research_search_intents(provider, created_at, id) WHERE stop_reason='';
CREATE INDEX IF NOT EXISTS research_search_intents_item_idx
    ON research_search_intents(research_run_id, request_item_id, scope_revision, ordinal);
