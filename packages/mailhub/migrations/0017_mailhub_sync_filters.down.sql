BEGIN;

DROP INDEX IF EXISTS mail_sync_jobs_backfill_scope_idx;
ALTER TABLE mail_sync_jobs
    DROP CONSTRAINT IF EXISTS mail_sync_jobs_filter_date_range,
    DROP COLUMN IF EXISTS received_before,
    DROP COLUMN IF EXISTS received_after,
    DROP COLUMN IF EXISTS label_refs,
    DROP COLUMN IF EXISTS folder_ref;

COMMIT;
