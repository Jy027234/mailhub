BEGIN;

ALTER TABLE mail_drafts
    DROP COLUMN IF EXISTS attachment_refs,
    DROP COLUMN IF EXISTS bcc_addresses,
    DROP COLUMN IF EXISTS cc_addresses;

COMMIT;
