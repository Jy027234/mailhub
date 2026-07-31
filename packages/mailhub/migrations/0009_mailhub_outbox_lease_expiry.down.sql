BEGIN;

DROP INDEX IF EXISTS mail_outbox_lease_expiry_idx;
ALTER TABLE mail_outbox_operations
    DROP COLUMN IF EXISTS lease_expires_at;

COMMIT;
