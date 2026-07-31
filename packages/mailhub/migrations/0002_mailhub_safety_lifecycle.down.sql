-- Roll back 0002 only.  0001 remains installed; its core columns are never dropped.
BEGIN;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'mail_connections', 'mail_threads', 'mail_messages',
        'mail_sync_cursors', 'mail_agent_policies', 'mail_delegation_grants',
        'mail_drafts', 'mail_outbox_operations', 'mail_audit_events',
        'mail_action_candidates'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS mailhub_tenant_isolation ON %I', table_name);
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', table_name);
    END LOOP;
END $$;

DROP TABLE IF EXISTS mail_retention_policies;
DROP TABLE IF EXISTS mail_content_objects;

COMMIT;
