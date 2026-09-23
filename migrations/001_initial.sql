CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    language_code TEXT NOT NULL DEFAULT 'ru',
    timezone TEXT NOT NULL DEFAULT 'Asia/Almaty',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (telegram_id IS NOT NULL),
    CHECK (char_length(language_code) BETWEEN 2 AND 16)
);

-- Every inbound/outbound transport message is represented here.  Procurement
-- state refers to these immutable facts instead of to a "latest message".
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('telegram','whatsapp')),
    direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
    external_chat_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    message_kind TEXT NOT NULL CHECK (
        message_kind IN ('text','voice','audio','document','photo','location','contact','callback','system')
    ),
    text_content TEXT NOT NULL DEFAULT '',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_payload)='object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel, external_chat_id, external_message_id)
);
CREATE INDEX IF NOT EXISTS messages_user_received_idx ON messages(user_id, received_at DESC);
CREATE INDEX IF NOT EXISTS messages_channel_chat_idx ON messages(channel, external_chat_id, received_at DESC);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','completed','failed')),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_payload)='object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error_code TEXT NOT NULL DEFAULT '',
    CHECK ((status='completed' AND completed_at IS NOT NULL) OR status<>'completed')
);

CREATE TABLE IF NOT EXISTS incoming_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    external_file_id TEXT NOT NULL,
    external_file_unique_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('voice','audio','document','photo')),
    original_filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    declared_size BIGINT CHECK (declared_size IS NULL OR declared_size >= 0),
    declared_duration_seconds INTEGER CHECK (
        declared_duration_seconds IS NULL OR declared_duration_seconds >= 0
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','downloading','downloaded','failed')
    ),
    storage_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, external_file_id)
);
CREATE INDEX IF NOT EXISTS incoming_attachments_status_idx
    ON incoming_attachments(status, created_at);

-- Original and derived files are durable artifacts.  A worker puts only an
-- artifact/attachment UUID in a job payload, never a large document body.
CREATE TABLE IF NOT EXISTS source_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attachment_id UUID NOT NULL REFERENCES incoming_attachments(id) ON DELETE CASCADE,
    artifact_kind TEXT NOT NULL CHECK (
        artifact_kind IN ('original','normalized_audio','rendered_page','thumbnail','extraction_json')
    ),
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    mime_type TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attachment_id, artifact_kind, sha256)
);

CREATE TABLE IF NOT EXISTS content_extractions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attachment_id UUID NOT NULL REFERENCES incoming_attachments(id) ON DELETE CASCADE,
    extraction_kind TEXT NOT NULL CHECK (
        extraction_kind IN ('document_text','ocr_text','transcript','structured_request')
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    engine TEXT NOT NULL,
    engine_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running','succeeded','failed')
    ),
    content_text TEXT NOT NULL DEFAULT '',
    content_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(content_json)='object'),
    content_sha256 TEXT CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    language_code TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (attachment_id, extraction_kind, version),
    CHECK ((status='succeeded' AND completed_at IS NOT NULL) OR status<>'succeeded')
);
CREATE INDEX IF NOT EXISTS content_extractions_attachment_idx
    ON content_extractions(attachment_id, extraction_kind, version DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind <> ''),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload)='object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','running','retry','succeeded','dead')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 50),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL UNIQUE,
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (
        (status='running' AND locked_at IS NOT NULL AND locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR status<>'running'
    )
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status, available_at, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL CHECK (event_type <> ''),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload)='object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','sending','retry','sent','dead')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 50),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL UNIQUE,
    external_id TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    CHECK (
        (status='sending' AND locked_at IS NOT NULL AND locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR status<>'sending'
    )
);
CREATE INDEX IF NOT EXISTS outbox_claim_idx
    ON outbox_events(status, available_at, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS procurement_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    source_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    search_area_text TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL DEFAULT '',
    deadline_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft','needs_clarification','ready','researching','contacting','evaluating','report_ready','selected','closed','cancelled')
    ),
    source_kind TEXT NOT NULL DEFAULT 'telegram' CHECK (
        source_kind IN ('telegram','notion_import','manual','api')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS procurement_cases_owner_status_idx
    ON procurement_cases(owner_user_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS request_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    name TEXT NOT NULL,
    specification_text TEXT NOT NULL DEFAULT '',
    quantity NUMERIC(18,6) CHECK (quantity IS NULL OR quantity > 0),
    unit TEXT NOT NULL DEFAULT '',
    analogs_allowed BOOLEAN,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft','needs_clarification','ready','researching','offers_found','no_match','selected','closed')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, line_number)
);
CREATE INDEX IF NOT EXISTS request_items_case_idx ON request_items(case_id, line_number);

-- Parser output is retained before it is projected into request_items.  A
-- partial unique index makes the applied version the unambiguous current one.
CREATE TABLE IF NOT EXISTS case_parse_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    source_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    source_extraction_id UUID REFERENCES content_extractions(id) ON DELETE SET NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '',
    normalized_payload JSONB NOT NULL CHECK (jsonb_typeof(normalized_payload)='object'),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft','applied','superseded','rejected')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ,
    UNIQUE (case_id, version),
    CHECK ((status='applied' AND applied_at IS NOT NULL) OR status<>'applied')
);
CREATE UNIQUE INDEX IF NOT EXISTS case_parse_versions_one_applied_idx
    ON case_parse_versions(case_id) WHERE status='applied';
CREATE UNIQUE INDEX IF NOT EXISTS case_parse_versions_source_message_idx
    ON case_parse_versions(source_message_id) WHERE source_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS item_requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_item_id UUID NOT NULL REFERENCES request_items(id) ON DELETE CASCADE,
    requirement_key TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT 'equals' CHECK (
        operator IN ('equals','contains','minimum','maximum','range','one_of','present')
    ),
    expected_value JSONB NOT NULL,
    is_hard BOOLEAN NOT NULL DEFAULT TRUE,
    source_pointer TEXT NOT NULL DEFAULT '',
    confidence NUMERIC(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_item_id, requirement_key, source_pointer),
    CHECK (jsonb_typeof(expected_value) <> 'null')
);

CREATE TABLE IF NOT EXISTS clarifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    request_item_id UUID REFERENCES request_items(id) ON DELETE CASCADE,
    asked_in_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    answered_in_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    topic TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','dismissed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ,
    CHECK ((status='answered' AND answered_at IS NOT NULL) OR status<>'answered')
);
CREATE INDEX IF NOT EXISTS clarifications_open_idx
    ON clarifications(case_id, status, created_at);

-- Append-only domain timeline. Current status columns are projections; this is
-- the forensic record used to explain who/what moved a workflow forward.
CREATE TABLE IF NOT EXISTS workflow_events (
    id BIGSERIAL PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE CASCADE,
    request_item_id UUID REFERENCES request_items(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user','agent','worker','system')),
    actor_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    data JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(data)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflow_events_case_idx ON workflow_events(case_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_kind TEXT NOT NULL DEFAULT 'system' CHECK (
        actor_kind IN ('user','agent','worker','system')
    ),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_entity_idx
    ON audit_log(entity_type, entity_id, created_at DESC);
