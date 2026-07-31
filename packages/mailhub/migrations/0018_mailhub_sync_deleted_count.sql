-- Persist provider deletion accounting with the durable sync job result.
-- The value is bounded metadata only; message bodies and provider tombstones
-- remain governed by the projection/audit contracts.

BEGIN;

ALTER TABLE mail_sync_jobs
    ADD COLUMN IF NOT EXISTS deleted_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_deleted_count_nonnegative
    CHECK (deleted_count >= 0);

COMMIT;
