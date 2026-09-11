"""Backup/restore and migration-window drill for the durable runtime.

`MAIL-ADOPT-009` asks for disaster-recovery evidence rather than a plan.  This
runs the drill against a real PostgreSQL 16 container and measures what actually
happened:

* a baseline of the schema shape (table count, RLS flags, migration ledger) is
  captured **before** the disaster, so restore fidelity is measured against
  evidence instead of asserted;
* a logical backup is taken with `pg_dump` and held outside the database, the
  way an operator's off-host copy would be;
* the schema is destroyed and restored, and the ledger, table shape, RLS flags
  and a pre-backup row must all come back;
* the RPO boundary is *proved*, not assumed: a row written after the dump must be
  absent from the restored database;
* the migration N-1 window is exercised: roll back to a target, confirm the later
  objects are gone, then re-upgrade and confirm they return.

The container is created and removed by this script.  It never touches an
existing database unless `--database-url` is supplied.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PACKAGE_ROOT / "migrations"
SCHEMA_VERSION = "mailhub.dr_drill.v1"

CONTAINER = "mailhub-dr-postgres"
PORT = "55432"
DB_USER = "mailhub"
DB_PASSWORD = "dr-drill-password"
DB_NAME = "mailhub"
IMAGE = "postgres:16.4-alpine"

ROLLBACK_TARGET = "0016"
#: A representative object added after the rollback target, used to prove the
#: N-1 rollback actually removed later schema rather than only the ledger row.
LATER_COLUMN = ("mail_messages", "cc_addresses")


def _docker(*args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], input=input_bytes, capture_output=True, check=False)


def _psql(sql: str) -> str:
    result = _docker(
        "exec",
        "-i",
        CONTAINER,
        "psql",
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
        "-tAc",
        sql,
    )
    if result.returncode != 0:
        raise RuntimeError("psql_failed: " + result.stderr.decode("utf-8", "replace").strip()[:200])
    return result.stdout.decode("utf-8", "replace").strip()


def _scalar(sql: str) -> int:
    value = _psql(sql)
    return int(value) if value else 0


def _start_container(reuse: bool) -> None:
    if not reuse:
        _docker("rm", "-f", CONTAINER)
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-p",
        f"127.0.0.1:{PORT}:5432",
        "-e",
        f"POSTGRES_USER={DB_USER}",
        "-e",
        f"POSTGRES_PASSWORD={DB_PASSWORD}",
        "-e",
        f"POSTGRES_DB={DB_NAME}",
        IMAGE,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        probe = _docker("exec", CONTAINER, "pg_isready", "-U", DB_USER, "-d", DB_NAME)
        if probe.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError("postgres_did_not_become_ready")


def _database_url() -> str:
    return f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@127.0.0.1:{PORT}/{DB_NAME}"


def _migrate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "apply_migrations.py"),
            "--database-url",
            _database_url(),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=PACKAGE_ROOT,
    )


def _ledger() -> list[str]:
    raw = _psql("SELECT version FROM mailhub_schema_migrations ORDER BY version")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _ledger_numbers(ledger: Sequence[str]) -> list[int]:
    """Compare ledger entries by numeric prefix.

    The ledger stores the migration file stem, so a plain string comparison
    would treat "0016_x" as greater than "0016" and silently drop the target
    itself from the expected set.
    """

    numbers: list[int] = []
    for entry in ledger:
        match = re.match(r"(\d+)", entry)
        if match is None:
            raise RuntimeError(f"ledger_entry_unparsable:{entry}")
        numbers.append(int(match.group(1)))
    return sorted(numbers)


def _table_shape() -> dict[str, Any]:
    return {
        "tables": _scalar(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ),
        "rls_enabled": _scalar(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND rowsecurity"
        ),
    }


def _seed(*, connection_id: str, email: str) -> None:
    _psql(
        "INSERT INTO mail_connections (connection_id, tenant_id, subject_id, provider, "
        "email_address, credential_ref, granted_scopes, content_mode, status, revision, "
        "created_at, updated_at) VALUES ("
        f"'{connection_id}', 'dr-tenant', 'dr-subject', 'imap_smtp', '{email}', "
        "'imapcred_dr', '{}', 'bounded_processing', 'active', 1, now(), now())"
    )


def _connection_count(email: str) -> int:
    return _scalar(f"SELECT count(*) FROM mail_connections WHERE email_address='{email}'")


def _column_exists(table: str, column: str) -> bool:
    return (
        _psql(
            "SELECT count(*) FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' AND column_name='{column}'"
        )
        != "0"
    )


def run(*, keep_container: bool) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    metrics: dict[str, Any] = {}

    _start_container(reuse=False)
    try:
        upgrade = _migrate("--direction", "upgrade")
        checks["migrations_applied"] = upgrade.returncode == 0
        if upgrade.returncode != 0:
            raise RuntimeError("migration_upgrade_failed: " + upgrade.stderr.strip()[:300])

        baseline_shape = _table_shape()
        baseline_ledger = _ledger()
        metrics["baseline"] = {**baseline_shape, "ledger_entries": len(baseline_ledger)}
        checks["baseline_has_full_chain"] = len(baseline_ledger) == len(
            sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.endswith(".down.sql"))
        )
        checks["baseline_has_rls_tables"] = baseline_shape["rls_enabled"] > 0

        pre_backup_id = "11111111-1111-4111-8111-111111111111"
        _seed(connection_id=pre_backup_id, email="pre-backup@example.test")
        checks["pre_backup_row_present"] = _connection_count("pre-backup@example.test") == 1

        dump = _docker("exec", CONTAINER, "pg_dump", "-U", DB_USER, "-d", DB_NAME, "-Fc")
        checks["backup_created"] = dump.returncode == 0 and len(dump.stdout) > 0
        metrics["backup_bytes"] = len(dump.stdout)
        if dump.returncode != 0:
            raise RuntimeError("pg_dump_failed: " + dump.stderr.decode("utf-8", "replace")[:300])

        # Written after the dump: this is what an RPO window would lose.
        post_backup_id = "22222222-2222-4222-8222-222222222222"
        _seed(connection_id=post_backup_id, email="post-backup@example.test")
        checks["post_backup_row_present_before_disaster"] = (
            _connection_count("post-backup@example.test") == 1
        )

        _psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        checks["schema_destroyed"] = _table_shape()["tables"] == 0

        # The restore is the RTO measurement: from an empty schema back to serving.
        restore_started = time.monotonic()
        restore = _docker(
            "exec",
            "-i",
            CONTAINER,
            "pg_restore",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "--no-owner",
            input_bytes=dump.stdout,
        )
        metrics["rto_seconds"] = round(time.monotonic() - restore_started, 2)
        checks["restore_completed"] = restore.returncode == 0
        if restore.returncode != 0:
            raise RuntimeError(
                "pg_restore_failed: " + restore.stderr.decode("utf-8", "replace")[:300]
            )

        restored_shape = _table_shape()
        restored_ledger = _ledger()
        checks["ledger_restored_exactly"] = restored_ledger == baseline_ledger
        checks["table_shape_restored"] = restored_shape == baseline_shape
        checks["rls_flags_restored"] = (
            restored_shape["rls_enabled"] == baseline_shape["rls_enabled"]
        )
        checks["pre_backup_row_restored"] = _connection_count("pre-backup@example.test") == 1
        # The RPO boundary, proved rather than assumed.
        checks["post_backup_row_absent_after_restore"] = (
            _connection_count("post-backup@example.test") == 0
        )
        metrics["restored"] = {**restored_shape, "ledger_entries": len(restored_ledger)}

        # Migration N-1 window: rollback to the target, then re-upgrade.
        rollback = _migrate("--direction", "down", "--target", ROLLBACK_TARGET)
        checks["rollback_completed"] = rollback.returncode == 0
        expected_after_rollback = [
            number for number in _ledger_numbers(baseline_ledger) if number <= int(ROLLBACK_TARGET)
        ]
        rolled_back_numbers = _ledger_numbers(_ledger())
        metrics["ledger_after_rollback"] = rolled_back_numbers
        checks["rollback_removed_later_ledger_rows"] = (
            rolled_back_numbers == expected_after_rollback
        )
        checks["rollback_removed_later_schema"] = not _column_exists(*LATER_COLUMN)

        reupgrade = _migrate("--direction", "upgrade")
        checks["reupgrade_completed"] = reupgrade.returncode == 0
        checks["reupgrade_restored_later_ledger_rows"] = _ledger() == baseline_ledger
        checks["reupgrade_restored_later_schema"] = _column_exists(*LATER_COLUMN)
    finally:
        if not keep_container:
            _docker("rm", "-f", CONTAINER)

    return {
        "schema": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "postgres_image": IMAGE,
        "rollback_target": ROLLBACK_TARGET,
        "checks": checks,
        "metrics": metrics,
        "rpo": {
            "boundary": "the pg_dump instant",
            "seconds": metrics.get("rto_seconds", 0),
            "proved_by": "post_backup_row_absent_after_restore",
        },
        "rto_seconds": metrics.get("rto_seconds"),
        "passed": all(checks.values()),
    }


def validate_bundle(bundle: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if bundle.get("schema") != SCHEMA_VERSION:
        issues.append("schema_mismatch")
    if bundle.get("passed") is not True:
        issues.append("not_passed")
    checks = bundle.get("checks")
    if not isinstance(checks, Mapping) or not checks:
        issues.append("checks_missing")
    else:
        failed = sorted(str(name) for name, value in checks.items() if value is not True)
        if failed:
            issues.append("checks_failed:" + ",".join(failed))
    for key in ("database_url", "password", "token", "secret"):
        if key in bundle:
            issues.append("forbidden_key:" + key)
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup/restore and rollback drill.")
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args()

    if args.validate is not None:
        bundle: Any = json.loads(args.validate.read_text(encoding="utf-8"))
        if not isinstance(bundle, Mapping):
            print("evidence is not a JSON object")
            return 1
        issues = validate_bundle(bundle)
        for issue in issues:
            print("  [FAIL] " + issue)
        print("drill validation: " + ("ok" if not issues else f"{len(issues)} issue(s)"))
        return 0 if not issues else 1

    bundle = run(keep_container=args.keep_container)
    issues = validate_bundle(bundle)
    for name, value in bundle["checks"].items():
        print(f"  [{'PASS' if value else 'FAIL'}] {name}")
    print(f"  RTO: {bundle['rto_seconds']}s | backup {bundle['metrics'].get('backup_bytes')} bytes")
    for issue in issues:
        print("  [FAIL] " + issue)
    print("drill : " + ("pass" if not issues else f"{len(issues)} issue(s)"))
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("wrote: " + str(args.json_path))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
