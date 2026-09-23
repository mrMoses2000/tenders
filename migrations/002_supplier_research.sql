-- Milestone 2: durable research provenance, supplier identity resolution and
-- time-versioned offers.  No table in this migration authorises communication
-- with a supplier; outbound actions continue to require action_approvals.

CREATE TABLE IF NOT EXISTS research_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned','running','succeeded','partial','failed','cancelled')
    ),
    requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
    search_scope JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(search_scope)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (completed_at IS NULL OR started_at IS NOT NULL),
    CHECK (completed_at IS NULL OR completed_at >= started_at)
);
CREATE INDEX IF NOT EXISTS research_runs_case_idx
    ON research_runs(case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS research_runs_status_idx
    ON research_runs(status, created_at);

CREATE TABLE IF NOT EXISTS research_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (
        provider IN (
            'own_database','2gis_api','2gis_web','web','supplier_site','marketplace','manual'
        )
    ),
    query_text TEXT NOT NULL CHECK (query_text <> ''),
    city TEXT NOT NULL DEFAULT '',
    search_area_text TEXT NOT NULL DEFAULT '',
    latitude NUMERIC(9,6) CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    longitude NUMERIC(9,6) CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    radius_meters INTEGER CHECK (radius_meters IS NULL OR radius_meters > 0),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(parameters)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS research_queries_run_idx
    ON research_queries(research_run_id, created_at);

-- An evidence row is an immutable capture, not a mutable cache entry.  Its
-- payload digest and caller-supplied logical key make retries exactly-once.
CREATE TABLE IF NOT EXISTS source_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_query_id UUID REFERENCES research_queries(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL CHECK (
        source_type IN ('2gis','web','supplier_site','marketplace','supplier_message','manual','import')
    ),
    source_locator TEXT NOT NULL CHECK (source_locator <> ''),
    source_url TEXT,
    external_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    captured_at TIMESTAMPTZ NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot_storage_path TEXT NOT NULL DEFAULT '',
    raw_payload JSONB NOT NULL CHECK (jsonb_typeof(raw_payload)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_evidence_query_idx
    ON source_evidence(research_query_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS source_evidence_locator_idx
    ON source_evidence(source_type, source_locator, captured_at DESC);

CREATE TABLE IF NOT EXISTS suppliers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL CHECK (display_name <> ''),
    supplier_type TEXT NOT NULL DEFAULT 'business' CHECK (
        supplier_type IN ('business','sole_proprietor','individual','market_stall','unknown')
    ),
    country_code TEXT NOT NULL DEFAULT 'KZ' CHECK (country_code ~ '^[A-Z]{2}$'),
    bin_iin TEXT CHECK (bin_iin IS NULL OR bin_iin ~ '^[0-9]{12}$'),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate','active','inactive','blocked','merged')
    ),
    reliability_level TEXT NOT NULL DEFAULT 'unknown' CHECK (
        reliability_level IN ('unknown','low','medium','high')
    ),
    do_not_contact_reason TEXT NOT NULL DEFAULT '',
    merged_into_id UUID REFERENCES suppliers(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status='merged' AND merged_into_id IS NOT NULL) OR status<>'merged'),
    CHECK (merged_into_id IS NULL OR merged_into_id <> id)
);
CREATE INDEX IF NOT EXISTS suppliers_name_idx ON suppliers(lower(display_name));
CREATE INDEX IF NOT EXISTS suppliers_active_idx ON suppliers(status, updated_at DESC);

-- Aliases hold both weak names and strong external identities.  A normalized
-- 2GIS id/domain/BIN can point to only one active supplier, while ordinary
-- display-name aliases are intentionally allowed to collide.
CREATE TABLE IF NOT EXISTS supplier_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    alias_type TEXT NOT NULL CHECK (
        alias_type IN ('name','2gis_firm_id','domain','bin_iin','instagram','source_record')
    ),
    display_value TEXT NOT NULL CHECK (display_value <> ''),
    normalized_value TEXT NOT NULL CHECK (normalized_value <> ''),
    source_evidence_id UUID REFERENCES source_evidence(id) ON DELETE SET NULL,
    confidence NUMERIC(4,3) NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (supplier_id, alias_type, normalized_value),
    CHECK (last_seen_at >= first_seen_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_aliases_strong_identity_idx
    ON supplier_aliases(alias_type, normalized_value)
    WHERE active AND alias_type IN ('2gis_firm_id','domain','bin_iin','instagram','source_record');
CREATE INDEX IF NOT EXISTS supplier_aliases_lookup_idx
    ON supplier_aliases(alias_type, normalized_value) WHERE active;

CREATE TABLE IF NOT EXISTS supplier_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    contact_type TEXT NOT NULL CHECK (
        contact_type IN ('phone','whatsapp','email','website','instagram','telegram')
    ),
    display_value TEXT NOT NULL CHECK (display_value <> ''),
    normalized_value TEXT NOT NULL CHECK (normalized_value <> ''),
    label TEXT NOT NULL DEFAULT '',
    is_whatsapp_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    source_evidence_id UUID NOT NULL REFERENCES source_evidence(id) ON DELETE RESTRICT,
    confidence NUMERIC(4,3) NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (supplier_id, contact_type, normalized_value),
    CHECK (last_seen_at >= first_seen_at),
    CHECK (NOT is_whatsapp_confirmed OR contact_type IN ('phone','whatsapp'))
);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_contacts_active_identity_idx
    ON supplier_contacts(
        (CASE WHEN contact_type IN ('phone','whatsapp') THEN 'phone' ELSE contact_type END),
        normalized_value
    ) WHERE active;
CREATE INDEX IF NOT EXISTS supplier_contacts_supplier_idx
    ON supplier_contacts(supplier_id, active, contact_type);

CREATE TABLE IF NOT EXISTS supplier_locations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    location_key TEXT NOT NULL CHECK (location_key ~ '^[0-9a-f]{64}$'),
    country_code TEXT NOT NULL DEFAULT 'KZ' CHECK (country_code ~ '^[A-Z]{2}$'),
    city TEXT NOT NULL DEFAULT '',
    address_text TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    market_name TEXT NOT NULL DEFAULT '',
    pavilion TEXT NOT NULL DEFAULT '',
    latitude NUMERIC(9,6) CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    longitude NUMERIC(9,6) CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    source_provider TEXT NOT NULL DEFAULT '',
    provider_place_id TEXT NOT NULL DEFAULT '',
    source_evidence_id UUID NOT NULL REFERENCES source_evidence(id) ON DELETE RESTRICT,
    confidence NUMERIC(4,3) NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (supplier_id, location_key),
    CHECK ((latitude IS NULL) = (longitude IS NULL)),
    CHECK (last_seen_at >= first_seen_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_locations_provider_place_idx
    ON supplier_locations(source_provider, provider_place_id)
    WHERE active AND source_provider <> '' AND provider_place_id <> '';
CREATE INDEX IF NOT EXISTS supplier_locations_geo_idx
    ON supplier_locations(country_code, city, market_name) WHERE active;

CREATE TABLE IF NOT EXISTS offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    supplier_id UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    supplier_location_id UUID REFERENCES supplier_locations(id) ON DELETE SET NULL,
    supplier_product_name TEXT NOT NULL DEFAULT '',
    model_sku TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'lead' CHECK (
        status IN ('lead','confirm','exact','mismatch','not_found','withdrawn','selected')
    ),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS offers_item_status_idx
    ON offers(request_item_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS offers_supplier_idx
    ON offers(supplier_id, updated_at DESC);

-- Observations are append-only facts. Unknown price, quantity and logistics
-- remain NULL; a lead from 2GIS must not masquerade as a priced exact offer.
CREATE TABLE IF NOT EXISTS offer_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    source_evidence_id UUID NOT NULL REFERENCES source_evidence(id) ON DELETE RESTRICT,
    observed_at TIMESTAMPTZ NOT NULL,
    observed_by TEXT NOT NULL CHECK (observed_by <> ''),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('2gis','web','supplier_site','marketplace','supplier_message','manual','import')
    ),
    external_message_id TEXT NOT NULL DEFAULT '',
    price_amount NUMERIC(14,2) CHECK (price_amount IS NULL OR price_amount >= 0),
    currency CHAR(3) CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    price_unit TEXT,
    pack_qty NUMERIC(18,6) CHECK (pack_qty IS NULL OR pack_qty > 0),
    moq NUMERIC(18,6) CHECK (moq IS NULL OR moq > 0),
    order_multiple NUMERIC(18,6) CHECK (order_multiple IS NULL OR order_multiple > 0),
    available_qty NUMERIC(18,6) CHECK (available_qty IS NULL OR available_qty >= 0),
    availability_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        availability_status IN ('unknown','in_stock_unquantified','in_stock','preorder','out_of_stock')
    ),
    vat_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        vat_status IN ('unknown','included','excluded','not_applicable')
    ),
    document_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        document_status IN ('unknown','available','partial','unavailable')
    ),
    delivery_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        delivery_status IN ('unknown','pickup','delivery','both','unavailable')
    ),
    pickup_point TEXT,
    availability_date DATE,
    evidence_level SMALLINT NOT NULL DEFAULT 1 CHECK (evidence_level BETWEEN 1 AND 5),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_payload)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (price_amount IS NULL OR currency IS NOT NULL),
    CHECK (availability_status <> 'in_stock' OR available_qty IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS offer_observations_offer_idx
    ON offer_observations(offer_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS offer_observations_freshness_idx
    ON offer_observations(source_type, observed_at DESC);
