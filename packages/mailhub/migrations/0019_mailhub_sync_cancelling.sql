-- Preserve the worker lease while a running sync reaches a safe cancel
-- checkpoint.  A cancelled queued job remains terminal immediately; a
-- running job is represented as cancelling until its owner finalizes it.

BEGIN;

-- 0003 created an inline status CHECK.  Drop that generated constraint before
-- installing the versioned constraint below so existing databases are safe.
ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_check;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_status_values
    CHECK (status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled'));

COMMIT;
