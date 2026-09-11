"""Fault injection against a real PostgreSQL and the real repository.

The other drills show the system behaves when nothing goes wrong, or when a whole
server disappears on purpose.  This one interrupts the work itself, at the
moments where a half-finished write would be invisible:

  process   the backend serving an open transaction is terminated;
  network   the database is paused underneath a live connection pool;
  storage   the database stops accepting writes;
  crash     a worker leases an outbox operation and never comes back.

Each scenario asserts two things: the failure is surfaced rather than swallowed,
and nothing partial survived it.  A drill that only checked "an error was
raised" would pass while leaving a half-written row behind.

    python scripts/fault_drill.py --json ../../docs/reports/mailhub-fault-drill.json
    python scripts/fault_drill.py --validate ../../docs/reports/mailhub-fault-drill.json

Everything runs in a throwaway container; no real database is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "mailhub-fault-postgres"
IMAGE = "postgres:16.4-alpine"
DB_USER = "mailhub"
DB_PASSWORD = "fault-drill-password"
DB_NAME = "mailhub"
PORT = "55437"
SCHEMA = "mailhub.fault_drill.v1"

TENANT = "fault-tenant"
SUBJECT = "fault-subject"


def _docker(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], capture_output=True, check=False)


def _start_container() -> None:
    _docker("rm", "-f", CONTAINER)
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-p",
        "127.0.0.1:" + PORT + ":5432",
        "-e",
        "POSTGRES_USER=" + DB_USER,
        "-e",
        "POSTGRES_PASSWORD=" + DB_PASSWORD,
        "-e",
        "POSTGRES_DB=" + DB_NAME,
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
    return (
        "postgresql+asyncpg://" + DB_USER + ":" + DB_PASSWORD + "@127.0.0.1:" + PORT + "/" + DB_NAME
    )


def _migrate() -> tuple[int, str]:
    completed = subprocess.run(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "apply_migrations.py"),
            "--database-url",
            _database_url(),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=PACKAGE_ROOT,
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()[-300:]


def _engine() -> Any:
    from sqlalchemy.ext.asyncio import create_async_engine

    # A short timeout and a pre-ping: the network scenario has to notice the
    # database is gone quickly, and recover once it is back.
    return create_async_engine(
        _database_url(),
        pool_size=4,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={"timeout": 8, "command_timeout": 8},
    )


def _autocommit_engine() -> Any:
    """For statements PostgreSQL refuses to run inside a transaction block."""

    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(_database_url(), isolation_level="AUTOCOMMIT")


async def _audit_rows(engine: Any) -> int:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM mail_audit_events WHERE tenant_id = :tenant"),
                    {"tenant": TENANT},
                )
            ).scalar_one()
        )


async def _process_fault(engine: Any) -> dict[str, Any]:
    """Terminate the backend that is holding an open, uncommitted write."""

    from sqlalchemy import text

    before = await _audit_rows(engine)
    surfaced = False
    error = ""
    async with engine.connect() as writer:
        transaction = await writer.begin()
        pid = (await writer.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await writer.execute(
            text(
                "INSERT INTO mail_audit_events"
                " (tenant_id, subject_id, event_type, target_ref, occurred_at, metadata)"
                " VALUES (:tenant, :subject, 'fault.probe', 'fault-probe', now(), '{}'::jsonb)"
            ),
            {"tenant": TENANT, "subject": "fault-subject"},
        )
        async with engine.connect() as killer:
            await killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        try:
            await transaction.commit()
        except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
            surfaced = True
            error = type(exc).__name__
    after = await _audit_rows(engine)
    return {
        "failure_surfaced": surfaced,
        "error": error,
        "rows_before": before,
        "rows_after": after,
    }


async def _network_fault(engine: Any) -> dict[str, Any]:
    """Pause the database underneath a live pool, then bring it back."""

    from sqlalchemy import text

    before = await _audit_rows(engine)
    _docker("pause", CONTAINER)
    surfaced = False
    error = ""
    try:
        async with asyncio.timeout(20):
            async with engine.connect() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO mail_audit_events"
                        " (tenant_id, subject_id, event_type, target_ref, occurred_at, metadata)"
                        " VALUES (:tenant, 'fault-subject', 'fault.probe', 'paused', now(),"
                        " '{}'::jsonb)"
                    ),
                    {"tenant": TENANT},
                )
                await conn.commit()
    except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
        surfaced = True
        error = type(exc).__name__
    finally:
        _docker("unpause", CONTAINER)

    recovered = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            recovered = True
            break
        except Exception:  # noqa: BLE001 - the drill is waiting for recovery
            await asyncio.sleep(2)
    after = await _audit_rows(engine)
    return {
        "failure_surfaced": surfaced,
        "error": error,
        "recovered": recovered,
        "rows_before": before,
        "rows_after": after,
    }


async def _storage_fault(engine: Any) -> dict[str, Any]:
    """Make the database refuse writes, then let it accept them again."""

    from sqlalchemy import text
    from sqlalchemy import text as _text

    async def set_read_only(enabled: bool) -> None:
        control_engine = _autocommit_engine()
        try:
            async with control_engine.connect() as control:
                await control.execute(
                    _text(
                        "ALTER SYSTEM SET default_transaction_read_only = "
                        + ("on" if enabled else "off")
                    )
                )
                await control.execute(_text("SELECT pg_reload_conf()"))
        finally:
            await control_engine.dispose()

    before = await _audit_rows(engine)
    await set_read_only(True)
    surfaced = False
    error = ""
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO mail_audit_events"
                    " (tenant_id, subject_id, event_type, target_ref, occurred_at, metadata)"
                    " VALUES (:tenant, 'fault-subject', 'fault.probe', 'read-only', now(),"
                    " '{}'::jsonb)"
                ),
                {"tenant": TENANT},
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
        surfaced = True
        error = type(exc).__name__
    await set_read_only(False)
    wrote_again = False
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO mail_audit_events"
                    " (tenant_id, subject_id, event_type, target_ref, occurred_at, metadata)"
                    " VALUES (:tenant, 'fault-subject', 'fault.probe', 'restored', now(),"
                    " '{}'::jsonb)"
                ),
                {"tenant": TENANT},
            )
            await conn.commit()
        wrote_again = True
    except Exception:  # noqa: BLE001 - asserted on below
        wrote_again = False
    after = await _audit_rows(engine)
    return {
        "failure_surfaced": surfaced,
        "error": error,
        "writes_restored": wrote_again,
        "rows_before": before,
        "rows_after": after,
    }


async def _crash_fault(engine: Any) -> dict[str, Any]:
    """A worker leases an operation and never comes back."""

    import hashlib

    from mailhub.domain import (
        DeliveryStatus,
        MailboxConnection,
        MailDraft,
        MailOutboxOperation,
        ProviderName,
    )
    from mailhub.persistence.sqlalchemy import SqlAlchemyMailRepository
    from mailhub.storage import RepositoryConflictError

    repository = SqlAlchemyMailRepository(engine)
    # Real rows: the outbox references a draft and a connection by foreign key,
    # so a crash scenario with invented ids would fail on the constraint rather
    # than on anything being tested.
    connection = MailboxConnection(
        connection_id=uuid4(),
        tenant_id=TENANT,
        subject_id=SUBJECT,
        provider=ProviderName.IMAP_SMTP,
        email_address="fault@example.test",
        credential_ref="fault-credential",
    )
    await repository.save_connection(connection)
    body = "fault drill draft"
    draft = MailDraft(
        draft_id=uuid4(),
        tenant_id=TENANT,
        subject_id=SUBJECT,
        connection_id=connection.connection_id,
        thread_id=None,
        recipient_addresses=("fault@example.test",),
        subject="fault drill",
        # The row keeps a reference, not the bytes; the repository enforces it.
        body_text=None,
        body_object_ref="object://fault/draft",
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    await repository.save_draft(draft)
    operation = MailOutboxOperation(
        operation_id=uuid4(),
        tenant_id=TENANT,
        subject_id=SUBJECT,
        draft_id=draft.draft_id,
        connection_id=connection.connection_id,
        idempotency_key="fault-crash-" + str(uuid4()),
        status=DeliveryStatus.QUEUED,
    )
    await repository.create_or_get_operation(operation)
    leased_at = datetime.now(UTC)
    leased = await repository.lease_operation(
        tenant_id=TENANT,
        operation_id=operation.operation_id,
        worker_id="worker-that-dies",
        now=leased_at,
        lease_seconds=10,
    )
    assert leased.lease_owner == "worker-that-dies"

    surfaced = False
    error = ""
    try:
        await repository.lease_operation(
            tenant_id=TENANT,
            operation_id=operation.operation_id,
            worker_id="worker-that-takes-over",
            now=leased_at + timedelta(seconds=11),
            lease_seconds=10,
        )
    except RepositoryConflictError as exc:
        surfaced = True
        error = str(exc)
    recovered = await repository.get_operation(
        tenant_id=TENANT, operation_id=operation.operation_id
    )
    return {
        "failure_surfaced": surfaced,
        "error": error,
        "status_after": recovered.status.value if recovered is not None else "missing",
        "lease_cleared": recovered is not None and recovered.lease_owner is None,
    }


async def _drive() -> dict[str, Any]:
    """Every scenario on one engine, and therefore one event loop."""

    engine = _engine()
    try:
        return {
            "process": await _process_fault(engine),
            "network": await _network_fault(engine),
            "storage": await _storage_fault(engine),
            "crash": await _crash_fault(engine),
        }
    finally:
        await engine.dispose()


def run(*, keep_container: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    _start_container()
    check("container_ready", True, IMAGE)
    code, output = _migrate()
    check("schema_migrated", code == 0, output)

    # One event loop for every scenario: asyncpg connections belong to the loop
    # that opened them, so a fresh asyncio.run per scenario fails on the second.
    scenarios = asyncio.run(_drive())
    process = scenarios["process"]
    network = scenarios["network"]
    storage = scenarios["storage"]
    crash = scenarios["crash"]

    check(
        "terminated_backend_surfaces_the_failure",
        process["failure_surfaced"],
        process["error"],
    )
    check(
        "terminated_backend_leaves_nothing_behind",
        process["rows_after"] == process["rows_before"],
        "rows " + str(process["rows_before"]) + " -> " + str(process["rows_after"]),
    )

    check("paused_database_surfaces_the_failure", network["failure_surfaced"], network["error"])
    check("pool_recovers_after_the_partition", network["recovered"], "a query succeeded again")
    check(
        "paused_write_left_nothing_behind",
        network["rows_after"] == network["rows_before"],
        "rows " + str(network["rows_before"]) + " -> " + str(network["rows_after"]),
    )

    check("read_only_database_refuses_writes", storage["failure_surfaced"], storage["error"])
    check("writes_resume_once_storage_recovers", storage["writes_restored"], "a write landed")
    check(
        "refused_write_left_nothing_behind",
        storage["rows_after"] == storage["rows_before"] + 1,
        "rows "
        + str(storage["rows_before"])
        + " -> "
        + str(storage["rows_after"])
        + " (only the restored write may have landed)",
    )

    check(
        "crashed_worker_blocks_the_next_lease",
        crash["failure_surfaced"],
        crash["error"][:120],
    )
    check(
        "crashed_worker_leaves_an_unknown_outcome",
        crash["status_after"] == "outcome_unknown",
        "status=" + crash["status_after"],
    )
    check("crashed_worker_releases_its_lease", crash["lease_cleared"], "lease owner cleared")

    if not keep_container:
        _docker("rm", "-f", CONTAINER)

    failures = [item["name"] for item in checks if not item["ok"]]
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "image": IMAGE,
        "scenarios": {
            "process": process,
            "network": network,
            "storage": storage,
            "crash": crash,
        },
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
        "terminated_backend_surfaces_the_failure",
        "terminated_backend_leaves_nothing_behind",
        "paused_database_surfaces_the_failure",
        "pool_recovers_after_the_partition",
        "read_only_database_refuses_writes",
        "writes_resume_once_storage_recovers",
        "crashed_worker_leaves_an_unknown_outcome",
    ):
        if required not in names:
            issues.append("check_missing:" + required)
    scenarios = bundle.get("scenarios")
    if not isinstance(scenarios, Mapping):
        issues.append("scenarios_missing")
    else:
        for name in ("process", "network", "storage", "crash"):
            entry = scenarios.get(name)
            if not isinstance(entry, Mapping):
                issues.append("scenario_missing:" + name)
            elif entry.get("failure_surfaced") is not True:
                issues.append("scenario_did_not_surface_a_failure:" + name)
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
