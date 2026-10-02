-- Bind each post-migration observation to the product/request variant in force
-- when its quote projection is refreshed. Historical observations remain
-- unbound and cannot silently carry terms across a variant change.
CREATE UNIQUE INDEX IF NOT EXISTS offer_observations_id_offer_idx
    ON offer_observations(id,offer_id);

CREATE TABLE IF NOT EXISTS offer_observation_variants (
    observation_id UUID PRIMARY KEY,
    offer_id UUID NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    variant_fingerprint CHAR(64) NOT NULL CHECK (variant_fingerprint ~ '^[0-9a-f]{64}$'),
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (observation_id, offer_id, request_item_id),
    FOREIGN KEY (observation_id,offer_id)
        REFERENCES offer_observations(id,offer_id) ON DELETE CASCADE,
    FOREIGN KEY (offer_id,request_item_id)
        REFERENCES offers(id,request_item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS offer_observation_variants_scope_idx
    ON offer_observation_variants(offer_id, request_item_id, variant_fingerprint);

CREATE TABLE IF NOT EXISTS quote_snapshot_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id UUID NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    variant_fingerprint CHAR(64) NOT NULL CHECK (variant_fingerprint ~ '^[0-9a-f]{64}$'),
    snapshot_hash CHAR(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (offer_id, version),
    UNIQUE (offer_id, snapshot_hash),
    UNIQUE (id, offer_id, request_item_id),
    FOREIGN KEY (offer_id,request_item_id)
        REFERENCES offers(id,request_item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS quote_snapshot_versions_item_idx
    ON quote_snapshot_versions(request_item_id, created_at DESC);

-- Mutable pointer is deliberately separate from immutable snapshot versions.
CREATE TABLE IF NOT EXISTS quote_snapshot_current (
    offer_id UUID PRIMARY KEY REFERENCES offers(id) ON DELETE CASCADE,
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    snapshot_id UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (snapshot_id, offer_id, request_item_id)
        REFERENCES quote_snapshot_versions(id, offer_id, request_item_id) ON DELETE CASCADE
);
