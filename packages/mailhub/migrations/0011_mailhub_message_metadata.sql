-- Preserve bounded message metadata needed by the unified inbox filters.
-- This migration never stores MIME parts, attachment bytes, or message body.
ALTER TABLE mail_messages
    ADD COLUMN attachment_count INTEGER NOT NULL DEFAULT 0
    CHECK (attachment_count >= 0 AND attachment_count <= 100);

COMMENT ON COLUMN mail_messages.attachment_count IS
    'Bounded provider-reported attachment metadata; governed bytes remain outside MailHub.';
