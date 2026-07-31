-- MailHub core schema, PostgreSQL 16+
-- Secrets and raw MIME/body bytes are deliberately absent.  Objects are held by
-- ObjectStorePort and referenced by body_object_ref/attachment refs.

CREATE TABLE mail_connections (
    connection_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('sandbox', 'gmail', 'microsoft_graph', 'imap_smtp')),
    email_address TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    granted_scopes TEXT[] NOT NULL DEFAULT '{}',
    content_mode TEXT NOT NULL CHECK (content_mode IN ('metadata_only', 'bounded_processing', 'encrypted_cache', 'archive')),
    status TEXT NOT NULL CHECK (status IN ('pending_authorization', 'active', 'degraded', 'reauthorization_required', 'revoked', 'deleting', 'deleted')),
    revision BIGINT NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, connection_id),
    UNIQUE (tenant_id, subject_id, email_address)
);

CREATE TABLE mail_threads (
    thread_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    connection_id UUID NOT NULL REFERENCES mail_connections(connection_id),
    provider_thread_ref TEXT NOT NULL,
    normalized_subject TEXT NOT NULL,
    participant_addresses TEXT[] NOT NULL,
    latest_at TIMESTAMPTZ NOT NULL,
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    revision BIGINT NOT NULL CHECK (revision > 0),
    UNIQUE (tenant_id, thread_id),
    UNIQUE (connection_id, provider_thread_ref),
    FOREIGN KEY (tenant_id, connection_id)
        REFERENCES mail_connections(tenant_id, connection_id)
);

CREATE TABLE mail_messages (
    message_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    connection_id UUID NOT NULL REFERENCES mail_connections(connection_id),
    thread_id UUID NOT NULL REFERENCES mail_threads(thread_id),
    provider_message_ref TEXT NOT NULL,
    internet_message_id TEXT,
    sender_address TEXT NOT NULL,
    recipient_addresses TEXT[] NOT NULL,
    subject TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    body_object_ref TEXT,
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    labels TEXT[] NOT NULL DEFAULT '{}',
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    revision BIGINT NOT NULL CHECK (revision > 0),
    UNIQUE (tenant_id, message_id),
    UNIQUE (connection_id, provider_message_ref),
    FOREIGN KEY (tenant_id, connection_id)
        REFERENCES mail_connections(tenant_id, connection_id),
    FOREIGN KEY (tenant_id, thread_id)
        REFERENCES mail_threads(tenant_id, thread_id)
);

CREATE TABLE mail_sync_cursors (
    connection_id UUID NOT NULL REFERENCES mail_connections(connection_id),
    tenant_id TEXT NOT NULL,
    folder_ref TEXT NOT NULL,
    cursor_kind TEXT NOT NULL,
    cursor_value TEXT,
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    status TEXT NOT NULL CHECK (status IN ('idle', 'syncing', 'retrying', 'blocked')),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (connection_id, folder_ref),
    FOREIGN KEY (tenant_id, connection_id)
        REFERENCES mail_connections(tenant_id, connection_id)
);

CREATE TABLE mail_agent_policies (
    policy_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_subject_id TEXT NOT NULL,
    allowed_connection_ids UUID[] NOT NULL DEFAULT '{}',
    allowed_folder_refs TEXT[] NOT NULL DEFAULT '{}',
    allowed_actions TEXT[] NOT NULL DEFAULT '{}',
    allowed_domains TEXT[] NOT NULL DEFAULT '{}',
    allowed_data_classes TEXT[] NOT NULL DEFAULT '{public,internal}',
    thread_only BOOLEAN NOT NULL DEFAULT TRUE,
    allowed_automation_level TEXT NOT NULL,
    max_per_hour INTEGER NOT NULL DEFAULT 0 CHECK (max_per_hour >= 0),
    max_per_day INTEGER NOT NULL DEFAULT 0 CHECK (max_per_day >= 0),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (valid_until > valid_from),
    UNIQUE (tenant_id, policy_id)
);

CREATE TABLE mail_delegation_grants (
    grant_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    policy_id UUID NOT NULL REFERENCES mail_agent_policies(policy_id),
    agent_subject_id TEXT NOT NULL,
    granted_by_subject_id TEXT NOT NULL,
    capability_ids TEXT[] NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revision BIGINT NOT NULL CHECK (revision > 0),
    CHECK (expires_at > granted_at),
    FOREIGN KEY (tenant_id, policy_id)
        REFERENCES mail_agent_policies(tenant_id, policy_id)
);

CREATE TABLE mail_drafts (
    draft_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    connection_id UUID NOT NULL REFERENCES mail_connections(connection_id),
    thread_id UUID REFERENCES mail_threads(thread_id),
    recipient_addresses TEXT[] NOT NULL,
    subject TEXT NOT NULL,
    body_object_ref TEXT,
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    revision BIGINT NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL,
    provider_draft_ref TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, draft_id),
    FOREIGN KEY (tenant_id, connection_id)
        REFERENCES mail_connections(tenant_id, connection_id),
    FOREIGN KEY (tenant_id, thread_id)
        REFERENCES mail_threads(tenant_id, thread_id)
);

CREATE TABLE mail_outbox_operations (
    operation_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    draft_id UUID NOT NULL REFERENCES mail_drafts(draft_id),
    connection_id UUID NOT NULL REFERENCES mail_connections(connection_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    action_id UUID,
    input_digest CHAR(64),
    agent_subject_id TEXT,
    policy_id UUID,
    grant_id UUID,
    policy_revision BIGINT,
    grant_revision BIGINT,
    approval_ref TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    provider_message_ref TEXT,
    provider_request_id TEXT,
    error_code TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, operation_id),
    UNIQUE (connection_id, idempotency_key),
    FOREIGN KEY (tenant_id, draft_id)
        REFERENCES mail_drafts(tenant_id, draft_id),
    FOREIGN KEY (tenant_id, connection_id)
        REFERENCES mail_connections(tenant_id, connection_id)
);

CREATE TABLE mail_audit_events (
    audit_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE mail_action_candidates (
    candidate_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    message_id UUID NOT NULL REFERENCES mail_messages(message_id),
    candidate_type TEXT NOT NULL CHECK (candidate_type IN ('project', 'task', 'knowledge')),
    payload JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]',
    confidence NUMERIC(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    requires_review BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'applied', 'revoked', 'failed')),
    revision BIGINT NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, message_id, candidate_type, revision),
    FOREIGN KEY (tenant_id, message_id)
        REFERENCES mail_messages(tenant_id, message_id)
);

CREATE INDEX mail_messages_tenant_received_idx ON mail_messages (tenant_id, received_at DESC);
CREATE INDEX mail_threads_tenant_latest_idx ON mail_threads (tenant_id, latest_at DESC);
CREATE INDEX mail_outbox_due_idx ON mail_outbox_operations (status, next_attempt_at, updated_at);
CREATE INDEX mail_audit_tenant_time_idx ON mail_audit_events (tenant_id, occurred_at DESC);
CREATE INDEX mail_candidates_scope_status_idx ON mail_action_candidates (tenant_id, subject_id, status, created_at DESC);
