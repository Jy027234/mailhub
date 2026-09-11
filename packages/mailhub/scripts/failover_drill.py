"""Streaming-replication failover drill across two real PostgreSQL containers.

The backup and point-in-time drills prove data survives.  Neither proves the
service keeps running when the primary is gone, which is what a standby is for.

The primary and the standby are separate containers on their own network.  That
matters: in the official image postgres is PID 1, and the kernel ignores SIGQUIT
sent to PID 1, so a cluster sharing a container cannot actually be killed --
only the container runtime can remove it.  A drill where the primary merely
restarts proves nothing about failover.

    python scripts/failover_drill.py --json ../../docs/reports/mailhub-failover-drill.json
    python scripts/failover_drill.py --validate ../../docs/reports/mailhub-failover-drill.json

The decisive check is the timeline: promotion advances it, so a bundle that
restarted a second cluster instead of promoting it cannot pass.
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

NETWORK = "mailhub-failover-net"
PRIMARY = "mailhub-failover-primary"
STANDBY = "mailhub-failover-standby"
IMAGE = "postgres:16.4-alpine"
DB_USER = "mailhub"
DB_PASSWORD = "failover-drill-password"
DB_NAME = "mailhub"
STANDBY_DIR = "/failover/standby"
SLOT = "mailhub_standby"
MARKER_TABLE = "failover_marker"
SCHEMA = "mailhub.failover_drill.v1"


def _docker(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], capture_output=True, check=False)


def _exec_on(
    container: str, *args: str, user: str = "postgres", env: Mapping[str, str] | None = None
) -> str:
    command = ["exec", "-u", user]
    for key, value in (env or {}).items():
        command += ["-e", key + "=" + value]
    command += [container, *args]
    result = _docker(*command)
    if result.returncode != 0:
        # Both streams: pg_ctl explains itself on stdout and leaves stderr empty,
        # so reporting only stderr hides why a command failed.
        combined = (
            result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
        ).strip()
        raise RuntimeError("exec_failed:" + " ".join(args[:2]) + ":" + combined[:300])
    return result.stdout.decode("utf-8", "replace")


def _psql_on(container: str, sql: str) -> str:
    return _exec_on(
        container,
        "psql",
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
        "-tAc",
        sql,
        env={"PGPASSWORD": DB_PASSWORD},
    ).strip()


def _wait_ready(container: str, *, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = _docker("exec", "-u", "postgres", container, "pg_isready", "-U", DB_USER)
        if probe.returncode == 0:
            return True
        time.sleep(2)
    return False


def _remove_containers() -> None:
    """Containers only: the network is created before this runs."""

    _docker("rm", "-f", PRIMARY)
    _docker("rm", "-f", STANDBY)


def _teardown() -> None:
    _remove_containers()
    # Only removable once nothing is attached to it.
    _docker("network", "rm", NETWORK)


def _start_primary() -> None:
    _docker(
        "run",
        "-d",
        "--name",
        PRIMARY,
        "--network",
        NETWORK,
        "-e",
        "POSTGRES_USER=" + DB_USER,
        "-e",
        "POSTGRES_PASSWORD=" + DB_PASSWORD,
        "-e",
        "POSTGRES_DB=" + DB_NAME,
        IMAGE,
    )
    if not _wait_ready(PRIMARY):
        raise RuntimeError("primary_did_not_become_ready")


def _start_standby_shell() -> None:
    """A running container with no cluster, so the copy can be made into it."""

    _docker(
        "run",
        "-d",
        "--name",
        STANDBY,
        "--network",
        NETWORK,
        "--entrypoint",
        "sleep",
        IMAGE,
        "infinity",
    )
    _exec_on(STANDBY, "mkdir", "-p", "/failover", user="root")
    _exec_on(STANDBY, "chown", "-R", "postgres:postgres", "/failover", user="root")


def _enable_replication() -> str:
    _psql_on(PRIMARY, "ALTER SYSTEM SET wal_level = 'replica'")
    _psql_on(PRIMARY, "ALTER SYSTEM SET max_wal_senders = 4")
    _psql_on(PRIMARY, "ALTER SYSTEM SET max_replication_slots = 4")
    _psql_on(PRIMARY, "ALTER SYSTEM SET wal_keep_size = '64MB'")
    # The image ships replication rules for 127.0.0.1 only and the standby
    # arrives from another address, so the rule has to be added explicitly.
    hba_file = _psql_on(PRIMARY, "SHOW hba_file")
    _exec_on(
        PRIMARY,
        "sh",
        "-c",
        "echo 'host replication all all scram-sha-256' >> " + hba_file,
    )
    _docker("restart", PRIMARY)
    if not _wait_ready(PRIMARY):
        raise RuntimeError("primary_did_not_return_after_restart")
    return _psql_on(PRIMARY, "SHOW wal_level")


def _wait_for_row(container: str, note: str, timeout: float = 45.0) -> bool:
    """Replication is asynchronous: the row arriving is polled, never assumed."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found = _psql_on(
                container,
                "SELECT count(*) FROM " + MARKER_TABLE + " WHERE note = '" + note + "'",
            )
        except RuntimeError:
            time.sleep(1)
            continue
        if found == "1":
            return True
        time.sleep(1)
    return False


