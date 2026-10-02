-- Cross-item cache of validated directory-page descriptors only. Card facts,
-- supplier evidence, offers, prices and availability stay query-scoped.
CREATE TABLE IF NOT EXISTS supplier_directory_page_pool (
    cache_key CHAR(64) PRIMARY KEY CHECK (cache_key ~ '^[a-f0-9]{64}$'),
    source TEXT NOT NULL CHECK (source='2gis'),
    category_query TEXT NOT NULL CHECK (category_query <> ''),
    locality JSONB NOT NULL CHECK (jsonb_typeof(locality)='object'),
    search_cursor TEXT NOT NULL DEFAULT '',
    result_ttl_hours SMALLINT NOT NULL CHECK (result_ttl_hours BETWEEN 1 AND 2160),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at <= created_at + result_ttl_hours * interval '1 hour')
);
CREATE INDEX IF NOT EXISTS supplier_directory_page_pool_expiry_idx
    ON supplier_directory_page_pool(expires_at);
