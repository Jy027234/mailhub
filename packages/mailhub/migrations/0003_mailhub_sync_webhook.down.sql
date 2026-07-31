-- Roll back only the additive sync/webhook tables.  0001/0002 core data is preserved.

BEGIN;
ALTER TABLE IF EXISTS mail_webhook_receipts DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS mail_sync_jobs DISABLE ROW LEVEL SECURITY;
DROP TABLE IF EXISTS mail_webhook_receipts;
DROP TABLE IF EXISTS mail_sync_jobs;
COMMIT;
