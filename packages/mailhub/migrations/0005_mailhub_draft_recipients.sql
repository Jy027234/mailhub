-- Additive draft recipient/attachment references for controlled reply-all/BCC review.
-- Bodies and attachment bytes remain outside PostgreSQL.
BEGIN;

ALTER TABLE mail_drafts
    ADD COLUMN cc_addresses TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN bcc_addresses TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN attachment_refs TEXT[] NOT NULL DEFAULT '{}';

COMMIT;
