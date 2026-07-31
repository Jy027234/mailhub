-- Body-free, owner-scoped routing facts for receipt replay.
-- Existing receipt rows remain valid with an empty route; only verified
-- Provider notification routes may populate this metadata.

BEGIN;

ALTER TABLE mail_webhook_receipts
    ADD COLUMN IF NOT EXISTS route_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
