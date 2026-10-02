-- Operator-authorized execution scope changes are separate immutable records.
-- The approved research_runs.search_scope envelope and its plan hash stay intact.
CREATE TABLE IF NOT EXISTS research_scope_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0),
    city TEXT NOT NULL CHECK (city <> ''),
    scope TEXT NOT NULL CHECK (scope = 'citywide'),
    preferred_area_text TEXT NOT NULL CHECK (preferred_area_text <> ''),
    authorization_reference TEXT NOT NULL CHECK (authorization_reference <> ''),
    authorized_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    base_plan_sha256 TEXT NOT NULL CHECK (base_plan_sha256 ~ '^[a-f0-9]{64}$'),
    revision_sha256 TEXT NOT NULL CHECK (revision_sha256 ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (research_run_id, revision),
    UNIQUE (research_run_id, authorization_reference)
);
CREATE INDEX IF NOT EXISTS research_scope_revisions_run_idx
    ON research_scope_revisions(research_run_id, revision DESC);

-- Rows are append-only through the repository API. They are separate from the
-- frozen plan, and are not linked to action_approvals or any outbound action.
