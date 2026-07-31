-- Durable quota leases for tenant/subject/provider-account admission.
-- No provider credentials or message content are stored here.
-- The PostgresQuotaPort serializes acquisition with pg_advisory_xact_lock;
-- keeping that lock in the transaction makes all dimensions atomic.

CREATE TABLE mail_quota_leases (
    lease_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    account_id UUID,
    operation TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at > acquired_at),
    CHECK (consumed_at IS NULL OR released_at IS NOT NULL)
);

CREATE INDEX mail_quota_active_idx
    ON mail_quota_leases (tenant_id, account_id, expires_at)
    WHERE released_at IS NULL;

CREATE INDEX mail_quota_history_idx
    ON mail_quota_leases (tenant_id, subject_id, consumed_at)
    WHERE consumed_at IS NOT NULL;

ALTER TABLE mail_quota_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_quota_leases FORCE ROW LEVEL SECURITY;
CREATE POLICY mailhub_tenant_isolation ON mail_quota_leases
    USING (tenant_id = current_setting('mailhub.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('mailhub.tenant_id', true));
