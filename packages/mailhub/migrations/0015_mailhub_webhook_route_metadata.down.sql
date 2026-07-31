BEGIN;

ALTER TABLE mail_webhook_receipts
    DROP COLUMN IF EXISTS route_metadata;

COMMIT;
