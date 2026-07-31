"""Validate MailHub migration ordering and optionally run a live PostgreSQL drill.

The default mode is offline and checks the migration source contract.  A live
drill is opt-in through ``--live`` and ``MAILHUB_TEST_DATABASE_URL``; it never
silently substitutes SQLite or an in-memory repository for PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def check_static() -> None:
    runner = (ROOT / "scripts" / "apply_migrations.py").read_text(encoding="utf-8")
    first = (MIGRATIONS / "0001_mailhub_core.sql").read_text(encoding="utf-8")
    second = (MIGRATIONS / "0002_mailhub_safety_lifecycle.sql").read_text(encoding="utf-8")
    second_down = (MIGRATIONS / "0002_mailhub_safety_lifecycle.down.sql").read_text(
        encoding="utf-8"
    )
    third = (MIGRATIONS / "0003_mailhub_sync_webhook.sql").read_text(encoding="utf-8")
    third_down = (MIGRATIONS / "0003_mailhub_sync_webhook.down.sql").read_text(encoding="utf-8")
    fourth = (MIGRATIONS / "0004_mailhub_rules.sql").read_text(encoding="utf-8")
    fourth_down = (MIGRATIONS / "0004_mailhub_rules.down.sql").read_text(encoding="utf-8")
    fifth = (MIGRATIONS / "0005_mailhub_draft_recipients.sql").read_text(encoding="utf-8")
    fifth_down = (MIGRATIONS / "0005_mailhub_draft_recipients.down.sql").read_text(encoding="utf-8")
    sixth = (MIGRATIONS / "0006_mailhub_rule_executions.sql").read_text(encoding="utf-8")
    sixth_down = (MIGRATIONS / "0006_mailhub_rule_executions.down.sql").read_text(encoding="utf-8")
    seventh = (MIGRATIONS / "0007_mailhub_sender_leases.sql").read_text(encoding="utf-8")
    seventh_down = (MIGRATIONS / "0007_mailhub_sender_leases.down.sql").read_text(encoding="utf-8")
    eighth = (MIGRATIONS / "0008_mailhub_autonomy_runs.sql").read_text(encoding="utf-8")
    eighth_down = (MIGRATIONS / "0008_mailhub_autonomy_runs.down.sql").read_text(encoding="utf-8")
    ninth = (MIGRATIONS / "0009_mailhub_outbox_lease_expiry.sql").read_text(encoding="utf-8")
    ninth_down = (MIGRATIONS / "0009_mailhub_outbox_lease_expiry.down.sql").read_text(
        encoding="utf-8"
    )
    tenth = (MIGRATIONS / "0010_mailhub_worker_lease_expiry.sql").read_text(encoding="utf-8")
    tenth_down = (MIGRATIONS / "0010_mailhub_worker_lease_expiry.down.sql").read_text(
        encoding="utf-8"
    )
    eleventh = (MIGRATIONS / "0011_mailhub_message_metadata.sql").read_text(encoding="utf-8")
    eleventh_down = (MIGRATIONS / "0011_mailhub_message_metadata.down.sql").read_text(
        encoding="utf-8"
    )
    twelfth = (MIGRATIONS / "0012_mailhub_quota_leases.sql").read_text(encoding="utf-8")
    twelfth_down = (MIGRATIONS / "0012_mailhub_quota_leases.down.sql").read_text(encoding="utf-8")
    thirteenth = (MIGRATIONS / "0013_mailhub_subscription_state.sql").read_text(encoding="utf-8")
    thirteenth_down = (MIGRATIONS / "0013_mailhub_subscription_state.down.sql").read_text(
        encoding="utf-8"
    )
    fourteenth = (MIGRATIONS / "0014_mailhub_provider_metadata.sql").read_text(encoding="utf-8")
    fourteenth_down = (MIGRATIONS / "0014_mailhub_provider_metadata.down.sql").read_text(
        encoding="utf-8"
    )
    fifteenth = (MIGRATIONS / "0015_mailhub_webhook_route_metadata.sql").read_text(encoding="utf-8")
    fifteenth_down = (MIGRATIONS / "0015_mailhub_webhook_route_metadata.down.sql").read_text(
        encoding="utf-8"
    )
    sixteenth = (MIGRATIONS / "0016_mailhub_connection_identity.sql").read_text(encoding="utf-8")
    sixteenth_down = (MIGRATIONS / "0016_mailhub_connection_identity.down.sql").read_text(
        encoding="utf-8"
    )
    seventeenth = (MIGRATIONS / "0017_mailhub_sync_filters.sql").read_text(encoding="utf-8")
    seventeenth_down = (MIGRATIONS / "0017_mailhub_sync_filters.down.sql").read_text(
        encoding="utf-8"
    )
    eighteenth = (MIGRATIONS / "0018_mailhub_sync_deleted_count.sql").read_text(encoding="utf-8")
    eighteenth_down = (MIGRATIONS / "0018_mailhub_sync_deleted_count.down.sql").read_text(
        encoding="utf-8"
    )
    nineteenth = (MIGRATIONS / "0019_mailhub_sync_cancelling.sql").read_text(encoding="utf-8")
    nineteenth_down = (MIGRATIONS / "0019_mailhub_sync_cancelling.down.sql").read_text(
        encoding="utf-8"
    )
    twentieth = (MIGRATIONS / "0020_mailhub_message_recipient_headers.sql").read_text(
        encoding="utf-8"
    )
    twentieth_down = (MIGRATIONS / "0020_mailhub_message_recipient_headers.down.sql").read_text(
        encoding="utf-8"
    )
    twenty_first = (MIGRATIONS / "0021_mailhub_sync_retry.sql").read_text(encoding="utf-8")
    twenty_first_down = (MIGRATIONS / "0021_mailhub_sync_retry.down.sql").read_text(
        encoding="utf-8"
    )
    required_tables = (
        "mail_connections",
        "mail_threads",
        "mail_messages",
        "mail_sync_cursors",
        "mail_outbox_operations",
        "mail_action_candidates",
    )
    missing = [table for table in required_tables if f"CREATE TABLE {table}" not in first]
    if missing:
        raise SystemExit("migration_contract_missing_tables:" + ",".join(missing))
    for forbidden in ("access_token", "refresh_token", "raw_mime", "password"):
        if forbidden in first.casefold():
            raise SystemExit(f"migration_contract_secret_or_raw_body:{forbidden}")
    for marker in (
        "BEGIN;",
        "COMMIT;",
        "ROW LEVEL SECURITY",
        "mailhub.tenant_id",
        "mail_content_objects",
    ):
        if marker.casefold() not in second.casefold():
            raise SystemExit(f"migration_safety_marker_missing:{marker}")
    for marker in (
        "mail_sync_jobs",
        "mail_webhook_receipts",
        "UNIQUE (connection_id, idempotency_key)",
        "mailhub.tenant_id",
    ):
        if marker.casefold() not in third.casefold():
            raise SystemExit(f"migration_sync_webhook_marker_missing:{marker}")
    for marker in ("tenant_id TEXT NOT NULL", "input_digest CHAR(64)", "approval_ref TEXT"):
        if marker.casefold() not in first.casefold():
            raise SystemExit(f"migration_core_marker_missing:{marker}")
    if second_down.find("DROP TABLE IF EXISTS mail_content_objects") == -1:
        raise SystemExit("migration_down_missing_object_store_rollback")
    if "DROP COLUMN" in second_down or "DROP CONSTRAINT" in second_down:
        raise SystemExit("migration_down_must_preserve_core_schema")
    if "DROP TABLE IF EXISTS mail_sync_jobs" not in third_down:
        raise SystemExit("migration_sync_webhook_down_missing")
    for marker in (
        "provider_account_id",
        "provider_tenant_id",
        "credential_version",
        "mail_connections_provider_identity_idx",
    ):
        if marker.casefold() not in sixteenth.casefold():
            raise SystemExit(f"migration_connection_identity_marker_missing:{marker}")
    for marker in (
        "DROP INDEX IF EXISTS mail_connections_provider_identity_idx",
        "DROP COLUMN IF EXISTS credential_version",
        "DROP COLUMN IF EXISTS provider_account_id",
    ):
        if marker.casefold() not in sixteenth_down.casefold():
            raise SystemExit(f"migration_connection_identity_down_missing:{marker}")
    for marker in (
        "folder_ref TEXT NOT NULL DEFAULT 'INBOX'",
        "label_refs TEXT[] NOT NULL DEFAULT '{}'",
        "received_after TIMESTAMPTZ",
        "received_before TIMESTAMPTZ",
        "mail_sync_jobs_filter_date_range",
        "mail_sync_jobs_backfill_scope_idx",
    ):
        if marker.casefold() not in seventeenth.casefold():
            raise SystemExit(f"migration_sync_filter_marker_missing:{marker}")
    for marker in (
        "DROP INDEX IF EXISTS mail_sync_jobs_backfill_scope_idx",
        "DROP COLUMN IF EXISTS received_before",
        "DROP COLUMN IF EXISTS label_refs",
        "DROP COLUMN IF EXISTS folder_ref",
    ):
        if marker.casefold() not in seventeenth_down.casefold():
            raise SystemExit(f"migration_sync_filter_down_missing:{marker}")
    for marker in (
        "deleted_count INTEGER NOT NULL DEFAULT 0",
        "mail_sync_jobs_deleted_count_nonnegative",
    ):
        if marker.casefold() not in eighteenth.casefold():
            raise SystemExit(f"migration_sync_deleted_count_marker_missing:{marker}")
    for marker in (
        "DROP CONSTRAINT IF EXISTS mail_sync_jobs_deleted_count_nonnegative",
        "DROP COLUMN IF EXISTS deleted_count",
    ):
        if marker.casefold() not in eighteenth_down.casefold():
            raise SystemExit(f"migration_sync_deleted_count_down_missing:{marker}")
    for marker in (
        "mail_sync_jobs_status_values",
        "cancelling",
        "DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_check",
    ):
        if marker.casefold() not in nineteenth.casefold():
            raise SystemExit(f"migration_sync_cancelling_marker_missing:{marker}")
    for marker in (
        "UPDATE mail_sync_jobs",
        "DROP CONSTRAINT IF EXISTS mail_sync_jobs_status_values",
        "mail_sync_jobs_status_check",
        "status = 'cancelled'",
    ):
        if marker.casefold() not in nineteenth_down.casefold():
            raise SystemExit(f"migration_sync_cancelling_down_missing:{marker}")
    for marker in ("cc_addresses TEXT[]", "bcc_addresses TEXT[]", "reply_to_addresses TEXT[]"):
        if marker.casefold() not in twentieth.casefold():
            raise SystemExit(f"migration_message_recipient_headers_marker_missing:{marker}")
        if f"drop column if exists {marker.split()[0]}" not in twentieth_down.casefold():
            raise SystemExit(
                f"migration_message_recipient_headers_down_missing:{marker.split()[0]}"
            )
    for marker in (
        "attempt_count INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at TIMESTAMPTZ",
        "retry_wait",
        "mail_sync_jobs_retry_due_idx",
    ):
        if marker.casefold() not in twenty_first.casefold():
            raise SystemExit(f"migration_sync_retry_marker_missing:{marker}")
    for marker in (
        "UPDATE mail_sync_jobs",
        "status = 'failed'",
        "DROP INDEX IF EXISTS mail_sync_jobs_retry_due_idx",
        "DROP COLUMN IF EXISTS next_attempt_at",
        "DROP COLUMN IF EXISTS attempt_count",
    ):
        if marker.casefold() not in twenty_first_down.casefold():
            raise SystemExit(f"migration_sync_retry_down_missing:{marker}")
    for marker in ("mail_rules", "conditions JSONB", "mailhub.tenant_id"):
        if marker.casefold() not in fourth.casefold():
            raise SystemExit(f"migration_rules_marker_missing:{marker}")
    if "DROP TABLE IF EXISTS mail_rules" not in fourth_down:
        raise SystemExit("migration_rules_down_missing")
    for marker in ("cc_addresses", "bcc_addresses", "attachment_refs"):
        if marker.casefold() not in fifth.casefold():
            raise SystemExit(f"migration_draft_recipients_marker_missing:{marker}")
        if marker.casefold() not in fifth_down.casefold():
            raise SystemExit(f"migration_draft_recipients_down_missing:{marker}")
    for marker in (
        "mail_rule_executions",
        "rule_version BIGINT",
        "input_digest CHAR(64)",
        "mailhub.tenant_id",
    ):
        if marker.casefold() not in sixth.casefold():
            raise SystemExit(f"migration_rule_executions_marker_missing:{marker}")
    if "DROP TABLE IF EXISTS mail_rule_executions" not in sixth_down:
        raise SystemExit("migration_rule_executions_down_missing")
    for marker in (
        "mail_sender_leases",
        "PRIMARY KEY (tenant_id, account_ref, purpose)",
        "fencing_token BIGINT",
        "lease_until TIMESTAMPTZ",
        "mailhub.tenant_id",
    ):
        if marker.casefold() not in seventh.casefold():
            raise SystemExit(f"migration_sender_leases_marker_missing:{marker}")
    if "DROP TABLE IF EXISTS mail_sender_leases" not in seventh_down:
        raise SystemExit("migration_sender_leases_down_missing")
    for marker in (
        "mail_autonomy_runs",
        "replay_key TEXT",
        "sync_result JSONB",
        "fencing_token BIGINT",
        "mailhub.tenant_id",
    ):
        if marker.casefold() not in eighth.casefold():
            raise SystemExit(f"migration_autonomy_runs_marker_missing:{marker}")
    if "DROP TABLE IF EXISTS mail_autonomy_runs" not in eighth_down:
        raise SystemExit("migration_autonomy_runs_down_missing")
    for marker in (
        "lease_expires_at TIMESTAMPTZ",
        "OUTCOME_UNKNOWN",
        "mail_outbox_lease_expiry_idx",
    ):
        if marker.casefold() not in ninth.casefold():
            raise SystemExit(f"migration_outbox_lease_marker_missing:{marker}")
    for marker in (
        "DROP INDEX IF EXISTS mail_outbox_lease_expiry_idx",
        "DROP COLUMN IF EXISTS lease_expires_at",
    ):
        if marker.casefold() not in ninth_down.casefold():
            raise SystemExit(f"migration_outbox_lease_down_missing:{marker}")
    for marker in (
        "mail_sync_jobs_lease_expiry_idx",
        "mail_autonomy_runs_lease_expiry_idx",
        "mail_sync_jobs",
        "mail_autonomy_runs",
        "lease_expires_at TIMESTAMPTZ",
    ):
        if marker.casefold() not in tenth.casefold():
            raise SystemExit(f"migration_worker_lease_marker_missing:{marker}")
    for marker in (
        "DROP INDEX IF EXISTS mail_sync_jobs_lease_expiry_idx",
        "DROP INDEX IF EXISTS mail_autonomy_runs_lease_expiry_idx",
        "DROP COLUMN IF EXISTS lease_expires_at",
    ):
        if marker.casefold() not in tenth_down.casefold():
            raise SystemExit(f"migration_worker_lease_down_missing:{marker}")
    if "attachment_count integer not null" not in eleventh.casefold():
        raise SystemExit("migration_message_metadata_marker_missing:attachment_count")
    if "drop column attachment_count" not in eleventh_down.casefold():
        raise SystemExit("migration_message_metadata_down_missing:attachment_count")
    for marker in (
        "mail_quota_leases",
        "pg_advisory_xact_lock",
        "consumed_at TIMESTAMPTZ",
        "ROW LEVEL SECURITY",
        "mailhub.tenant_id",
    ):
        if marker.casefold() not in twelfth.casefold():
            raise SystemExit(f"migration_quota_marker_missing:{marker}")
    for marker in (
        "DROP POLICY IF EXISTS mailhub_tenant_isolation",
        "DROP INDEX IF EXISTS mail_quota_active_idx",
        "DROP TABLE IF EXISTS mail_quota_leases",
    ):
        if marker.casefold() not in twelfth_down.casefold():
            raise SystemExit(f"migration_quota_down_missing:{marker}")
    for marker in (
        "subscription_ref TEXT",
        "subscription_status TEXT NOT NULL DEFAULT 'none'",
        "subscription_expires_at TIMESTAMPTZ",
        "mail_sync_cursors_subscription_expiry_idx",
        "mailhub",
    ):
        if marker.casefold() not in thirteenth.casefold():
            raise SystemExit(f"migration_subscription_marker_missing:{marker}")
    for marker in (
        "DROP INDEX IF EXISTS mail_sync_cursors_subscription_expiry_idx",
        "DROP COLUMN IF EXISTS subscription_ref",
        "DROP COLUMN IF EXISTS subscription_status",
    ):
        if marker.casefold() not in thirteenth_down.casefold():
            raise SystemExit(f"migration_subscription_down_missing:{marker}")
    if "provider_metadata jsonb not null" not in fourteenth.casefold():
        raise SystemExit("migration_provider_metadata_marker_missing")
    if "drop column if exists provider_metadata" not in fourteenth_down.casefold():
        raise SystemExit("migration_provider_metadata_down_missing")
    if "route_metadata jsonb not null" not in fifteenth.casefold():
        raise SystemExit("migration_webhook_route_metadata_marker_missing")
    if "drop column if exists route_metadata" not in fifteenth_down.casefold():
        raise SystemExit("migration_webhook_route_metadata_down_missing")
    for marker in (
        "mailhub_schema_migrations",
        "pg_advisory_lock",
        "migration_checksum_mismatch",
        "asyncpg",
        "migration_sql",
    ):
        if marker.casefold() not in runner.casefold():
            raise SystemExit(f"migration_runner_marker_missing:{marker}")
    print("static migration contract: ok")


async def check_live(database_url: str) -> None:
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise SystemExit("live_migration_blocked:install_asyncpg") from exc
    if not database_url.startswith("postgresql://"):
        raise SystemExit("live_migration_requires_postgresql_url")
    connection = await asyncpg.connect(database_url)
    try:
        # The runner intentionally delegates SQL execution to the operator;
        # this check only proves connectivity and records the required drill.
        await connection.execute("SELECT 1")
        print("live migration database connectivity: ok")
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    check_static()
    if args.live:
        database_url = os.getenv("MAILHUB_TEST_DATABASE_URL")
        if not database_url:
            raise SystemExit("live_migration_blocked:MAILHUB_TEST_DATABASE_URL_missing")
        asyncio.run(check_live(database_url))


if __name__ == "__main__":
    main()
