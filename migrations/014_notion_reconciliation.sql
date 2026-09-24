-- Preserve Notion identity and historical assertions without turning them into
-- verified purchase, delivery, or customer-acceptance events.
CREATE TABLE IF NOT EXISTS external_record_links (
    source_system TEXT NOT NULL CHECK (source_system <> ''),
    external_id TEXT NOT NULL CHECK (external_id <> ''),
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('customer','case','item','supplier','offer')),
    customer_id UUID REFERENCES customers(id) ON DELETE RESTRICT,
    case_id UUID REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    request_item_id UUID REFERENCES request_items(id) ON DELETE RESTRICT,
    supplier_id UUID REFERENCES suppliers(id) ON DELETE RESTRICT,
    offer_id UUID REFERENCES offers(id) ON DELETE RESTRICT,
    source_url TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, external_id),
    CHECK (num_nonnulls(customer_id,case_id,request_item_id,supplier_id,offer_id) = 1),
    CHECK (
        (entity_kind='customer' AND customer_id IS NOT NULL) OR
        (entity_kind='case' AND case_id IS NOT NULL) OR
        (entity_kind='item' AND request_item_id IS NOT NULL) OR
        (entity_kind='supplier' AND supplier_id IS NOT NULL) OR
        (entity_kind='offer' AND offer_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS external_record_links_customer_idx
    ON external_record_links(source_system, customer_id) WHERE customer_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS external_record_links_case_idx
    ON external_record_links(source_system, case_id) WHERE case_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS external_record_links_item_idx
    ON external_record_links(source_system, request_item_id) WHERE request_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS external_record_links_supplier_idx
    ON external_record_links(source_system, supplier_id) WHERE supplier_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS external_record_links_offer_idx
    ON external_record_links(source_system, offer_id) WHERE offer_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS request_item_financials (
    request_item_id UUID PRIMARY KEY REFERENCES request_items(id) ON DELETE RESTRICT,
    tender_unit_price_private NUMERIC(16,4) CHECK (
        tender_unit_price_private IS NULL OR tender_unit_price_private >= 0
    ),
    currency CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    source_system TEXT NOT NULL DEFAULT 'manual',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Each import is an assertion from an external system. NULL means unknown.
-- A later import appends a snapshot; it never rewrites the original assertion.
CREATE TABLE IF NOT EXISTS request_item_source_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE RESTRICT,
    source_system TEXT NOT NULL CHECK (source_system <> ''),
    external_id TEXT NOT NULL CHECK (external_id <> ''),
    source_hash TEXT NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    source_url TEXT NOT NULL DEFAULT '',
    captured_at TIMESTAMPTZ NOT NULL,
    reported_delivered_quantity NUMERIC(18,6) CHECK (
        reported_delivered_quantity IS NULL OR reported_delivered_quantity >= 0
    ),
    reported_accepted_quantity NUMERIC(18,6) CHECK (
        reported_accepted_quantity IS NULL OR reported_accepted_quantity >= 0
    ),
    reported_rejected_quantity NUMERIC(18,6) CHECK (
        reported_rejected_quantity IS NULL OR reported_rejected_quantity >= 0
    ),
    reported_remaining_quantity NUMERIC(18,6) CHECK (
        reported_remaining_quantity IS NULL OR reported_remaining_quantity >= 0
    ),
    movement_label TEXT NOT NULL DEFAULT '',
    reconciliation_label TEXT NOT NULL DEFAULT '',
    location_label TEXT NOT NULL DEFAULT '',
    raw_payload JSONB NOT NULL CHECK (jsonb_typeof(raw_payload)='object'),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, external_id, source_hash)
);
CREATE INDEX IF NOT EXISTS request_item_source_snapshots_item_idx
    ON request_item_source_snapshots(request_item_id, captured_at DESC, imported_at DESC);
