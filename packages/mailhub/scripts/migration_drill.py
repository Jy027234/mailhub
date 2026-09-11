"""Shadow, cutover and rollback drill for the generic migration kit.

MAIL-ADOPT-011's acceptance is a full move off a legacy mail module.  There is no
second product in this repository to move off, so the drill drives the kit
against a deliberately small **fixture** legacy module of a different stack: its
own SQLite file, its own cursor vocabulary, its own sender.

That distinction is carried in the evidence rather than hidden: the bundle names
the legacy side as a fixture and records that a real adopter's cutover is still
the operator's step.  What the drill does prove is that the kit's parts work
together -- a shadow comparison that can fail, a cursor takeover that refuses to
overwrite, a sender interlock that admits exactly one side, and a rollback that
gives authority back.

    python scripts/migration_drill.py --json ../../docs/reports/mailhub-migration-drill.json
    python scripts/migration_drill.py --validate ../../docs/reports/mailhub-migration-drill.json

The legacy fixture lives in a temporary directory and is never a customer system.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "mailhub-migration-postgres"
IMAGE = "postgres:16.4-alpine"
DB_USER = "mailhub"
DB_PASSWORD = "migration-drill-password"
DB_NAME = "mailhub"
PORT = "55438"
SCHEMA = "mailhub.migration_drill.v1"

TENANT = "migration-tenant"
SUBJECT = "migration-subject"
LEGACY_SYSTEM = "fixture-legacy-mail"
LEGACY_OWNER = "legacy-mail-sender"
MAILHUB_OWNER = "mailhub-sender"
ACCOUNT = "ops@example.test"
MESSAGES = 12

LEGACY_SCHEMA = """
CREATE TABLE legacy_messages (
    legacy_id TEXT PRIMARY KEY,
    thread_ref TEXT NOT NULL,
    subject TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    cursor_value TEXT NOT NULL
);
CREATE TABLE legacy_send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note TEXT NOT NULL
);
"""


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


# -- the fixture legacy module --------------------------------------------


class FixtureLegacyMail:
    """A legacy mail module of a different stack, for the drill only.

    It has its own storage (SQLite), its own cursor vocabulary (page numbers
    rather than UIDVALIDITY/UID) and its own sender.  It is not a product and
    the evidence says so; it exists so the kit is exercised against something
    that is genuinely not MailHub.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        connection = sqlite3.connect(self.path)
        connection.executescript(LEGACY_SCHEMA)
        connection.commit()
        connection.close()

    def seed(self, count: int) -> list[dict[str, str]]:
        connection = sqlite3.connect(self.path)
        rows: list[dict[str, str]] = []
        for index in range(count):
            body = "legacy body " + str(index)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            row = {
                "legacy_id": "legacy-" + str(index),
                "thread_ref": "legacy-thread-" + str(index),
                "subject": "legacy subject " + str(index),
                "content_sha256": digest,
                "cursor_value": "page:" + str(index + 1),
            }
            connection.execute(
                "INSERT INTO legacy_messages VALUES (?, ?, ?, ?, ?)",
                (
                    row["legacy_id"],
                    row["thread_ref"],
                    row["subject"],
                    row["content_sha256"],
                    row["cursor_value"],
                ),
            )
            rows.append(row)
        connection.commit()
        connection.close()
        return rows

    def last_cursor(self) -> str:
        connection = sqlite3.connect(self.path)
        try:
            value = connection.execute(
                "SELECT cursor_value FROM legacy_messages ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            return str(value[0]) if value else ""
        finally:
            connection.close()

    async def send_if_authorised(
        self, interlock: Any, *, tenant_id: str, account_ref: str, owner: str, note: str
    ) -> bool:
        """Send only while holding the shared lease, as a migrated system must.

        A fixture that logged unconditionally would make the "no dual send"
        check meaningless: it would record an attempt whatever the interlock
        said, and the check would pass while nothing stopped the send.
        """

        if not await interlock.claim(tenant_id=tenant_id, account_ref=account_ref, owner=owner):
            return False
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("INSERT INTO legacy_send_log (note) VALUES (?)", (note,))
            connection.commit()
        finally:
            connection.close()
        await interlock.release(tenant_id=tenant_id, account_ref=account_ref, owner=owner)
        return True

    def sent_count(self) -> int:
        connection = sqlite3.connect(self.path)
        try:
            return int(connection.execute("SELECT count(*) FROM legacy_send_log").fetchone()[0])
        finally:
            connection.close()


def _translate_cursor(legacy_cursor: str, *, uidvalidity: int) -> str:
    """The drill's own translation, documented rather than assumed.

    A page number is not a UID.  This fixture pretends one page holds a fixed
    number of messages so the arithmetic is explicit; a real host would use its
    own mapping, which is exactly why the kit refuses to guess one.
    """

    page = int(legacy_cursor.split(":")[1])
    return str(uidvalidity) + ":" + str(page * 25)


def _legacy_facts(row: Mapping[str, str]) -> dict[str, object]:
    return {
        "identity": row["legacy_id"],
        "cursor": row["cursor_value"],
        "content_sha256": row["content_sha256"],
        "attachments": 0,
        "business_link": row["thread_ref"],
        "failure_state": "ok",
    }


def _mailhub_facts(row: Mapping[str, str]) -> dict[str, object]:
    return {
        "identity": row["legacy_id"],
        "cursor": row["cursor_value"],
        "content_sha256": row["content_sha256"],
        "attachments": 0,
        "business_link": row["thread_ref"],
        "failure_state": "ok",
    }


async def _run(*, keep_container: bool) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from mailhub.domain import (
        MailboxConnection,
        MailMessageProjection,
        MailThread,
        ProviderName,
    )
    from mailhub.hosts.migration import (
        MigrationController,
        MigrationFeatureFlags,
        NoDualSenderInterlock,
        compare_shadow,
    )
    from mailhub.persistence.sqlalchemy import SqlAlchemyMailRepository
    from mailhub.storage import RepositoryConflictError

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    _start_container()
    check("container_ready", True, IMAGE)
    code, output = _migrate()
    check("schema_migrated", code == 0, output)

    with tempfile.TemporaryDirectory(prefix="mailhub-legacy-", ignore_cleanup_errors=True) as work:
        legacy = FixtureLegacyMail(Path(work) / "legacy.db")
        rows = legacy.seed(MESSAGES)

        engine = create_async_engine(_database_url())
        try:
            repository = SqlAlchemyMailRepository(engine)
            connection = MailboxConnection(
                connection_id=uuid4(),
                tenant_id=TENANT,
                subject_id=SUBJECT,
                provider=ProviderName.IMAP_SMTP,
                email_address=ACCOUNT,
                credential_ref="migration-credential",
            )
            await repository.save_connection(connection)

            # -- shadow read --------------------------------------------------
            comparisons = [
                compare_shadow(
                    tenant_id=TENANT,
                    legacy_system=LEGACY_SYSTEM,
                    legacy_ref=row["legacy_id"],
                    mailhub_ref=row["legacy_id"],
                    legacy=_legacy_facts(row),
                    mailhub=_mailhub_facts(row),
                )
                for row in rows
            ]
            check(
                "shadow_comparison_is_authority_safe",
                all(item.authority_safe for item in comparisons),
                str(len(comparisons)) + " facts compared",
            )
            divergence = compare_shadow(
                tenant_id=TENANT,
                legacy_system=LEGACY_SYSTEM,
                legacy_ref=rows[0]["legacy_id"],
                mailhub_ref=rows[0]["legacy_id"],
                legacy=_legacy_facts(rows[0]),
                mailhub={**_mailhub_facts(rows[0]), "content_sha256": "0" * 64},
            )
            check(
                "shadow_comparison_can_fail",
                not divergence.authority_safe and not divergence.content_hash_match,
                "a changed digest is not authority safe",
            )

            # -- ingest the same facts into MailHub ---------------------------
            threads: dict[str, Any] = {}
            for row in rows:
                thread = MailThread(
                    thread_id=uuid4(),
                    tenant_id=TENANT,
                    connection_id=connection.connection_id,
                    provider_thread_ref=row["thread_ref"],
                    normalized_subject=row["subject"],
                    participant_addresses=("buyer@example.test",),
                    latest_at=datetime.now(UTC),
                )
                await repository.save_thread(thread)
                threads[row["legacy_id"]] = thread
                await repository.save_message(
                    MailMessageProjection(
                        message_id=uuid4(),
                        tenant_id=TENANT,
                        connection_id=connection.connection_id,
                        thread_id=thread.thread_id,
                        provider_message_ref=row["legacy_id"],
                        internet_message_id="<" + row["legacy_id"] + "@example.test>",
                        sender_address="buyer@example.test",
                        recipient_addresses=(ACCOUNT,),
                        subject=row["subject"],
                        received_at=datetime.now(UTC),
                        body_text=None,
                        body_object_ref="object://migration/" + row["legacy_id"],
                        content_sha256=row["content_sha256"],
                    )
                )
            stored = await repository.list_messages(tenant_id=TENANT, subject_id=SUBJECT, limit=100)
            check(
                "every_legacy_message_reached_mailhub",
                len(stored) == MESSAGES,
                str(len(stored)) + " of " + str(MESSAGES),
            )

            # -- cutover ------------------------------------------------------
            interlock = NoDualSenderInterlock()
            controller = MigrationController(interlock=interlock)
            batch = await controller.start(
                tenant_id=TENANT,
                legacy_system=LEGACY_SYSTEM,
                account_ref=ACCOUNT,
                owner=MAILHUB_OWNER,
                flags=MigrationFeatureFlags(
                    shadow_read=True,
                    mailhub_read_authority=True,
                    mailhub_send_authority=True,
                ),
            )
            takeover = await controller.take_over_cursor(
                batch_id=batch.batch_id,
                cursors=repository,
                connection_id=connection.connection_id,
                folder_ref="INBOX",
                legacy_cursor=legacy.last_cursor(),
                adopted_cursor=_translate_cursor(legacy.last_cursor(), uidvalidity=1789113608),
            )
            adopted = await repository.get_cursor(
                tenant_id=TENANT, connection_id=connection.connection_id, folder_ref="INBOX"
            )
            check(
                "cursor_taken_over_from_the_legacy_position",
                adopted == takeover.adopted_cursor == "1789113608:300",
                "legacy " + takeover.legacy_cursor + " -> mailhub " + str(adopted),
            )

            refused = False
            try:
                await controller.take_over_cursor(
                    batch_id=batch.batch_id,
                    cursors=repository,
                    connection_id=connection.connection_id,
                    folder_ref="INBOX",
                    legacy_cursor="page:99",
                    adopted_cursor="1789113608:999",
                )
            except RepositoryConflictError as exc:
                refused = str(exc) == "cursor_conflict"
            still = await repository.get_cursor(
                tenant_id=TENANT, connection_id=connection.connection_id, folder_ref="INBOX"
            )
            check(
                "a_second_takeover_is_refused",
                refused and still == "1789113608:300",
                "cursor still " + str(still),
            )

            claimed = await controller.claim_send_authority(batch_id=batch.batch_id)
            check("mailhub_claimed_send_authority", claimed, "the batch holds the lease")
            legacy_sent = await legacy.send_if_authorised(
                interlock,
                tenant_id=TENANT,
                account_ref=ACCOUNT,
                owner=LEGACY_OWNER,
                note="attempt-during-cutover",
            )
            check(
                "legacy_sender_is_refused_during_cutover",
                not legacy_sent,
                "the legacy sender could not take the lease",
            )
            check(
                "no_message_left_the_legacy_sender_during_cutover",
                legacy.sent_count() == 0,
                str(legacy.sent_count()) + " messages sent by the legacy side",
            )

            # -- rollback -----------------------------------------------------
            rolled_back = await controller.rollback(
                batch_id=batch.batch_id, reason="shadow_mismatch_found_in_review"
            )
            check(
                "rollback_is_recorded",
                rolled_back.state == "rolled_back"
                and any(
                    event.event_type == "mail.migration.rollback_completed"
                    and event.reason == "shadow_mismatch_found_in_review"
                    for event in controller.events
                ),
                "state=" + rolled_back.state,
            )
            mailhub_blocked = False
            try:
                await controller.claim_send_authority(batch_id=batch.batch_id)
            except ValueError as exc:
                mailhub_blocked = str(exc) == "migration_batch_not_claimable"
            check(
                "mailhub_cannot_send_after_rollback",
                mailhub_blocked,
                "claiming authority on a rolled back batch is refused",
            )
            legacy_again = await legacy.send_if_authorised(
                interlock,
                tenant_id=TENANT,
                account_ref=ACCOUNT,
                owner=LEGACY_OWNER,
                note="sent-after-rollback",
            )
            check(
                "legacy_sender_works_again_after_rollback",
                legacy_again and legacy.sent_count() == 1,
                "the lease is free again and a message went out",
            )
            preserved = await repository.get_cursor(
                tenant_id=TENANT, connection_id=connection.connection_id, folder_ref="INBOX"
            )
            check(
                "rollback_does_not_silently_rewrite_the_cursor",
                preserved == "1789113608:300",
                "cursor left at " + str(preserved) + " for the operator to decide",
            )
        finally:
            await engine.dispose()

    if not keep_container:
        _docker("rm", "-f", CONTAINER)

    failures = [item["name"] for item in checks if not item["ok"]]
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "mailhub_repository": "postgresql (migrated schema)",
        "legacy_module": "fixture (explicitly labelled, not a customer system)",
        "legacy_stack": "sqlite, page-number cursors, own sender log",
        "legacy_system": LEGACY_SYSTEM,
        "account_ref": ACCOUNT,
        "checks": checks,
        "failures": failures,
        "passed": not failures,
        "remaining": (
            "a shadow -> cutover -> rollback drill on a real second product's mail "
            "module is the operator's step; this drill proves the kit, not a "
            "customer migration"
        ),
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
        "shadow_comparison_is_authority_safe",
        "shadow_comparison_can_fail",
        "cursor_taken_over_from_the_legacy_position",
        "a_second_takeover_is_refused",
        "mailhub_claimed_send_authority",
        "legacy_sender_is_refused_during_cutover",
        "no_message_left_the_legacy_sender_during_cutover",
        "rollback_is_recorded",
        "mailhub_cannot_send_after_rollback",
        "legacy_sender_works_again_after_rollback",
    ):
        if required not in names:
            issues.append("check_missing:" + required)
    legacy = str(bundle.get("legacy_module", ""))
    if "fixture" not in legacy:
        # The bundle must not be readable as a customer migration.
        issues.append("legacy_module_not_labelled_as_a_fixture")
    if not str(bundle.get("remaining", "")).strip():
        issues.append("remaining_work_not_recorded")
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

    bundle = asyncio.run(_run(keep_container=args.keep_container))
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
