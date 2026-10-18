-- Account/session-wide WhatsApp controls. New sessions start held; only an
-- authenticated operator action may release the explicit hold.
CREATE TABLE whatsapp_account_controls (
    session_name TEXT PRIMARY KEY CHECK (session_name <> ''),
    operator_held BOOLEAN NOT NULL DEFAULT TRUE,
    operator_hold_reason TEXT NOT NULL DEFAULT 'new_session_fail_closed',
    operator_hold_actor UUID REFERENCES users(id) ON DELETE RESTRICT,
    operator_hold_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider_state TEXT NOT NULL DEFAULT 'unknown' CHECK (
        provider_state IN ('unknown','clear','restricted','timelock','capped','unavailable')
    ),
    provider_observed_at TIMESTAMPTZ,
    provider_source TEXT NOT NULL DEFAULT '',
    provider_restriction_until TIMESTAMPTZ,
    circuit_open BOOLEAN NOT NULL DEFAULT FALSE,
    circuit_reason TEXT NOT NULL DEFAULT '',
    circuit_opened_at TIMESTAMPTZ,
    daily_message_limit INTEGER NOT NULL DEFAULT 30 CHECK (daily_message_limit >= 0),
    daily_new_contact_limit INTEGER NOT NULL DEFAULT 0 CHECK (daily_new_contact_limit >= 0),
    minimum_interval_seconds INTEGER NOT NULL DEFAULT 120 CHECK (minimum_interval_seconds >= 0),
    max_concurrent_posts INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrent_posts BETWEEN 1 AND 4),
    last_post_reserved_at TIMESTAMPTZ,
    post_generation_sequence BIGINT NOT NULL DEFAULT 0 CHECK (post_generation_sequence >= 0),
    probe_requested_at TIMESTAMPTZ,
    probe_request_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((provider_state='unknown' AND provider_observed_at IS NULL)
        OR (provider_state<>'unknown' AND provider_observed_at IS NOT NULL))
);

CREATE TABLE whatsapp_account_send_reservations (
    outbox_event_id UUID PRIMARY KEY REFERENCES outbox_events(id) ON DELETE RESTRICT,
    session_name TEXT NOT NULL REFERENCES whatsapp_account_controls(session_name) ON DELETE RESTRICT,
    conversation_id UUID NOT NULL REFERENCES supplier_conversations(id) ON DELETE RESTRICT,
    supplier_contact_id UUID NOT NULL REFERENCES supplier_contacts(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('reserved','accepted','unknown','rejected')),
    is_new_contact BOOLEAN NOT NULL,
    reserved_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    outcome_source TEXT NOT NULL DEFAULT 'reservation' CHECK (
        outcome_source IN ('migration','reservation','http','reconciliation','reconstructed_native_trace','operator_reconciliation','provider_rejection')
    ),
    post_worker TEXT,
    post_generation BIGINT,
    post_lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((state='accepted' AND accepted_at IS NOT NULL) OR state<>'accepted'),
    CHECK (
        (post_worker IS NULL AND post_generation IS NULL AND post_lease_expires_at IS NULL)
        OR (post_worker IS NOT NULL AND post_generation IS NOT NULL
            AND post_lease_expires_at IS NOT NULL)
    )
);
CREATE INDEX whatsapp_account_send_budget_idx
    ON whatsapp_account_send_reservations(session_name,reserved_at)
    WHERE state IN ('reserved','accepted','unknown');
CREATE INDEX whatsapp_account_new_contact_budget_idx
    ON whatsapp_account_send_reservations(session_name,supplier_contact_id,reserved_at)
    WHERE is_new_contact AND state IN ('reserved','accepted','unknown');

CREATE TABLE whatsapp_account_control_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_name TEXT NOT NULL CHECK (session_name <> ''),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'session_bootstrapped','hold_set','hold_released','budget_configured',
        'provider_observed','probe_requested','send_reserved','send_accepted',
        'send_unknown','send_rejected','circuit_opened','circuit_recovered'
    )),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    actor_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    outbox_event_id UUID REFERENCES outbox_events(id) ON DELETE RESTRICT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX whatsapp_account_control_events_session_idx
    ON whatsapp_account_control_events(session_name,created_at DESC);

CREATE FUNCTION reject_whatsapp_account_control_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'WhatsApp account control events are immutable';
END;
$$;
CREATE TRIGGER whatsapp_account_control_events_immutable
    BEFORE UPDATE OR DELETE ON whatsapp_account_control_events
    FOR EACH ROW EXECUTE FUNCTION reject_whatsapp_account_control_event_mutation();

