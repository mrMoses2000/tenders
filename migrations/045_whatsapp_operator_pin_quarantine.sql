-- Preserve authenticated operator pin retries and immutable evidence for exact
-- pre-043 legacy unknowns. Quarantine never changes reservation state/history.
ALTER TABLE whatsapp_account_control_events
    ADD COLUMN operator_idempotency_key TEXT;

CREATE UNIQUE INDEX whatsapp_account_control_events_operator_key_idx
    ON whatsapp_account_control_events(session_name,operator_idempotency_key)
    WHERE operator_idempotency_key IS NOT NULL;

ALTER TABLE whatsapp_account_control_events
    DROP CONSTRAINT whatsapp_account_control_events_event_type_check,
    ADD CONSTRAINT whatsapp_account_control_events_event_type_check CHECK (
        event_type IN (
            'session_bootstrapped','hold_set','hold_released','budget_configured',
            'provider_observed','probe_requested','send_reserved','send_accepted',
            'send_unknown','send_rejected','circuit_opened','circuit_recovered',
            'account_identity_pinned','account_identity_changed','account_identity_observed',
            'account_identity_mismatch','account_identity_check_failed',
            'legacy_unknown_quarantined'
        )
    );

CREATE TABLE whatsapp_legacy_unknown_quarantines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_name TEXT NOT NULL REFERENCES whatsapp_account_controls(session_name) ON DELETE RESTRICT,
    actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    operator_idempotency_key TEXT NOT NULL CHECK (btrim(operator_idempotency_key) <> ''),
    inventory_sha256 TEXT NOT NULL CHECK (inventory_sha256 ~ '^[0-9a-f]{64}$'),
    reservation_count INTEGER NOT NULL CHECK (reservation_count > 0),
    source_reference TEXT NOT NULL CHECK (
        source_reference ~ '^[A-Za-z0-9][A-Za-z0-9._:/#-]{2,159}$'
    ),
    reason TEXT NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 240),
    cutoff_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_name,operator_idempotency_key)
);

CREATE TABLE whatsapp_legacy_unknown_quarantine_items (
    quarantine_id UUID NOT NULL REFERENCES whatsapp_legacy_unknown_quarantines(id) ON DELETE RESTRICT,
    outbox_event_id UUID NOT NULL REFERENCES whatsapp_account_send_reservations(outbox_event_id) ON DELETE RESTRICT,
    PRIMARY KEY (quarantine_id,outbox_event_id),
    UNIQUE (outbox_event_id)
);
CREATE INDEX whatsapp_legacy_unknown_quarantine_items_event_idx
    ON whatsapp_legacy_unknown_quarantine_items(outbox_event_id);

CREATE FUNCTION reject_whatsapp_legacy_quarantine_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'legacy unknown quarantine attestations are immutable';
END;
$$;
CREATE TRIGGER whatsapp_legacy_unknown_quarantines_immutable
    BEFORE UPDATE OR DELETE ON whatsapp_legacy_unknown_quarantines
    FOR EACH ROW EXECUTE FUNCTION reject_whatsapp_legacy_quarantine_mutation();
CREATE TRIGGER whatsapp_legacy_unknown_quarantine_items_immutable
    BEFORE UPDATE OR DELETE ON whatsapp_legacy_unknown_quarantine_items
    FOR EACH ROW EXECUTE FUNCTION reject_whatsapp_legacy_quarantine_mutation();
