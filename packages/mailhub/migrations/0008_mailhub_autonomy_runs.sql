-- Durable, proposal-only Agent recommendation run state.
-- Apply after 0007_mailhub_sender_leases.sql.  The table stores only IDs,
-- counters and evidence references; credentials and raw message content stay
-- in their existing governed stores.

CREATE TABLE mail_autonomy_runs (
    run_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    connection_id UUID NOT NULL,
    replay_key TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'recommend_only'
        CHECK (mode = 'recommend_only'),
    requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 500),
    message_limit INTEGER NOT NULL CHECK (message_limit BETWEEN 1 AND 200),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')),
    message_ids UUID[] NOT NULL DEFAULT '{}',
    analyzed_message_ids UUID[] NOT NULL DEFAULT '{}',
    candidate_ids UUID[] NOT NULL DEFAULT '{}',
    sync_result JSONB NOT NULL DEFAULT '{}',
    sync_job_id UUID,
    error_code TEXT,
    pause_reason TEXT,
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, subject_id, connection_id, replay_key),
    FOREIGN KEY (tenant_id, connection_id)
        REFERENCES mail_connections(tenant_id, connection_id),
    FOREIGN KEY (sync_job_id) REFERENCES mail_sync_jobs(job_id)
);

CREATE INDEX mail_autonomy_runs_scope_created_idx
    ON mail_autonomy_runs (tenant_id, subject_id, created_at DESC);
CREATE INDEX mail_autonomy_runs_queue_idx
    ON mail_autonomy_runs (status, updated_at);

ALTER TABLE mail_autonomy_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_autonomy_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mailhub_tenant_isolation ON mail_autonomy_runs;
CREATE POLICY mailhub_tenant_isolation ON mail_autonomy_runs
    USING (tenant_id = current_setting('mailhub.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('mailhub.tenant_id', true));
