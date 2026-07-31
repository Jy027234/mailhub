BEGIN;

DROP INDEX IF EXISTS mail_connections_provider_identity_idx;
ALTER TABLE mail_connections
    DROP CONSTRAINT IF EXISTS mail_connections_credential_version_positive;
ALTER TABLE mail_connections
    DROP COLUMN IF EXISTS credential_version,
    DROP COLUMN IF EXISTS provider_tenant_id,
    DROP COLUMN IF EXISTS provider_account_id;

COMMIT;
