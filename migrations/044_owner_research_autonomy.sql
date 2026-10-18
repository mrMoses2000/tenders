-- Opt-in research only. This migration enrolls nobody and grants no outbound consent.
ALTER TABLE customers ADD CONSTRAINT customers_id_owner_unique UNIQUE (id,owner_user_id);

CREATE TABLE owner_research_automation_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    customer_id UUID,
    version INTEGER NOT NULL CHECK (version > 0),
    research_autostart BOOLEAN NOT NULL DEFAULT FALSE,
    policy JSONB NOT NULL CHECK (jsonb_typeof(policy)='object'),
    policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    consent_reference TEXT NOT NULL CHECK (btrim(consent_reference) <> ''),
    consent_text TEXT NOT NULL CHECK (btrim(consent_text) <> ''),
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by UUID REFERENCES users(id) ON DELETE RESTRICT,
    FOREIGN KEY (customer_id,owner_user_id) REFERENCES customers(id,owner_user_id),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK (revoked_at IS NULL OR revoked_by=owner_user_id)
);
CREATE UNIQUE INDEX owner_research_profile_active_owner ON owner_research_automation_profiles
    (owner_user_id) WHERE customer_id IS NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX owner_research_profile_active_project ON owner_research_automation_profiles
    (owner_user_id,customer_id) WHERE customer_id IS NOT NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX owner_research_profile_version_owner ON owner_research_automation_profiles
    (owner_user_id,version) WHERE customer_id IS NULL;
CREATE UNIQUE INDEX owner_research_profile_version_project ON owner_research_automation_profiles
    (owner_user_id,customer_id,version) WHERE customer_id IS NOT NULL;

CREATE FUNCTION guard_research_profile_episode() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'research consent episodes cannot be deleted'; END IF;
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revoked_by<>OLD.owner_user_id
       OR (to_jsonb(NEW)-'revoked_at'-'revoked_by') <>
          (to_jsonb(OLD)-'revoked_at'-'revoked_by') THEN
        RAISE EXCEPTION 'research consent episodes are immutable except owner revocation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER research_profile_episode_immutable BEFORE UPDATE OR DELETE
    ON owner_research_automation_profiles FOR EACH ROW
    EXECUTE FUNCTION guard_research_profile_episode();

CREATE TABLE case_research_policy_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    parse_version_id UUID NOT NULL REFERENCES case_parse_versions(id) ON DELETE RESTRICT,
    intake_version INTEGER NOT NULL CHECK (intake_version>0),
    profile_id UUID NOT NULL REFERENCES owner_research_automation_profiles(id) ON DELETE RESTRICT,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    customer_id UUID,
    source_payload_sha256 TEXT NOT NULL CHECK (source_payload_sha256 ~ '^[0-9a-f]{64}$'),
    applied_payload_sha256 TEXT NOT NULL CHECK (applied_payload_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    snapshot_sha256 TEXT NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id,intake_version),
    UNIQUE (parse_version_id),
    FOREIGN KEY (customer_id,owner_user_id) REFERENCES customers(id,owner_user_id)
);

CREATE FUNCTION guard_research_snapshot_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM procurement_cases c
        JOIN case_parse_versions v ON v.case_id=c.id
        JOIN owner_research_automation_profiles p ON p.id=NEW.profile_id
        JOIN users u ON u.id=c.owner_user_id AND u.active
        WHERE c.id=NEW.case_id AND v.id=NEW.parse_version_id
          AND v.version=NEW.intake_version AND v.status='applied'
          AND c.owner_user_id=NEW.owner_user_id AND p.owner_user_id=NEW.owner_user_id
          AND c.customer_id IS NOT DISTINCT FROM NEW.customer_id
          AND p.customer_id IS NOT DISTINCT FROM NEW.customer_id
          AND p.research_autostart AND p.revoked_at IS NULL
    ) THEN RAISE EXCEPTION 'research snapshot scope is not authorized'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER research_snapshot_scope BEFORE INSERT ON case_research_policy_snapshots
    FOR EACH ROW EXECUTE FUNCTION guard_research_snapshot_scope();

CREATE FUNCTION reject_research_fact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'research authorization facts are immutable'; END $$;
CREATE TRIGGER research_snapshot_immutable BEFORE UPDATE OR DELETE
    ON case_research_policy_snapshots FOR EACH ROW EXECUTE FUNCTION reject_research_fact_mutation();

CREATE TABLE research_policy_authorizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id UUID NOT NULL UNIQUE REFERENCES case_research_policy_snapshots(id),
    research_run_id UUID NOT NULL UNIQUE REFERENCES research_runs(id),
    plan_sha256 TEXT NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
    binding JSONB NOT NULL CHECK (jsonb_typeof(binding)='object'),
    binding_sha256 TEXT NOT NULL CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER research_authorization_immutable BEFORE UPDATE OR DELETE
    ON research_policy_authorizations FOR EACH ROW EXECUTE FUNCTION reject_research_fact_mutation();

ALTER TABLE case_pipeline_runtime
    ADD COLUMN research_policy_snapshot_id UUID REFERENCES case_research_policy_snapshots(id),
    ADD COLUMN active_city_tier INTEGER CHECK (active_city_tier>=0),
    ADD COLUMN report_checkpoint_at TIMESTAMPTZ;

-- Sourcing evidence is distinct from accounting and never creates a purchase.
CREATE TABLE item_sourcing_dispositions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id),
    request_item_id UUID NOT NULL,
    owner_user_id UUID NOT NULL REFERENCES users(id),
    source_artifact_id UUID NOT NULL REFERENCES source_artifacts(id),
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    disposition TEXT NOT NULL CHECK (disposition IN ('bought','skip','reduce_quantity')),
    quantity NUMERIC(18,6) CHECK (quantity>0),
    unit TEXT NOT NULL CHECK (btrim(unit)<>''),
    owner_assertion TEXT NOT NULL CHECK (btrim(owner_assertion)<>''),
    exact_match_rationale TEXT NOT NULL CHECK (btrim(exact_match_rationale)<>''),
    evidence_sha256 TEXT NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (request_item_id,case_id) REFERENCES request_items(id,case_id),
    CHECK (disposition='skip' OR quantity IS NOT NULL)
);
CREATE TRIGGER sourcing_disposition_immutable BEFORE UPDATE OR DELETE
    ON item_sourcing_dispositions FOR EACH ROW EXECUTE FUNCTION reject_research_fact_mutation();
