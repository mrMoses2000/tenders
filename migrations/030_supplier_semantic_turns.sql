-- Durable single-item turns for a supplier conversation that may target
-- several offers. Turn history supplements, and never mutates, the immutable
-- conversation target and message records.

CREATE UNIQUE INDEX IF NOT EXISTS supplier_conversation_targets_id_conversation_idx
    ON supplier_conversation_targets(id,conversation_id);

CREATE TABLE IF NOT EXISTS supplier_conversation_item_turns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES supplier_conversations(id) ON DELETE RESTRICT,
    target_id UUID NOT NULL,
    turn_no INTEGER NOT NULL CHECK (turn_no > 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued','awaiting_approval','pending_reply','needs_time','referred',
            'answered','unavailable','ambiguous','closed'
        )
    ),
    draft_text TEXT NOT NULL CHECK (draft_text <> ''),
    approval_id UUID REFERENCES action_approvals(id) ON DELETE RESTRICT,
    inbound_message_id UUID REFERENCES supplier_conversation_messages(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL DEFAULT '',
    next_check_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (target_id,conversation_id)
        REFERENCES supplier_conversation_targets(id,conversation_id) ON DELETE RESTRICT,
    UNIQUE (conversation_id,turn_no),
    CHECK (
        (status='awaiting_approval' AND approval_id IS NOT NULL)
        OR status<>'awaiting_approval'
    ),
    CHECK (
        (status='needs_time' AND next_check_at IS NOT NULL)
        OR status<>'needs_time'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS supplier_conversation_one_open_turn_idx
    ON supplier_conversation_item_turns(conversation_id)
    WHERE status IN (
        'awaiting_approval','pending_reply','needs_time','ambiguous'
    );
CREATE INDEX IF NOT EXISTS supplier_conversation_turn_queue_idx
    ON supplier_conversation_item_turns(conversation_id,turn_no)
    WHERE status='queued';
