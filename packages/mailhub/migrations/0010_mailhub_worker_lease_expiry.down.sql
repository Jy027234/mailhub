BEGIN;

DROP INDEX IF EXISTS mail_sync_jobs_lease_expiry_idx;
DROP INDEX IF EXISTS mail_autonomy_runs_lease_expiry_idx;

ALTER TABLE mail_sync_jobs
    DROP COLUMN IF EXISTS lease_expires_at;

ALTER TABLE mail_autonomy_runs
    DROP COLUMN IF EXISTS lease_expires_at;

COMMIT;
