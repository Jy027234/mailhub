"""Capacity drill against a real PostgreSQL through the real repository.

A benchmark on a toy table would measure PostgreSQL.  This drives
SqlAlchemyMailRepository -- the same object the service uses -- against the
migrated schema, so what is measured is the write path, the read path, the RLS
setup per transaction and the unique index that makes re-ingestion idempotent.

    python scripts/capacity_drill.py --json ../../docs/reports/mailhub-capacity-drill.json
    python scripts/capacity_drill.py --validate ../../docs/reports/mailhub-capacity-drill.json

The thresholds are deliberately modest.  The value of the bundle is the recorded
baseline plus the invariants, not a fast number: a run that loses rows, lets one
tenant see another's, or duplicates a re-ingested message fails regardless of how
quickly it did so.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import quantiles
from typing import Any
from uuid import uuid4

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "mailhub-capacity-postgres"
IMAGE = "postgres:16.4-alpine"
DB_USER = "mailhub"
DB_PASSWORD = "capacity-drill-password"
DB_NAME = "mailhub"
PORT = "55436"
SCHEMA = "mailhub.capacity_drill.v1"

TENANT = "capacity-tenant"
SUBJECT = "capacity-subject"
OTHER_TENANT = "capacity-other-tenant"
OTHER_SUBJECT = "capacity-other-subject"

THREADS = 40
MESSAGES_PER_THREAD = 25
CONCURRENCY = 8
READ_ITERATIONS = 60
READ_LIMIT = 50

#: A floor, not a target.  It exists so a future run can regress against
#: something recorded instead of against a feeling.
MIN_INGEST_PER_SECOND = 50.0
MAX_P95_MILLISECONDS = 250.0


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


def _percentiles(samples: Sequence[float]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    ordered = sorted(samples)
    if len(ordered) < 3:
        return {"p50": ordered[0], "p95": ordered[-1], "p99": ordered[-1]}
    cuts = quantiles(ordered, n=100, method="inclusive")
    return {
        "p50": round(cuts[49], 3),
        "p95": round(cuts[94], 3),
        "p99": round(cuts[98], 3),
    }


def _connection_values(tenant: str, subject: str, email: str) -> Any:
    from mailhub.domain import MailboxConnection, ProviderName

    return MailboxConnection(
        connection_id=uuid4(),
        tenant_id=tenant,
        subject_id=subject,
        provider=ProviderName.IMAP_SMTP,
        email_address=email,
        credential_ref="capacity-credential",
    )


def _thread(tenant: str, connection_id: Any, index: int) -> Any:
    from mailhub.domain import MailThread

    return MailThread(
        thread_id=uuid4(),
        tenant_id=tenant,
        connection_id=connection_id,
        provider_thread_ref="capacity-thread-" + str(index),
        normalized_subject="capacity subject " + str(index),
        participant_addresses=("buyer@example.test",),
        latest_at=datetime.now(UTC),
        message_count=MESSAGES_PER_THREAD,
    )


def _message(tenant: str, connection_id: Any, thread_id: Any, ref: str, when: Any) -> Any:
    from mailhub.domain import MailMessageProjection

    return MailMessageProjection(
        message_id=uuid5_ref(ref),
        tenant_id=tenant,
        connection_id=connection_id,
        thread_id=thread_id,
        provider_message_ref=ref,
        internet_message_id="<" + ref + "@example.test>",
        sender_address="buyer@example.test",
        recipient_addresses=("capacity@example.test",),
        subject="capacity load",
        received_at=when,
        # Metadata-only: the body lives in the object store and the row keeps
        # a reference, which is also how the product ingests by default.
        body_text=None,
        body_object_ref="object://capacity/" + ref,
        # A deterministic digest per reference: when the body lives in the object
        # store the row only has to carry a well-formed one.
        content_sha256=hashlib.sha256(ref.encode("utf-8")).hexdigest(),
    )


def uuid5_ref(ref: str) -> Any:
    from uuid import NAMESPACE_URL, uuid5

    return uuid5(NAMESPACE_URL, ref)


async def _seed(repository: Any) -> dict[str, Any]:
    connection = _connection_values(TENANT, SUBJECT, "capacity@example.test")
    other = _connection_values(OTHER_TENANT, OTHER_SUBJECT, "other@example.test")
    await repository.save_connection(connection)
    await repository.save_connection(other)

    threads = [_thread(TENANT, connection.connection_id, index) for index in range(THREADS)]
    for thread in threads:
        await repository.save_thread(thread)

    started = time.perf_counter()
    latencies: list[float] = []
    errors: list[str] = []
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def write(thread: Any, index: int) -> None:
        ref = "capacity-" + str(thread.provider_thread_ref) + "-" + str(index)
        message = _message(
            TENANT,
            connection.connection_id,
            thread.thread_id,
            ref,
            datetime.now(UTC) - timedelta(seconds=index),
        )
        async with semaphore:
            call_started = time.perf_counter()
            try:
                await repository.save_message(message)
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
                errors.append(type(exc).__name__ + ": " + str(exc))
                return
            latencies.append((time.perf_counter() - call_started) * 1000)

    await asyncio.gather(
        *(write(thread, index) for thread in threads for index in range(MESSAGES_PER_THREAD))
    )
    elapsed = time.perf_counter() - started
    expected = THREADS * MESSAGES_PER_THREAD
    return {
        "connection_id": connection.connection_id,
        "other_connection_id": other.connection_id,
        "threads": THREADS,
        "messages": expected,
        "elapsed_seconds": round(elapsed, 3),
        "ingest_per_second": round(expected / elapsed, 1) if elapsed else 0.0,
        "write_latency_ms": _percentiles(latencies),
        "errors": errors,
    }


async def _measure(repository: Any, connection_id: Any) -> dict[str, Any]:
    read_latency: list[float] = []
    listed = 0
    for _ in range(READ_ITERATIONS):
        started = time.perf_counter()
        page = await repository.list_messages(
            tenant_id=TENANT, subject_id=SUBJECT, limit=READ_LIMIT
        )
        read_latency.append((time.perf_counter() - started) * 1000)
        listed = len(page)

    thread_latency: list[float] = []
    for _ in range(READ_ITERATIONS):
        started = time.perf_counter()
        await repository.list_threads(tenant_id=TENANT, subject_id=SUBJECT, limit=READ_LIMIT)
        thread_latency.append((time.perf_counter() - started) * 1000)

    foreign = await repository.list_messages(
        tenant_id=OTHER_TENANT, subject_id=OTHER_SUBJECT, limit=READ_LIMIT
    )

    # Re-ingesting a message that is already stored must not duplicate it: the
    # unique index on (connection_id, provider_message_ref) is what makes a
    # retried sync safe, so it is exercised rather than assumed.
    first_thread = (await repository.list_threads(tenant_id=TENANT, subject_id=SUBJECT, limit=1))[0]
    duplicate = _message(
        TENANT,
        connection_id,
        first_thread.thread_id,
        "capacity-" + first_thread.provider_thread_ref + "-0",
        datetime.now(UTC),
    )
    _saved, created = await repository.save_message(duplicate)

    return {
        "read_latency_ms": _percentiles(read_latency),
        "thread_read_latency_ms": _percentiles(thread_latency),
        "listed_per_read": listed,
        "foreign_tenant_rows": len(foreign),
        "reingest_created_a_new_row": bool(created),
    }
    return {
        "read_latency_ms": _percentiles(read_latency),
        "thread_read_latency_ms": _percentiles(thread_latency),
        "listed_per_read": listed,
        "foreign_tenant_rows": len(foreign),
        "reingest_created_a_new_row": bool(created),
    }


async def _connection_id(repository: Any) -> Any:
    connections = await repository.list_connections(tenant_id=TENANT, subject_id=SUBJECT)
    return connections[0].connection_id


async def _drive() -> dict[str, Any]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from mailhub.persistence.sqlalchemy import SqlAlchemyMailRepository

    engine = create_async_engine(_database_url(), pool_size=CONCURRENCY * 2, max_overflow=4)
    repository = SqlAlchemyMailRepository(engine)
    try:
        ingest = await _seed(repository)
        reads = await _measure(repository, ingest["connection_id"])
        # Count what is actually in the table.  Asserting the arithmetic of the
        # workload would prove nothing about whether the writes landed.
        async with engine.connect() as conn:
            counted = (
                await conn.execute(
                    text("SELECT count(*) FROM mail_messages WHERE tenant_id = :tenant"),
                    {"tenant": TENANT},
                )
            ).scalar_one()
        reads["rows_stored"] = int(counted)
    finally:
        await engine.dispose()
    # The connection ids are internal plumbing; a bundle of measurements should
    # not carry identifiers that only mean something inside one run.
    measured = {
        key: value
        for key, value in ingest.items()
        if key not in {"connection_id", "other_connection_id"}
    }
    measured.update(reads)
    return measured


def run(*, keep_container: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    _start_container()
    check("container_ready", True, IMAGE)
    code, output = _migrate()
    check("schema_migrated", code == 0, output)

    measured = asyncio.run(_drive())
    expected = THREADS * MESSAGES_PER_THREAD

    check(
        "no_write_errors_under_concurrency",
        not measured["errors"],
        str(len(measured["errors"])) + " errors: " + "; ".join(measured["errors"][:2]),
    )
    check(
        "ingest_rate_meets_the_recorded_floor",
        measured["ingest_per_second"] >= MIN_INGEST_PER_SECOND,
        str(measured["ingest_per_second"]) + " msg/s (floor " + str(MIN_INGEST_PER_SECOND) + ")",
    )
    check(
        "read_p95_within_the_recorded_ceiling",
        measured["read_latency_ms"]["p95"] <= MAX_P95_MILLISECONDS,
        "p95 "
        + str(measured["read_latency_ms"]["p95"])
        + " ms (ceiling "
        + str(MAX_P95_MILLISECONDS)
        + ")",
    )
    check(
        "reads_return_the_configured_page",
        measured["listed_per_read"] == READ_LIMIT,
        "listed " + str(measured["listed_per_read"]) + " of limit " + str(READ_LIMIT),
    )
    check(
        "reingesting_the_same_message_does_not_duplicate",
        measured["reingest_created_a_new_row"] is False,
        "created=" + str(measured["reingest_created_a_new_row"]),
    )
    check(
        "another_tenant_sees_nothing",
        measured["foreign_tenant_rows"] == 0,
        str(measured["foreign_tenant_rows"]) + " rows visible to the other tenant",
    )
    check(
        "every_seeded_message_is_stored",
        measured["rows_stored"] == expected,
        str(measured["rows_stored"]) + " rows stored, expected " + str(expected),
    )

    if not keep_container:
        _docker("rm", "-f", CONTAINER)

    failures = [item["name"] for item in checks if not item["ok"]]
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "image": IMAGE,
        "workload": {
            "threads": THREADS,
            "messages": expected,
            "concurrency": CONCURRENCY,
            "read_iterations": READ_ITERATIONS,
            "read_limit": READ_LIMIT,
        },
        "thresholds": {
            "min_ingest_per_second": MIN_INGEST_PER_SECOND,
            "max_p95_milliseconds": MAX_P95_MILLISECONDS,
        },
        "measured": measured,
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
        "no_write_errors_under_concurrency",
        "ingest_rate_meets_the_recorded_floor",
        "read_p95_within_the_recorded_ceiling",
        "reingesting_the_same_message_does_not_duplicate",
        "another_tenant_sees_nothing",
        "every_seeded_message_is_stored",
    ):
        if required not in names:
            issues.append("check_missing:" + required)
    measured = bundle.get("measured")
    if not isinstance(measured, Mapping):
        issues.append("measurements_missing")
    else:
        for field in ("ingest_per_second", "read_latency_ms", "write_latency_ms", "rows_stored"):
            if field not in measured:
                issues.append("measurement_missing:" + field)
        if int(measured.get("foreign_tenant_rows") or 0) != 0:
            issues.append("tenant_isolation_violated")
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
    print("ingest   : " + str(bundle["measured"]["ingest_per_second"]) + " msg/s")
    print("read p95 : " + str(bundle["measured"]["read_latency_ms"]["p95"]) + " ms")
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
