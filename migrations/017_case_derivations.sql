-- Keep pilot cases and their lines traceable to the original request.
CREATE TABLE IF NOT EXISTS case_derivations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    derived_case_id UUID NOT NULL UNIQUE REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    purpose TEXT NOT NULL CHECK (purpose <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_case_id, purpose),
    CHECK (source_case_id <> derived_case_id)
);

CREATE TABLE IF NOT EXISTS item_derivations (
    derived_item_id UUID PRIMARY KEY REFERENCES request_items(id) ON DELETE RESTRICT,
    source_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE RESTRICT,
    case_derivation_id UUID NOT NULL REFERENCES case_derivations(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_derivation_id, source_item_id),
    CHECK (derived_item_id <> source_item_id)
);
