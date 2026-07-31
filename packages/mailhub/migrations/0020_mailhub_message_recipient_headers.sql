-- Preserve normalized recipient header classes for inbound messages.
-- Provider adapters never persist raw RFC 5322 headers or display names.

BEGIN;

ALTER TABLE mail_messages
    ADD COLUMN IF NOT EXISTS cc_addresses TEXT[] NOT NULL DEFAULT '{}';

ALTER TABLE mail_messages
    ADD COLUMN IF NOT EXISTS bcc_addresses TEXT[] NOT NULL DEFAULT '{}';

ALTER TABLE mail_messages
    ADD COLUMN IF NOT EXISTS reply_to_addresses TEXT[] NOT NULL DEFAULT '{}';

COMMIT;
