"""Point-in-time recovery drill against a real PostgreSQL cluster.

The existing drill proves a logical backup restores.  This proves the other
half of MAIL-ADOPT-009: continuous WAL archiving, and recovery to a moment
*between* two writes, so that the write after the target is provably gone.

Everything happens inside one container.  Mounting a PostgreSQL data directory
from the host works badly on Windows, and the point of the drill is PostgreSQL
behaviour, not host filesystem behaviour.

    python scripts/pitr_drill.py --json ../../docs/reports/mailhub-pitr-drill.json
    python scripts/pitr_drill.py --validate ../../docs/reports/mailhub-pitr-drill.json

The drill never touches a real database: the cluster is a throwaway container,
and the archive lives inside it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONTAINER = "mailhub-pitr-postgres"
IMAGE = "postgres:16.4-alpine"
DB_USER = "mailhub"
DB_PASSWORD = "pitr-drill-password"
DB_NAME = "mailhub"
ARCHIVE_DIR = "/pitr/archive"
BACKUP_DIR = "/pitr/base"
RESTORE_PORT = "5433"
SCHEMA = "mailhub.pitr_drill.v1"

MARKER_TABLE = "pitr_marker"


def _docker(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], capture_output=True, check=False)


def _exec(*args: str, user: str = "postgres", env: Mapping[str, str] | None = None) -> str:
    command = ["exec", "-u", user]
    for key, value in (env or {}).items():
        command += ["-e", key + "=" + value]
    command += [CONTAINER, *args]
    result = _docker(*command)
    if result.returncode != 0:
        raise RuntimeError(
            "exec_failed:"
            + " ".join(args[:2])
            + ":"
            + result.stderr.decode("utf-8", "replace").strip()[:300]
        )
    return result.stdout.decode("utf-8", "replace")


def _psql(sql: str, *, port: str = "5432", database: str = DB_NAME) -> str:
    return _exec(
        "psql",
        "-U",
        DB_USER,
        "-d",
        database,
        "-p",
        port,
        "-tAc",
        sql,
        env={"PGPASSWORD": DB_PASSWORD},
    ).strip()


def _wait_ready(*, port: str = "5432", timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = _docker(
            "exec", "-u", "postgres", CONTAINER, "pg_isready", "-U", DB_USER, "-p", port
        )
        if probe.returncode == 0:
            return True
        time.sleep(2)
    return False


def _start_container() -> None:
    _docker("rm", "-f", CONTAINER)
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-e",
        "POSTGRES_USER=" + DB_USER,
        "-e",
        "POSTGRES_PASSWORD=" + DB_PASSWORD,
        "-e",
        "POSTGRES_DB=" + DB_NAME,
        IMAGE,
    )
    if not _wait_ready():
        raise RuntimeError("postgres_did_not_become_ready")


def _enable_archiving() -> str:
    """Turn on WAL archiving and restart so the setting takes effect."""

    # The postgres user cannot create directories at the filesystem root, so
    # the drill directory is made as root and handed over.
    _exec("mkdir", "-p", ARCHIVE_DIR, BACKUP_DIR, user="root")
    _exec("chown", "-R", "postgres:postgres", "/pitr", user="root")
    _psql("ALTER SYSTEM SET wal_level = 'replica'")
    _psql("ALTER SYSTEM SET archive_mode = on")
    _psql("ALTER SYSTEM SET archive_command = 'cp %p " + ARCHIVE_DIR + "/%f'")
    _psql("ALTER SYSTEM SET archive_timeout = '5s'")
    _docker("restart", CONTAINER)
    if not _wait_ready():
        raise RuntimeError("postgres_did_not_return_after_restart")
    return _psql("SHOW archive_mode")


def _archive_segments() -> int:
    listing = _exec("sh", "-c", "ls -1 " + ARCHIVE_DIR + " | wc -l")
    return int(listing.strip() or "0")


def run(*, keep_container: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    _start_container()
    check("container_ready", True, IMAGE)
    archive_mode = _enable_archiving()
    check("archive_mode_enabled", archive_mode == "on", "archive_mode=" + archive_mode)

    _psql(
        "CREATE TABLE IF NOT EXISTS "
        + MARKER_TABLE
        + " (id integer PRIMARY KEY, note text NOT NULL, at timestamptz NOT NULL DEFAULT now())"
    )
    # Force a segment boundary so the base backup and the marker land in
    # different WAL files; otherwise the archive could stay empty by luck.
    _psql("SELECT pg_switch_wal()")

    backup = _docker(
        "exec",
        "-u",
        "postgres",
        "-e",
        "PGPASSWORD=" + DB_PASSWORD,
        CONTAINER,
        "pg_basebackup",
        "-D",
        BACKUP_DIR,
        "-Fp",
        "-Xs",
        "-R",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        DB_USER,
    )
    check(
        "base_backup_taken",
        backup.returncode == 0,
        backup.stderr.decode("utf-8", "replace").strip()[:200],
    )
    # pg_basebackup creates the destination with the process umask, and the
    # server refuses to start on anything looser than 0750.
    _exec("chmod", "700", BACKUP_DIR, user="root")

    _psql("INSERT INTO " + MARKER_TABLE + " (id, note) VALUES (1, 'before-target')")
    # The boundary has to be unambiguous, so the target time is captured a
    # whole second after the write it must keep, and before the one it must drop.
    time.sleep(1.2)
    target_time = _psql("SELECT clock_timestamp()")
    _psql("INSERT INTO " + MARKER_TABLE + " (id, note) VALUES (2, 'after-target')")
    _psql("SELECT pg_switch_wal()")
    time.sleep(1.5)

    segments = _archive_segments()
    check("wal_segments_archived", segments > 0, str(segments) + " files in " + ARCHIVE_DIR)

    # Prepare the restored copy: a recovery target rather than a streaming
    # standby, so it stops where we tell it to instead of following the primary.
    _exec(
        "sh",
        "-c",
        "rm -f "
        + BACKUP_DIR
        + "/standby.signal && touch "
        + BACKUP_DIR
        + "/recovery.signal && printf '%s\\n' \"restore_command = 'cp "
        + ARCHIVE_DIR
        + "/%f %p'\" \"recovery_target_time = '"
        + target_time
        + "'\" \"recovery_target_action = 'promote'\" >> "
        + BACKUP_DIR
        + "/postgresql.auto.conf",
    )
    _exec(
        "sh",
        "-c",
        "pg_ctl -D " + BACKUP_DIR + ' -o "-p ' + RESTORE_PORT + '" -l /pitr/recovery.log -w start',
    )
    recovered = _wait_ready(port=RESTORE_PORT)
    check("recovery_completed", recovered, "cluster answers on port " + RESTORE_PORT)

    before_count = int(_psql("SELECT count(*) FROM " + MARKER_TABLE, port=RESTORE_PORT) or "0")
    kept = int(
        _psql(
            "SELECT count(*) FROM " + MARKER_TABLE + " WHERE note = 'before-target'",
            port=RESTORE_PORT,
        )
        or "0"
    )
    dropped = int(
        _psql(
            "SELECT count(*) FROM " + MARKER_TABLE + " WHERE note = 'after-target'",
            port=RESTORE_PORT,
        )
        or "0"
    )
    check("write_before_target_recovered", kept == 1, "rows with note=before-target: " + str(kept))
    check(
        "write_after_target_excluded",
        dropped == 0,
        "rows with note=after-target: " + str(dropped),
    )
    check(
        "recovery_is_point_in_time",
        before_count == 1,
        "total rows in the restored cluster: " + str(before_count),
    )
    recovery_log = _exec("sh", "-c", "cat /pitr/recovery.log")
    # WAL generated after the base backup can only come from the archive, so
    # counting the restore lines is what proves archiving was exercised rather
    # than merely configured.
    restored_files = recovery_log.count("restored log file")
    check(
        "recovery_restored_from_archive",
        restored_files > 0,
        str(restored_files) + " WAL files restored from " + ARCHIVE_DIR,
    )
    stopped_at_target = "recovery stopping before commit" in recovery_log
    check(
        "recovery_stopped_at_target",
        stopped_at_target,
        "log records an explicit stop at the target time",
    )
    if not keep_container:
        _docker("rm", "-f", CONTAINER)

    failures = [item["name"] for item in checks if not item["ok"]]
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "image": IMAGE,
        "archive_dir": ARCHIVE_DIR,
        "backup_dir": BACKUP_DIR,
        "recovery_target_time": target_time,
        "archived_wal_segments": segments,
        "checks": checks,
        "failures": failures,
        "passed": not failures,
    }


def validate_bundle(bundle: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if bundle.get("schema") != SCHEMA:
        return ["schema_mismatch"]
    checks = bundle.get("checks")
    if not isinstance(checks, list) or not checks:
        return ["checks_missing"]
    for entry in checks:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            issues.append("check_without_a_name")
            continue
        if entry.get("ok") is not True:
            issues.append("check_failed:" + str(entry.get("name")))
    names = {str(item.get("name")) for item in checks if isinstance(item, Mapping)}
    for required in (
        "archive_mode_enabled",
        "base_backup_taken",
        "wal_segments_archived",
        "write_before_target_recovered",
        "write_after_target_excluded",
        "recovery_restored_from_archive",
        "recovery_stopped_at_target",
    ):
        if required not in names:
            issues.append("check_missing:" + required)
    if int(bundle.get("archived_wal_segments") or 0) <= 0:
        issues.append("no_wal_segments_archived")
    if bundle.get("failures") not in ([], None):
        issues.append("failures_recorded")
    if bundle.get("passed") is not True:
        issues.append("not_passed")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args()

    if args.validate is not None:
        recorded: Any = json.loads(args.validate.read_text(encoding="utf-8"))
        if not isinstance(recorded, Mapping):
            print("evidence is not a JSON object")
            return 1
        problems = validate_bundle(recorded)
        if problems:
            print("INVALID")
            for problem in problems:
                print("  - " + problem)
            return 1
        print("evidence ok: " + str(len(recorded.get("checks") or [])) + " checks")
        return 0

    bundle = run(keep_container=args.keep_container)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8"
        )
    for item in bundle["checks"]:
        print(
            ("ok  " if item["ok"] else "FAIL")
            + " "
            + str(item["name"])
            + " :: "
            + str(item["detail"])
        )
    print("")
    print("target   : " + str(bundle["recovery_target_time"]))
    print("segments : " + str(bundle["archived_wal_segments"]))
    print(
        "checks   : "
        + str(len(bundle["checks"]) - len(bundle["failures"]))
        + "/"
        + str(len(bundle["checks"]))
    )
    if args.json is not None:
        print("evidence : " + str(args.json))
    return 0 if bundle["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
