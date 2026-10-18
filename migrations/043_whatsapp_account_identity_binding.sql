-- Bind new physical WhatsApp actions to an explicitly pinned WAHA self JID.
-- Every column is additive; legacy rows remain NULL and are never backfilled.
ALTER TABLE whatsapp_account_controls
    ADD COLUMN pinned_account_jid TEXT,
    ADD COLUMN pinned_by UUID REFERENCES users(id) ON DELETE RESTRICT,
    ADD COLUMN pinned_at TIMESTAMPTZ,
    ADD COLUMN pin_observed_at TIMESTAMPTZ,
    ADD COLUMN pin_version BIGINT NOT NULL DEFAULT 0 CHECK (pin_version >= 0),
    ADD CONSTRAINT whatsapp_account_controls_pin_identity_check CHECK (
        (pinned_account_jid IS NULL AND pinned_by IS NULL AND pinned_at IS NULL
            AND pin_observed_at IS NULL AND pin_version=0)
        OR (pinned_account_jid IS NOT NULL
            AND pinned_account_jid ~ '^[0-9]{6,20}@c\.us$' AND pinned_by IS NOT NULL
            AND pinned_at IS NOT NULL AND pin_observed_at IS NOT NULL
            AND pin_version IS NOT NULL AND pin_version>0)
    );

ALTER TABLE whatsapp_account_send_reservations
    ADD COLUMN pinned_account_jid TEXT,
    ADD COLUMN account_pin_version BIGINT,
    ADD COLUMN observed_account_jid TEXT,
    ADD COLUMN account_identity_observed_at TIMESTAMPTZ,
    ADD CONSTRAINT whatsapp_send_reservations_account_identity_check CHECK (
        (pinned_account_jid IS NULL AND account_pin_version IS NULL
            AND observed_account_jid IS NULL AND account_identity_observed_at IS NULL)
        OR (pinned_account_jid IS NOT NULL AND account_pin_version IS NOT NULL
            AND observed_account_jid IS NOT NULL AND account_identity_observed_at IS NOT NULL
            AND pinned_account_jid ~ '^[0-9]{6,20}@c\.us$'
            AND account_pin_version>0 AND observed_account_jid=pinned_account_jid
            AND observed_account_jid ~ '^[0-9]{6,20}@c\.us$')
    );

ALTER TABLE whatsapp_send_acceptance_journal
    ADD COLUMN account_jid TEXT,
    ADD COLUMN account_pin_version BIGINT,
    ADD COLUMN account_identity_observed_at TIMESTAMPTZ,
    ADD CONSTRAINT whatsapp_send_acceptance_account_identity_check CHECK (
        (account_jid IS NULL AND account_pin_version IS NULL
            AND account_identity_observed_at IS NULL)
        OR (account_jid IS NOT NULL AND account_pin_version IS NOT NULL
            AND account_jid ~ '^[0-9]{6,20}@c\.us$' AND account_pin_version>0
            AND account_identity_observed_at IS NOT NULL)
    );

ALTER TABLE whatsapp_account_control_events
    DROP CONSTRAINT whatsapp_account_control_events_event_type_check,
    ADD CONSTRAINT whatsapp_account_control_events_event_type_check CHECK (
        event_type IN (
            'session_bootstrapped','hold_set','hold_released','budget_configured',
            'provider_observed','probe_requested','send_reserved','send_accepted',
            'send_unknown','send_rejected','circuit_opened','circuit_recovered',
            'account_identity_pinned','account_identity_changed','account_identity_observed',
            'account_identity_mismatch','account_identity_check_failed'
        )
    );
