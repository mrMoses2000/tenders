-- Versioned report artifacts. Files live in private content-addressed storage;
-- PostgreSQL stores identity, provenance and the current projection.

CREATE TABLE IF NOT EXISTS report_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    research_run_id UUID REFERENCES research_runs(id) ON DELETE SET NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    artifact_kind TEXT NOT NULL DEFAULT 'html' CHECK (artifact_kind='html'),
    storage_path TEXT NOT NULL CHECK (storage_path <> ''),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    summary JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(summary)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS report_artifacts_one_current_idx
    ON report_artifacts(case_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS report_artifacts_case_idx
    ON report_artifacts(case_id, version DESC);
