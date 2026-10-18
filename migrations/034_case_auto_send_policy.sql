-- Explicit owner consent for autonomous WhatsApp dialogue is scoped to one case.
-- Episodes and their consent evidence remain durable after revocation.
CREATE TABLE IF NOT EXISTS case_auto_send_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    consent_reference TEXT NOT NULL CHECK (length(btrim(consent_reference)) > 0),
    consent_text TEXT NOT NULL CHECK (length(btrim(consent_text)) > 0),
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by UUID REFERENCES users(id) ON DELETE RESTRICT,
    CHECK ((revoked_at IS NULL AND revoked_by IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS case_auto_send_policies_one_active_idx
    ON case_auto_send_policies(case_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS case_auto_send_policies_case_history_idx
    ON case_auto_send_policies(case_id, authorized_at DESC);

-- This immutable audit link distinguishes grants created by case consent from
-- human-approved grants and preserves the exact payload hash and policy episode.
CREATE TABLE IF NOT EXISTS auto_send_approval_authorizations (
    approval_id UUID PRIMARY KEY REFERENCES action_approvals(id) ON DELETE RESTRICT,
    policy_id UUID NOT NULL REFERENCES case_auto_send_policies(id) ON DELETE RESTRICT,
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    authorized_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auto_send_approval_authorizations_policy_idx
    ON auto_send_approval_authorizations(policy_id, authorized_at DESC);
