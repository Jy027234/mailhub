-- Durable evidence for bounded L3A rule decisions and idempotent retries.
-- Apply after 0005_mailhub_draft_recipients.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS mail_rule_executions (
    execution_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    rule_id UUID NOT NULL,
    rule_version BIGINT NOT NULL CHECK (rule_version > 0),
    message_id UUID NOT NULL,
    action_id UUID NOT NULL,
    input_digest CHAR(64) NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('dry_run', 'proposed', 'executed', 'skipped', 'blocked', 'failed', 'outcome_unknown')),
    reason TEXT NOT NULL,
    result JSONB NOT NULL DEFAULT '{}',
    revision BIGINT NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, execution_id)
);

CREATE INDEX IF NOT EXISTS mail_rule_executions_scope_idx
    ON mail_rule_executions (tenant_id, subject_id, rule_id, updated_at DESC);

ALTER TABLE mail_rule_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_rule_executions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mailhub_tenant_isolation ON mail_rule_executions;
CREATE POLICY mailhub_tenant_isolation ON mail_rule_executions
    USING (tenant_id = current_setting('mailhub.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('mailhub.tenant_id', true));

COMMIT;
