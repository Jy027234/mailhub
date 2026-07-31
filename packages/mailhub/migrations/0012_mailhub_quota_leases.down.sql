DROP POLICY IF EXISTS mailhub_tenant_isolation ON mail_quota_leases;
DROP INDEX IF EXISTS mail_quota_history_idx;
DROP INDEX IF EXISTS mail_quota_active_idx;
DROP TABLE IF EXISTS mail_quota_leases;
