BEGIN;

ALTER TABLE mail_messages
    DROP COLUMN IF EXISTS reply_to_addresses;

ALTER TABLE mail_messages
    DROP COLUMN IF EXISTS bcc_addresses;

ALTER TABLE mail_messages
    DROP COLUMN IF EXISTS cc_addresses;

COMMIT;
