BEGIN;

ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_deleted_count_nonnegative,
    DROP COLUMN IF EXISTS deleted_count;

COMMIT;
