-- Durable single-sender interlock for migration cutovers.
-- Apply after 0006_mailhub_rule_executions.sql.
-- A lease is scoped by tenant, account and purpose; legacy and MailHub
-- senders must use the same row/authority.  Body, token and provider data are
-- intentionally absent.

CREATE TABLE mail_sender_leases (
    tenant_id TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'email',
    owner TEXT NOT NULL,
    fencing_token BIGINT NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
    lease_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, account_ref, purpose)
);

CREATE INDEX mail_sender_leases_expiry_idx
    ON mail_sender_leases (lease_until);

ALTER TABLE mail_sender_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_sender_leases FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mailhub_tenant_isolation ON mail_sender_leases;
CREATE POLICY mailhub_tenant_isolation ON mail_sender_leases
    USING (tenant_id = current_setting('mailhub.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('mailhub.tenant_id', true));
