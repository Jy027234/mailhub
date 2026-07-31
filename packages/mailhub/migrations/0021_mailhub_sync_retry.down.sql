-- Retry state is not terminal data.  Reconcile any pending row before
-- removing the status/metadata columns during rollback.

BEGIN;

UPDATE mail_sync_jobs
SET status = 'failed',
    error_code = COALESCE(error_code, 'sync_retry_migration_rollback'),
    finished_at = COALESCE(finished_at, NOW()),
    updated_at = NOW()
WHERE status = 'retry_wait';

DROP INDEX IF EXISTS mail_sync_jobs_retry_due_idx;

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_values;

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_check;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_status_values
    CHECK (status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled'));

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_attempt_count_nonnegative;

ALTER TABLE mail_sync_jobs
    DROP COLUMN IF EXISTS next_attempt_at;

ALTER TABLE mail_sync_jobs
    DROP COLUMN IF EXISTS attempt_count;

COMMIT;
