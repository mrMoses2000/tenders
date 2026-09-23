-- Durable, explainable routing decision for every Telegram turn handled by the
-- procurement dialogue.  The extractor never chooses this intent and cannot
-- create or mutate a case unless this table records an intake-capable route.
CREATE TABLE IF NOT EXISTS dialogue_turns (
    message_id UUID PRIMARY KEY REFERENCES messages(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    case_id UUID REFERENCES procurement_cases(id) ON DELETE SET NULL,
    intent TEXT NOT NULL CHECK (
        intent IN (
            'create_request','update_request','case_query','close_position',
            'ordinary','active_case_conflict'
        )
    ),
    decision_source TEXT NOT NULL DEFAULT 'deterministic_rule' CHECK (
        decision_source='deterministic_rule'
    ),
    reason_code TEXT NOT NULL CHECK (reason_code <> ''),
    input_kind TEXT NOT NULL CHECK (
        input_kind IN ('text','document','voice','audio','photo','location')
    ),
    context_version INTEGER NOT NULL DEFAULT 0 CHECK (context_version >= 0),
    open_clarification_id UUID REFERENCES clarifications(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned','applied','ignored','failed')
    ),
    response_kind TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    CHECK ((status='planned' AND processed_at IS NULL)
           OR (status<>'planned' AND processed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS dialogue_turns_user_created_idx
    ON dialogue_turns(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS dialogue_turns_case_created_idx
    ON dialogue_turns(case_id, created_at DESC) WHERE case_id IS NOT NULL;
