-- Customers, evidence-backed purchasing, fulfilment and shared delivery costs.
-- Private tender values are isolated from supplier-search projections.

CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL CHECK (btrim(display_name) <> ''),
    bin_iin TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, display_name)
);

ALTER TABLE procurement_cases
    ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id) ON DELETE RESTRICT;

CREATE TABLE IF NOT EXISTS case_financials (
    case_id UUID PRIMARY KEY REFERENCES procurement_cases(id) ON DELETE CASCADE,
    tender_revenue_private NUMERIC(16,2) CHECK (
        tender_revenue_private IS NULL OR tender_revenue_private >= 0
    ),
    currency CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchase_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    source_artifact_id UUID NOT NULL REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN ('receipt','invoice','waybill','cash_receipt','other')
    ),
    document_number TEXT NOT NULL DEFAULT '',
    seller_name TEXT NOT NULL DEFAULT '',
    issued_at TIMESTAMPTZ,
    stated_total NUMERIC(16,2) CHECK (stated_total IS NULL OR stated_total >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, source_artifact_id),
    UNIQUE (id, case_id)
);

CREATE TABLE IF NOT EXISTS purchase_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL,
    purchase_document_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    offer_id UUID REFERENCES offers(id) ON DELETE RESTRICT,
    purchased_quantity NUMERIC(18,6) NOT NULL CHECK (purchased_quantity > 0),
    unit_cost NUMERIC(16,4) CHECK (unit_cost IS NULL OR unit_cost >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (purchase_document_id, request_item_id),
    FOREIGN KEY (purchase_document_id, case_id)
        REFERENCES purchase_documents(id, case_id) ON DELETE RESTRICT,
    FOREIGN KEY (request_item_id, case_id)
        REFERENCES request_items(id, case_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS purchase_items_request_item_idx
    ON purchase_items(request_item_id, created_at);

CREATE TABLE IF NOT EXISTS customer_acceptance_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL CHECK (event_type IN ('accepted','revoked')),
    accepted_quantity NUMERIC(18,6) NOT NULL CHECK (accepted_quantity > 0),
    source_artifact_id UUID REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (request_item_id, case_id)
        REFERENCES request_items(id, case_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS customer_acceptance_case_customer_idx
    ON customer_acceptance_events(case_id, customer_id, request_item_id, occurred_at);

CREATE TABLE IF NOT EXISTS delivery_carriers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    provider_kind TEXT NOT NULL CHECK (
        provider_kind IN ('yandex','indriver','private','olx','company','other')
    ),
    company_name TEXT NOT NULL DEFAULT '',
    driver_name TEXT NOT NULL DEFAULT '',
    normalized_phone TEXT NOT NULL CHECK (normalized_phone <> ''),
    source_url TEXT,
    notes TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, normalized_phone)
);

CREATE TABLE IF NOT EXISTS delivery_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    carrier_id UUID NOT NULL REFERENCES delivery_carriers(id) ON DELETE RESTRICT,
    total_cost NUMERIC(16,2) NOT NULL CHECK (total_cost >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    allocation_method TEXT NOT NULL DEFAULT 'equal_per_item' CHECK (
        allocation_method IN ('equal_per_item','quantity_weighted','manual')
    ),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, case_id)
);

CREATE TABLE IF NOT EXISTS delivery_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL,
    delivery_run_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    allocated_amount NUMERIC(16,2) NOT NULL CHECK (allocated_amount >= 0),
    allocation_weight NUMERIC(18,6) NOT NULL CHECK (allocation_weight > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (delivery_run_id, request_item_id),
    FOREIGN KEY (delivery_run_id, case_id)
        REFERENCES delivery_runs(id, case_id) ON DELETE RESTRICT,
    FOREIGN KEY (request_item_id, case_id)
        REFERENCES request_items(id, case_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS delivery_allocations_item_idx
    ON delivery_allocations(request_item_id, delivery_run_id);
