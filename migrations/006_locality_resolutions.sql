-- Immutable, source-backed interpretations of colloquial locality phrases.
-- Browser output is retained as evidence, while the selected result is made by
-- application-owned policy before this row is written.

CREATE TABLE IF NOT EXISTS locality_resolutions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    case_parse_version_id UUID NOT NULL REFERENCES case_parse_versions(id) ON DELETE RESTRICT,
    intake_version INTEGER NOT NULL CHECK (intake_version > 0),
    version INTEGER NOT NULL CHECK (version > 0),
    city TEXT NOT NULL CHECK (btrim(city) <> ''),
    phrase TEXT NOT NULL CHECK (btrim(phrase) <> ''),
    lookup_sha256 TEXT NOT NULL CHECK (lookup_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (
        status IN ('resolved','needs_clarification','unresolved')
    ),
    candidates JSONB NOT NULL CHECK (jsonb_typeof(candidates)='array'),
    candidates_sha256 TEXT NOT NULL CHECK (candidates_sha256 ~ '^[0-9a-f]{64}$'),
    result_payload JSONB NOT NULL CHECK (jsonb_typeof(result_payload)='object'),
    result_sha256 TEXT NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    selected_candidate JSONB,
    selected_candidate_sha256 TEXT,
    clarification_question TEXT,
    context_token TEXT NOT NULL CHECK (context_token ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, intake_version, lookup_sha256, version),
    CHECK (
        (status='resolved'
            AND selected_candidate IS NOT NULL
            AND jsonb_typeof(selected_candidate)='object'
            AND selected_candidate_sha256 ~ '^[0-9a-f]{64}$'
            AND clarification_question IS NULL)
        OR
        (status<>'resolved'
            AND selected_candidate IS NULL
            AND selected_candidate_sha256 IS NULL
            AND btrim(clarification_question) <> '')
    )
);
CREATE INDEX IF NOT EXISTS locality_resolutions_lookup_idx
    ON locality_resolutions(case_id, intake_version, lookup_sha256, version DESC);

-- A resolution is evidence, not a mutable projection. A new interpretation is
-- represented by a new version; deleting a whole case may still cascade for
-- retention/privacy purposes.
CREATE OR REPLACE FUNCTION reject_locality_resolution_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'locality_resolutions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS locality_resolutions_immutable_update ON locality_resolutions;
CREATE TRIGGER locality_resolutions_immutable_update
BEFORE UPDATE ON locality_resolutions
FOR EACH ROW EXECUTE FUNCTION reject_locality_resolution_update();
