-- The approver identity is not terminal data; dropping it only removes the
-- ability to re-assert separation of duties during the pre-I/O re-check.

BEGIN;

ALTER TABLE mail_outbox_operations
    DROP COLUMN IF EXISTS approver_subject_id;

COMMIT;
