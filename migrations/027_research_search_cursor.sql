-- Persist provider pagination independently of the approved, immutable query plan.
ALTER TABLE research_branch_checkpoints
    ADD COLUMN IF NOT EXISTS search_cursor_state JSONB NOT NULL
        DEFAULT '{"current": null, "seen": []}'::jsonb
        CHECK (
            jsonb_typeof(search_cursor_state) = 'object'
            AND jsonb_typeof(search_cursor_state->'current') IN ('string', 'null')
            AND jsonb_typeof(search_cursor_state->'seen') = 'array'
        );
