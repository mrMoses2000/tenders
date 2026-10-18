-- Durable fair scheduling and a timestamp for real persisted search progress.
ALTER TABLE research_item_groups
    ADD COLUMN scheduler_last_served_at TIMESTAMPTZ;

ALTER TABLE research_branch_checkpoints
    ADD COLUMN last_real_progress_at TIMESTAMPTZ;

UPDATE research_branch_checkpoints checkpoint
SET last_real_progress_at = (
    SELECT max(progress_at)
    FROM (
        SELECT page.created_at AS progress_at
        FROM research_discovery_pages page
        WHERE page.query_id = checkpoint.query_id
        UNION ALL
        SELECT evidence.captured_at AS progress_at
        FROM source_evidence evidence
        WHERE evidence.research_query_id = checkpoint.query_id
    ) existing_progress
);
