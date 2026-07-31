BEGIN;

ALTER TABLE mail_messages
    DROP COLUMN IF EXISTS provider_metadata;

COMMIT;
