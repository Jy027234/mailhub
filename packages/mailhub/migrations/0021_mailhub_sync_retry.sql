-- Durable bounded retry state for transient Provider/network sync failures.
-- A retry-wait job keeps its idempotency key and cursor fence, but releases
-- the worker lease until next_attempt_at is due.

BEGIN;

ALTER TABLE mail_sync_jobs
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE mail_sync_jobs
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_attempt_count_nonnegative;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_attempt_count_nonnegative
    CHECK (attempt_count >= 0);

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_values;

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_check;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_status_values
    CHECK (status IN ('queued', 'running', 'retry_wait', 'cancelling', 'succeeded', 'failed', 'cancelled'));

CREATE INDEX IF NOT EXISTS mail_sync_jobs_retry_due_idx
    ON mail_sync_jobs (status, next_attempt_at)
    WHERE status = 'retry_wait';

COMMIT;
