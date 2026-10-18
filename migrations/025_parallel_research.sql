-- Durable, bounded parallel research. Existing run/query rows and their hashes
-- remain immutable; this migration adds only scheduler/checkpoint state.

CREATE TABLE IF NOT EXISTS research_item_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    target_suppliers INTEGER NOT NULL DEFAULT 20 CHECK (target_suppliers > 0),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','researching','succeeded','partial','failed','paused')
    ),
    reason TEXT NOT NULL DEFAULT '',
    branches_total INTEGER NOT NULL DEFAULT 0 CHECK (branches_total >= 0),
    branches_succeeded INTEGER NOT NULL DEFAULT 0 CHECK (branches_succeeded >= 0),
    branches_failed INTEGER NOT NULL DEFAULT 0 CHECK (branches_failed >= 0),
    branches_blocked INTEGER NOT NULL DEFAULT 0 CHECK (branches_blocked >= 0),
    candidates_persisted INTEGER NOT NULL DEFAULT 0 CHECK (candidates_persisted >= 0),
    own_database_candidates INTEGER NOT NULL DEFAULT 0 CHECK (own_database_candidates >= 0),
    found_leads INTEGER NOT NULL DEFAULT 0 CHECK (found_leads >= 0),
    eligible_suppliers INTEGER NOT NULL DEFAULT 0 CHECK (eligible_suppliers >= 0),
    qualified_price_suppliers INTEGER NOT NULL DEFAULT 0 CHECK (qualified_price_suppliers >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (research_run_id, request_item_id)
);
CREATE INDEX IF NOT EXISTS research_item_groups_scheduler_idx
    ON research_item_groups(status, created_at, id);

CREATE TABLE IF NOT EXISTS research_lanes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES research_item_groups(id) ON DELETE CASCADE,
    lane_no SMALLINT NOT NULL CHECK (lane_no BETWEEN 0 AND 3),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','researching','succeeded','partial','failed','blocked','paused')
    ),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    owner_worker TEXT,
    lease_until TIMESTAMPTZ,
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (group_id, lane_no),
    UNIQUE (id, group_id)
);

-- One row per immutable research query; batch_no and exclusions are the durable
-- cursor for bounded follow-up searches. current_job_id fences stale job payloads.
CREATE TABLE IF NOT EXISTS research_branch_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    group_id UUID NOT NULL REFERENCES research_item_groups(id) ON DELETE CASCADE,
    lane_id UUID NOT NULL,
    query_id UUID NOT NULL REFERENCES research_queries(id) ON DELETE CASCADE,
    lane_no SMALLINT NOT NULL CHECK (lane_no BETWEEN 0 AND 3),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','researching','succeeded','failed','blocked','skipped','paused')
    ),
    legacy_uncheckpointed BOOLEAN NOT NULL DEFAULT false,
    current_batch_no INTEGER NOT NULL DEFAULT 0 CHECK (current_batch_no >= 0),
    batch_attempts INTEGER NOT NULL DEFAULT 0 CHECK (batch_attempts >= 0),
    max_batches INTEGER NOT NULL DEFAULT 20 CHECK (max_batches BETWEEN 1 AND 100),
    excluded_external_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(excluded_external_ids)='array'),
    candidate_receipts JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(candidate_receipts)='array'),
    pages_examined INTEGER NOT NULL DEFAULT 0 CHECK (pages_examined >= 0),
    candidates_persisted INTEGER NOT NULL DEFAULT 0 CHECK (candidates_persisted >= 0),
    own_database_candidates INTEGER NOT NULL DEFAULT 0 CHECK (own_database_candidates >= 0),
    current_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    owner_generation BIGINT NOT NULL DEFAULT 0 CHECK (owner_generation >= 0),
    owner_worker TEXT,
    lease_until TIMESTAMPTZ,
    last_error_code TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (lane_id, group_id) REFERENCES research_lanes(id, group_id) ON DELETE CASCADE,
    UNIQUE (research_run_id, query_id),
    UNIQUE (group_id, lane_no, query_id)
);
CREATE INDEX IF NOT EXISTS research_branch_checkpoints_group_idx
    ON research_branch_checkpoints(group_id, status, lane_no);
CREATE INDEX IF NOT EXISTS research_branch_checkpoints_job_idx
    ON research_branch_checkpoints(current_job_id) WHERE current_job_id IS NOT NULL;
