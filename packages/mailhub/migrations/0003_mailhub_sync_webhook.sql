-- Durable sync jobs and append-only provider webhook receipts.
-- Apply after 0002_mailhub_safety_lifecycle.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS mail_sync_jobs (
    job_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    connection_id UUID NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('incremental', 'backfill', 'reconcile')),
    requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 500),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    fetched_count INTEGER NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    saved_count INTEGER NOT NULL DEFAULT 0 CHECK (saved_count >= 0),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    cursor_before TEXT,
    cursor_after TEXT,
    provider_request_id TEXT,
    error_code TEXT,
    trace_id TEXT,
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (connection_id, idempotency_key),
    FOREIGN KEY (connection_id, tenant_id)
        REFERENCES mail_connections (connection_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS mail_sync_jobs_scope_idx
    ON mail_sync_jobs (tenant_id, subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mail_webhook_receipts (
    receipt_id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    body_sha256 CHAR(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    received_at TIMESTAMPTZ NOT NULL,
    verified BOOLEAN NOT NULL,
    duplicate BOOLEAN NOT NULL,
    status TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    UNIQUE (provider, tenant_id, event_id)
);

CREATE INDEX IF NOT EXISTS mail_webhook_receipts_scope_idx
    ON mail_webhook_receipts (tenant_id, received_at DESC);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['mail_sync_jobs', 'mail_webhook_receipts'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS mailhub_tenant_isolation ON %I', table_name);
        EXECUTE format(
            'CREATE POLICY mailhub_tenant_isolation ON %I USING (tenant_id = current_setting(''mailhub.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''mailhub.tenant_id'', true))',
            table_name
        );
    END LOOP;
END $$;

COMMIT;
