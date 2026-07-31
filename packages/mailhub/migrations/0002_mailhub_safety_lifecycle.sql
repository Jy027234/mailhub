-- MailHub safety/lifecycle migration, PostgreSQL 16+
-- Apply after 0001_mailhub_core.sql.  Core tenant columns and durable
-- authorization columns are part of 0001; this migration adds lifecycle
-- objects and enables the production RLS gate.  Every production connection must set
-- LOCAL mailhub.tenant_id (and, where applicable, mailhub.subject_id) before
-- reading or writing RLS-protected rows.  Application predicates remain a
-- second gate in the repository.

BEGIN;

CREATE TABLE IF NOT EXISTS mail_content_objects (
    object_ref TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('mail_message', 'mail_draft', 'attachment', 'export')),
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL DEFAULT 'text/plain',
    encryption_key_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    deletion_reason TEXT,
    retention_version BIGINT NOT NULL DEFAULT 1 CHECK (retention_version > 0)
);

CREATE INDEX IF NOT EXISTS mail_content_objects_expiry_idx
    ON mail_content_objects (tenant_id, expires_at)
    WHERE deleted_at IS NULL AND expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS mail_retention_policies (
    policy_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT,
    body_ttl_seconds BIGINT NOT NULL CHECK (body_ttl_seconds >= 0),
    attachment_ttl_seconds BIGINT NOT NULL CHECK (attachment_ttl_seconds >= 0),
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    region TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    effective_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    UNIQUE (tenant_id, subject_id, revision)
);

CREATE INDEX IF NOT EXISTS mail_retention_scope_idx
    ON mail_retention_policies (tenant_id, subject_id, effective_at DESC);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'mail_connections', 'mail_threads', 'mail_messages',
        'mail_sync_cursors', 'mail_agent_policies', 'mail_delegation_grants',
        'mail_drafts', 'mail_outbox_operations', 'mail_audit_events',
        'mail_action_candidates', 'mail_content_objects', 'mail_retention_policies'
    ] LOOP
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
