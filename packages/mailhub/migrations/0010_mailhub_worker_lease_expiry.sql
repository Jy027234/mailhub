BEGIN;

-- Sync and proposal-only autonomy workers must not remain RUNNING forever
-- after a process crash.  Expired rows are re-queued by the next fenced
-- claimant; the durable audit entry records why the retry occurred.
ALTER TABLE mail_sync_jobs
    ADD COLUMN lease_expires_at TIMESTAMPTZ;

ALTER TABLE mail_autonomy_runs
    ADD COLUMN lease_expires_at TIMESTAMPTZ;

CREATE INDEX mail_sync_jobs_lease_expiry_idx
    ON mail_sync_jobs (status, lease_expires_at)
    WHERE status = 'running';

CREATE INDEX mail_autonomy_runs_lease_expiry_idx
    ON mail_autonomy_runs (status, lease_expires_at)
    WHERE status = 'running';

COMMIT;
