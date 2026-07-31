-- Bounded normalized Provider facts for message projections.
-- This column deliberately excludes raw headers/MIME/body and credentials;
-- provider adapters may retain only small facts such as content type,
-- category labels, folder id, history id or change key.

BEGIN;

ALTER TABLE mail_messages
    ADD COLUMN IF NOT EXISTS provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
