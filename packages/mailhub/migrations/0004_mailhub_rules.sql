-- Declarative, versioned low-risk rule proposals.
-- Apply after 0003_mailhub_sync_webhook.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS mail_rules (
    rule_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_subject_id TEXT NOT NULL,
    name TEXT NOT NULL,
    conditions JSONB NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('label', 'archive', 'mark_read')),
    action_params JSONB NOT NULL,
    automation_level TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    max_per_hour INTEGER NOT NULL DEFAULT 0 CHECK (max_per_hour >= 0),
    max_per_day INTEGER NOT NULL DEFAULT 0 CHECK (max_per_day >= 0),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, rule_id)
);

CREATE INDEX IF NOT EXISTS mail_rules_scope_idx
    ON mail_rules (tenant_id, owner_subject_id, updated_at DESC);

ALTER TABLE mail_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_rules FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mailhub_tenant_isolation ON mail_rules;
CREATE POLICY mailhub_tenant_isolation ON mail_rules
    USING (tenant_id = current_setting('mailhub.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('mailhub.tenant_id', true));

COMMIT;