-- Bootstrap every session we can identify. This is deliberately independent
-- of the WAHA session's `WORKING` status and keeps the account held after deploy.
INSERT INTO whatsapp_account_controls(session_name,operator_held,operator_hold_reason)
SELECT session_name,TRUE,'migration_038_default_hold'
FROM (
    SELECT session_name FROM supplier_conversations
    UNION SELECT session_name FROM waha_webhook_events
    UNION SELECT session_name FROM whatsapp_send_acceptance_journal
) known_sessions
WHERE btrim(session_name)<>''
ON CONFLICT (session_name) DO NOTHING;

INSERT INTO whatsapp_account_control_events(session_name,event_type,reason,details)
SELECT session_name,'session_bootstrapped','migration_038_default_hold',
       jsonb_build_object('operator_held',TRUE,'provider_state','unknown')
FROM whatsapp_account_controls controls
WHERE controls.operator_hold_reason='migration_038_default_hold'
  AND NOT EXISTS (
      SELECT 1 FROM whatsapp_account_control_events event
      WHERE event.session_name=controls.session_name
        AND event.event_type='session_bootstrapped'
  );

-- Preserve existing successful ledger actions and any action that reserved a
-- provider id, even when projection/acceptance could not be proven. Pending or
-- dead actions that never reserved a provider id have not consumed a physical
-- account slot; they will reserve normally at final dispatch.
WITH physical_actions AS (
    SELECT event.id AS outbox_event_id,conversation.session_name,
           conversation.id AS conversation_id,conversation.supplier_contact_id,
           event.created_at AS reserved_at,
           CASE WHEN journal.outbox_event_id IS NOT NULL
                     OR message.outbox_event_id IS NOT NULL
                     OR event.external_id IS NOT NULL
                     OR event.status='sent'
                THEN 'accepted'
                ELSE 'unknown' END AS state,
           journal.accepted_at,
           CASE WHEN journal.outbox_event_id IS NOT NULL THEN journal.source
                ELSE 'migration' END AS outcome_source
    FROM outbox_events event
    JOIN supplier_conversations conversation
      ON conversation.id=CASE
          WHEN event.payload->>'conversation_id' ~*
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          THEN (event.payload->>'conversation_id')::uuid ELSE NULL END
    LEFT JOIN whatsapp_send_acceptance_journal journal
      ON journal.outbox_event_id=event.id
    LEFT JOIN supplier_conversation_messages message
      ON message.outbox_event_id=event.id AND message.direction='outbound'
    WHERE event.event_type='whatsapp.send_text'
      AND (event.provider_request_id IS NOT NULL OR event.external_id IS NOT NULL
        OR event.status='sent' OR message.outbox_event_id IS NOT NULL
        OR journal.outbox_event_id IS NOT NULL)
), ranked AS (
    SELECT action.*,
           row_number() OVER (
               PARTITION BY action.session_name,action.supplier_contact_id
               ORDER BY action.reserved_at,action.outbox_event_id
           )=1 AS is_new_contact
    FROM physical_actions action
)
INSERT INTO whatsapp_account_send_reservations(
    outbox_event_id,session_name,conversation_id,supplier_contact_id,state,
    is_new_contact,reserved_at,accepted_at,outcome_source
)
SELECT outbox_event_id,session_name,conversation_id,supplier_contact_id,state,
       is_new_contact,reserved_at,
       CASE WHEN state='accepted' THEN COALESCE(accepted_at,reserved_at) ELSE NULL END,
       outcome_source
FROM ranked
ON CONFLICT (outbox_event_id) DO NOTHING;

-- Any historical id reservation lacking acceptance proof keeps the session
-- circuit open. An empty/unavailable GET is not evidence to release it.
UPDATE whatsapp_account_controls controls SET
    circuit_open=TRUE,
    circuit_reason='unresolved_historical_provider_outcome',
    circuit_opened_at=now(),updated_at=now()
WHERE EXISTS (
    SELECT 1 FROM whatsapp_account_send_reservations reservation
    WHERE reservation.session_name=controls.session_name
      AND reservation.state='unknown'
);

INSERT INTO whatsapp_account_control_events(session_name,event_type,reason,details)
SELECT controls.session_name,'circuit_opened','unresolved_historical_provider_outcome',
       jsonb_build_object('unknown_reservations',(
           SELECT count(*) FROM whatsapp_account_send_reservations reservation
           WHERE reservation.session_name=controls.session_name
             AND reservation.state='unknown'
       ))
FROM whatsapp_account_controls controls
WHERE controls.circuit_open
  AND controls.circuit_reason='unresolved_historical_provider_outcome'
  AND NOT EXISTS (
      SELECT 1 FROM whatsapp_account_control_events event
      WHERE event.session_name=controls.session_name
        AND event.event_type='circuit_opened'
        AND event.reason='unresolved_historical_provider_outcome'
  );
