-- MailHub durable, non-secret Gmail watch / Graph subscription state.
-- Provider credentials, client-state values and notification bodies remain
-- host-owned; this migration stores only bounded references and timestamps.

BEGIN;

ALTER TABLE mail_sync_cursors
    ADD COLUMN IF NOT EXISTS subscription_ref TEXT,
    ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS subscription_callback_endpoint TEXT,
    ADD COLUMN IF NOT EXISTS subscription_client_state_ref TEXT,
    ADD COLUMN IF NOT EXISTS subscription_provider_request_id TEXT,
    ADD COLUMN IF NOT EXISTS watermark TIMESTAMPTZ;

ALTER TABLE mail_sync_cursors
    DROP CONSTRAINT IF EXISTS mail_sync_cursors_subscription_status_check;

ALTER TABLE mail_sync_cursors
    ADD CONSTRAINT mail_sync_cursors_subscription_status_check
    CHECK (subscription_status IN ('none', 'active', 'renewal_required', 'expired', 'cancelled', 'failed'));

CREATE INDEX IF NOT EXISTS mail_sync_cursors_subscription_expiry_idx
    ON mail_sync_cursors (subscription_expires_at)
    WHERE subscription_status IN ('active', 'renewal_required');

COMMIT;
