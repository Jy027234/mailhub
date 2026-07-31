-- Persist bounded backfill filters with the durable sync job.
-- Provider query languages remain connector-owned; this migration stores only
-- normalized folder/label/date facts and no search expression or message body.

BEGIN;

ALTER TABLE mail_sync_jobs
    ADD COLUMN IF NOT EXISTS folder_ref TEXT NOT NULL DEFAULT 'INBOX',
    ADD COLUMN IF NOT EXISTS label_refs TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS received_after TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS received_before TIMESTAMPTZ;

ALTER TABLE mail_sync_jobs
    ADD CONSTRAINT mail_sync_jobs_filter_date_range
    CHECK (received_after IS NULL OR received_before IS NULL OR received_after < received_before);

CREATE INDEX IF NOT EXISTS mail_sync_jobs_backfill_scope_idx
    ON mail_sync_jobs (tenant_id, subject_id, connection_id, mode, created_at DESC)
    WHERE mode = 'backfill';

COMMIT;
