-- Coalesced preparation and bounded history traversal; old evidence/jobs are retained.
ALTER TABLE whatsapp_account_controls
    ADD COLUMN next_probe_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE TABLE case_outreach_requests (
    case_id UUID PRIMARY KEY REFERENCES procurement_cases(id) ON DELETE CASCADE,
    dirty_generation BIGINT NOT NULL DEFAULT 0,
    prepared_generation BIGINT NOT NULL DEFAULT 0,
    current_job_id UUID REFERENCES jobs(id) ON DELETE RESTRICT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE case_outreach_sources (
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(case_id, source_key)
);
CREATE TABLE outreach_coalesced_jobs (
    original_job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE RESTRICT,
    canonical_job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    previous_error_code TEXT NOT NULL,
    previous_attempts INTEGER NOT NULL,
    coalesced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE supplier_conversations
    ADD COLUMN history_page_offset INTEGER NOT NULL DEFAULT 0 CHECK(history_page_offset >= 0),
    ADD COLUMN history_window_start TIMESTAMPTZ,
    ADD COLUMN history_window_end TIMESTAMPTZ,
    ADD COLUMN history_page_fingerprint TEXT NOT NULL DEFAULT '';
