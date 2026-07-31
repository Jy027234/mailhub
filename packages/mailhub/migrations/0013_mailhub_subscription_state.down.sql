BEGIN;

DROP INDEX IF EXISTS mail_sync_cursors_subscription_expiry_idx;
ALTER TABLE mail_sync_cursors
    DROP CONSTRAINT IF EXISTS mail_sync_cursors_subscription_status_check,
    DROP COLUMN IF EXISTS subscription_ref,
    DROP COLUMN IF EXISTS subscription_status,
    DROP COLUMN IF EXISTS subscription_expires_at,
    DROP COLUMN IF EXISTS subscription_callback_endpoint,
    DROP COLUMN IF EXISTS subscription_client_state_ref,
    DROP COLUMN IF EXISTS subscription_provider_request_id,
    DROP COLUMN IF EXISTS watermark;

COMMIT;
