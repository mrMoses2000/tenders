-- A physical WhatsApp chat has one durable owner, even when its conversation
-- targets several items. Generation fences renewals/releases after lease loss.
CREATE TABLE IF NOT EXISTS chat_actor_leases (
    actor_id UUID NOT NULL DEFAULT gen_random_uuid(),
    session_name TEXT NOT NULL CHECK (session_name <> ''),
    external_chat_id TEXT NOT NULL CHECK (external_chat_id <> ''),
    owner_worker_id TEXT,
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_name,external_chat_id),
    UNIQUE (actor_id),
    CHECK (
        (owner_worker_id IS NULL AND lease_expires_at IS NULL)
        OR (owner_worker_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS chat_actor_leases_expiry_idx
    ON chat_actor_leases(lease_expires_at)
    WHERE owner_worker_id IS NOT NULL;

-- The assignment links an item and supplier to one of migration 025's stable
-- lane IDs, while conversation_id points at the shared physical dialogue.
CREATE TABLE IF NOT EXISTS supplier_dialogue_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES research_item_groups(id) ON DELETE CASCADE,
    lane_id UUID NOT NULL,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    conversation_id UUID NOT NULL REFERENCES supplier_conversations(id) ON DELETE RESTRICT,
    target INTEGER NOT NULL DEFAULT 20 CHECK (target > 0),
    lane_count INTEGER NOT NULL DEFAULT 4 CHECK (lane_count = 4),
    max_per_lane INTEGER NOT NULL DEFAULT 5 CHECK (max_per_lane > 0),
    status TEXT NOT NULL DEFAULT 'assigned' CHECK (
        status IN ('assigned','waiting_reply','complete','released','failed')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (lane_id,group_id)
        REFERENCES research_lanes(id,group_id) ON DELETE RESTRICT,
    UNIQUE (group_id,request_item_id,supplier_id)
);

CREATE INDEX IF NOT EXISTS supplier_dialogue_assignments_lane_active_idx
    ON supplier_dialogue_assignments(lane_id,status,created_at)
    WHERE status IN ('assigned','waiting_reply');
CREATE INDEX IF NOT EXISTS supplier_dialogue_assignments_conversation_idx
    ON supplier_dialogue_assignments(conversation_id,status);
