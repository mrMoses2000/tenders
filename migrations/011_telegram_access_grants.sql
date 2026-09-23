CREATE TABLE IF NOT EXISTS telegram_access_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bootstrap_username TEXT NOT NULL UNIQUE,
    telegram_id BIGINT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'username_bootstrap'
        CHECK (source IN ('username_bootstrap')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (bootstrap_username ~ '^[a-z0-9_]{5,32}$'),
    CHECK (telegram_id > 0)
);

COMMENT ON TABLE telegram_access_grants IS
    'One-time username bootstrap bindings. Once present, authorization follows telegram_id.';

CREATE OR REPLACE FUNCTION reject_telegram_access_identity_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.bootstrap_username IS DISTINCT FROM OLD.bootstrap_username
       OR NEW.telegram_id IS DISTINCT FROM OLD.telegram_id
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.bound_at IS DISTINCT FROM OLD.bound_at THEN
        RAISE EXCEPTION 'telegram access identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS telegram_access_identity_immutable ON telegram_access_grants;
CREATE TRIGGER telegram_access_identity_immutable
BEFORE UPDATE ON telegram_access_grants
FOR EACH ROW EXECUTE FUNCTION reject_telegram_access_identity_change();
