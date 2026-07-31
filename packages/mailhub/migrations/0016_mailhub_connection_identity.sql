-- Persist non-secret provider identity and credential version returned by the
-- host OAuth broker.  Tokens and refresh material remain outside MailHub.

BEGIN;

ALTER TABLE mail_connections
    ADD COLUMN IF NOT EXISTS provider_account_id TEXT,
    ADD COLUMN IF NOT EXISTS provider_tenant_id TEXT,
    ADD COLUMN IF NOT EXISTS credential_version BIGINT NOT NULL DEFAULT 1;

ALTER TABLE mail_connections
    ADD CONSTRAINT mail_connections_credential_version_positive
    CHECK (credential_version >= 1);

CREATE INDEX IF NOT EXISTS mail_connections_provider_identity_idx
    ON mail_connections (tenant_id, subject_id, provider, provider_account_id)
    WHERE provider_account_id IS NOT NULL;

COMMIT;