def run(*, keep_containers: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    _docker("network", "create", NETWORK)
    _remove_containers()
    _start_primary()
    check("primary_ready", True, IMAGE)

    wal_level = _enable_replication()
    check("primary_accepts_replication", wal_level == "replica", "wal_level=" + wal_level)

    _psql_on(PRIMARY, "SELECT pg_create_physical_replication_slot('" + SLOT + "')")
    _psql_on(
        PRIMARY,
        "CREATE TABLE IF NOT EXISTS "
        + MARKER_TABLE
        + " (id integer PRIMARY KEY, note text NOT NULL, at timestamptz NOT NULL DEFAULT now())",
    )
    _psql_on(PRIMARY, "INSERT INTO " + MARKER_TABLE + " (id, note) VALUES (1, 'before-failover')")

    _start_standby_shell()
    backup = _docker(
        "exec",
        "-u",
        "postgres",
        "-e",
        "PGPASSWORD=" + DB_PASSWORD,
        STANDBY,
        "pg_basebackup",
        "-D",
        STANDBY_DIR,
        "-Fp",
        "-Xs",
        "-R",
        "-S",
        SLOT,
        "-h",
        PRIMARY,
        "-p",
        "5432",
        "-U",
        DB_USER,
    )
    taken = check(
        "standby_base_backup_taken",
        backup.returncode == 0,
        backup.stderr.decode("utf-8", "replace").strip()[:200],
    )
    # pg_basebackup creates its destination with the process umask, and the
    # server refuses to start on anything looser than 0750.
    _exec_on(STANDBY, "chmod", "700", STANDBY_DIR, user="root")

    if taken:
        _exec_on(
            STANDBY,
            "sh",
            "-c",
            "pg_ctl -D " + STANDBY_DIR + " -l /failover/standby.log -w start",
        )
    check("standby_started", _wait_ready(STANDBY), "standby accepts connections")

    in_recovery = _psql_on(STANDBY, "SELECT pg_is_in_recovery()") == "t"
    check("standby_is_in_recovery", in_recovery, "pg_is_in_recovery()=" + str(in_recovery))

    # The timeline is read from the current WAL file name rather than from
    # pg_control_checkpoint(), which reports the *last checkpoint* and therefore
    # still says 1 for a while after a promotion.
    timeline_sql = "SELECT substring(pg_walfile_name(pg_current_wal_lsn()) from 1 for 8)"
    timeline_before = _psql_on(PRIMARY, timeline_sql)
    check(
        "primary_timeline_is_initial",
        timeline_before == "00000001",
        "timeline " + timeline_before,
    )

    streaming = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        streaming = _psql_on(
            PRIMARY, "SELECT state FROM pg_stat_replication WHERE application_name = 'walreceiver'"
        )
        if streaming == "streaming":
            break
        time.sleep(1)
    check("walsender_reports_streaming", streaming == "streaming", "state=" + (streaming or "none"))

    _psql_on(PRIMARY, "INSERT INTO " + MARKER_TABLE + " (id, note) VALUES (2, 'replicated-live')")
    check(
        "live_write_reached_the_standby",
        _wait_for_row(STANDBY, "replicated-live"),
        "replicated-live visible on the standby",
    )

    # The point of the drill.  Not a clean shutdown and not a restart: the
    # primary container is killed, which is what a standby exists for.
    _docker("kill", PRIMARY)
    check(
        "primary_killed",
        not _wait_ready(PRIMARY, timeout=10),
        "primary no longer accepts connections",
    )

    promote = _docker("exec", "-u", "postgres", STANDBY, "pg_ctl", "-D", STANDBY_DIR, "promote")
    check(
        "standby_promote_issued",
        promote.returncode == 0,
        promote.stdout.decode("utf-8", "replace").strip()[:120],
    )

    promoted = False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _psql_on(STANDBY, "SELECT pg_is_in_recovery()") == "f":
            promoted = True
            break
        time.sleep(1)
    check("promoted_node_accepts_writes", promoted, "pg_is_in_recovery()=f after promotion")

    timeline_after = _psql_on(STANDBY, timeline_sql)
    check(
        "promotion_advanced_the_timeline",
        timeline_after != timeline_before,
        "timeline " + timeline_before + " -> " + timeline_after,
    )

    survived = _psql_on(
        STANDBY, "SELECT count(*) FROM " + MARKER_TABLE + " WHERE note = 'before-failover'"
    )
    check("no_data_lost_across_the_failover", survived == "1", "pre-failover rows: " + survived)

    _psql_on(STANDBY, "INSERT INTO " + MARKER_TABLE + " (id, note) VALUES (3, 'after-failover')")
    persisted = _psql_on(
        STANDBY, "SELECT count(*) FROM " + MARKER_TABLE + " WHERE note = 'after-failover'"
    )
    check("promoted_node_takes_new_writes", persisted == "1", "post-failover rows: " + persisted)

    if not keep_containers:
        _teardown()

    failures = [item["name"] for item in checks if not item["ok"]]
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "image": IMAGE,
        "topology": "two containers on " + NETWORK,
        "standby_dir": STANDBY_DIR,
        "replication_slot": SLOT,
        "timeline_before": timeline_before,
        "timeline_after": timeline_after,
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
        "standby_is_in_recovery",
        "walsender_reports_streaming",
        "live_write_reached_the_standby",
        "primary_killed",
        "promoted_node_accepts_writes",
        "promotion_advanced_the_timeline",
        "no_data_lost_across_the_failover",
        "promoted_node_takes_new_writes",
    ):
        if required not in names:
            issues.append("check_missing:" + required)
    if bundle.get("timeline_before") == bundle.get("timeline_after"):
        issues.append("timeline_did_not_advance")
    if bundle.get("failures") not in ([], None):
        issues.append("failures_recorded")
    if bundle.get("passed") is not True:
        issues.append("not_passed")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    parser.add_argument("--keep-containers", action="store_true")
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

    bundle = run(keep_containers=args.keep_containers)
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
    print("timeline : " + str(bundle["timeline_before"]) + " -> " + str(bundle["timeline_after"]))
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
