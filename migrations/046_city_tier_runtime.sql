-- Opt-in city discovery metadata. No canonical/source rows or outbound consent change.
CREATE TABLE research_city_admissions (
    research_run_id UUID PRIMARY KEY REFERENCES research_runs(id),
    snapshot_id UUID NOT NULL REFERENCES case_research_policy_snapshots(id),
    case_id UUID NOT NULL REFERENCES procurement_cases(id),
    tier INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 2),
    city TEXT NOT NULL,
    enum_city TEXT NOT NULL,
    binding JSONB NOT NULL CHECK (jsonb_typeof(binding)='object'),
    binding_sha256 TEXT NOT NULL CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (research_run_id,snapshot_id,case_id,tier),
    CHECK ((tier=0 AND city='Тараз' AND enum_city='Taraz')
        OR (tier=1 AND city='Шымкент' AND enum_city='Shymkent')
        OR (tier=2 AND city='Алматы' AND enum_city='Almaty'))
);
CREATE TRIGGER research_city_admission_immutable BEFORE UPDATE OR DELETE
    ON research_city_admissions FOR EACH ROW EXECUTE FUNCTION reject_research_fact_mutation();

CREATE TABLE research_city_item_cursors (
    snapshot_id UUID NOT NULL REFERENCES case_research_policy_snapshots(id),
    case_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    tier INTEGER NOT NULL DEFAULT 0 CHECK (tier BETWEEN 0 AND 3),
    current_run_id UUID,
    satisfied BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id,request_item_id),
    FOREIGN KEY (request_item_id,case_id) REFERENCES request_items(id,case_id),
    FOREIGN KEY (current_run_id,snapshot_id,case_id,tier)
        REFERENCES research_city_admissions(research_run_id,snapshot_id,case_id,tier),
    CHECK (NOT satisfied OR current_run_id IS NULL),
    CHECK (tier<3 OR current_run_id IS NULL)
);

-- Append observations, including resumable states. A recovery never rewrites history.
CREATE TABLE research_city_item_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    case_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    tier INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('satisfied','exhausted','budget_complete',
        'blocked','paused','incomplete','failed','not_started')),
    proof JSONB NOT NULL CHECK (jsonb_typeof(proof)='object'),
    proof_sha256 TEXT NOT NULL CHECK (proof_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (request_item_id,case_id) REFERENCES request_items(id,case_id),
    FOREIGN KEY (research_run_id,snapshot_id,case_id,tier)
        REFERENCES research_city_admissions(research_run_id,snapshot_id,case_id,tier),
    UNIQUE (research_run_id,request_item_id,proof_sha256)
);
CREATE TRIGGER research_city_outcome_immutable BEFORE UPDATE OR DELETE
    ON research_city_item_outcomes FOR EACH ROW EXECUTE FUNCTION reject_research_fact_mutation();

-- One checkpoint per actual first approved start, independent of subsequent runs/ticks.
CREATE TABLE research_city_report_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id),
    snapshot_id UUID NOT NULL REFERENCES case_research_policy_snapshots(id),
    start_run_id UUID NOT NULL REFERENCES research_runs(id),
    started_at TIMESTAMPTZ NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    artifact_id UUID UNIQUE REFERENCES report_artifacts(id),
    outbox_event_id UUID UNIQUE REFERENCES outbox_events(id),
    completed_at TIMESTAMPTZ,
    UNIQUE (case_id,snapshot_id),
    CHECK (due_at=started_at+interval '4 hours'),
    CHECK ((artifact_id IS NULL AND outbox_event_id IS NULL AND completed_at IS NULL)
        OR (artifact_id IS NOT NULL AND outbox_event_id IS NOT NULL AND completed_at IS NOT NULL))
);
