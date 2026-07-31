BEGIN;

-- A rollback must not strand an in-flight cancellation in a status that the
-- previous schema cannot represent.  The worker lease is deliberately fenced
-- before the status is collapsed to the old terminal value.
UPDATE mail_sync_jobs
SET status = 'cancelled',
    lease_owner = NULL,
    lease_expires_at = NULL,
    finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
    updated_at = CURRENT_TIMESTAMP
WHERE status = 'cancelling';

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_values;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_status_check
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'));

COMMIT;
