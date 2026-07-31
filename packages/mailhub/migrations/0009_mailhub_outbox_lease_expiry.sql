BEGIN;

-- A worker crash can leave an operation in LEASED/SENDING.  Expiry lets the
-- next worker convert the abandoned lease to OUTCOME_UNKNOWN before any new
-- Provider call, forcing reconciliation instead of a blind duplicate send.
ALTER TABLE mail_outbox_operations
    ADD COLUMN lease_expires_at TIMESTAMPTZ;

CREATE INDEX mail_outbox_lease_expiry_idx
    ON mail_outbox_operations (status, lease_expires_at)
    WHERE status IN ('leased', 'sending');

COMMIT;
