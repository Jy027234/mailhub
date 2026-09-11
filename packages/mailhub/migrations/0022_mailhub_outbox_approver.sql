-- Who approved an outbound send, recorded when the confirmation was verified.
-- Without it the pre-I/O re-check could only ask "was this confirmation
-- exercised?" and not "was it approved by somebody other than the requester?",
-- so separation of duties could not be re-asserted at the last moment.

BEGIN;

ALTER TABLE mail_outbox_operations
    ADD COLUMN IF NOT EXISTS approver_subject_id TEXT;

COMMENT ON COLUMN mail_outbox_operations.approver_subject_id IS
    'Principal that exercised the approval confirmation; NULL when the send needed no confirmation.';

COMMIT;
