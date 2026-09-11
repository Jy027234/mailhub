"""Live CONDSTORE negotiation evidence for the IMAP connector.

The unit tests prove the decision logic against a fake session; this probe
proves it against a server that is actually running.  It answers the one
question a fake cannot: does the server really accept the MODSEQ search key the
connector chose?

It is read-only with respect to mail content.  The only write is an optional
single probe message (--append-probe-message), and nothing is deleted or moved.

    python scripts/imap_condstore_live_probe.py --host 127.0.0.1 --port 10993
        --username user@example.test --password secret
        --ca-file /tmp/mh-dovecot/conf/ca.pem --append-probe-message
        --label dovecot --json docs/reports/mailhub-imap-condstore-dovecot.json

The decisive check is connector_cursor_carries_modseq: the connector emits a
three-field cursor only when CONDSTORE was in effect for the session.  A server
that merely advertises ENABLE (Dovecot 2.4 does) used to yield a two-field
cursor and silently pinned every sync to UID-only pagination.
"""

from __future__ import annotations

import argparse
import asyncio
import imaplib
import json
import os
import ssl
import sys
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from mailhub.domain import MailboxConnection

PROBE_SUBJECT = "mailhub-condstore-probe"
PROBE_BODY = "MailHub CONDSTORE negotiation probe; safe to delete."


class Evidence:
    def __init__(self) -> None:
        self.checks: list[dict[str, object]] = []

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def failures(self) -> list[str]:
        return [str(item["name"]) for item in self.checks if not item["ok"]]


def _context(ca_file: Path | None) -> ssl.SSLContext:
    if ca_file is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=str(ca_file))


def _first(payload: object) -> str:
    if not isinstance(payload, (list, tuple)) or not payload:
        return ""
    value = payload[0]
    if isinstance(value, bytes):
        return value.decode("ascii", "ignore").strip()
    if value is None:
        return ""
    return str(value).strip()


def _capabilities(args: argparse.Namespace) -> tuple[str, ...]:
    client = imaplib.IMAP4_SSL(args.host, args.port, ssl_context=_context(args.ca_file))
    try:
        client.login(args.username, args.password)
        status, data = client.capability()
        if status != "OK":
            return ()
        names: list[str] = []
        for item in data or []:
            if isinstance(item, bytes):
                names.extend(item.decode("ascii", "ignore").upper().split())
        return tuple(names)
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            client.logout()


def _enable_probe(args: argparse.Namespace) -> tuple[bool, bool, str]:
    """Return (enable_advertised, enable_accepted, highest_modseq)."""

    client = imaplib.IMAP4_SSL(args.host, args.port, ssl_context=_context(args.ca_file))
    try:
        client.login(args.username, args.password)
        status, data = client.capability()
        caps: list[str] = []
        for item in data or []:
            if isinstance(item, bytes):
                caps.extend(item.decode("ascii", "ignore").upper().split())
        if "ENABLE" not in caps:
            return False, False, ""
        try:
            enable_status, _ = client.enable("CONDSTORE")
        except imaplib.IMAP4.error:
            return True, False, ""
        if enable_status != "OK":
            return True, False, ""
        select_status, _ = client.select(args.folder, readonly=True)
        if select_status != "OK":
            return True, False, ""
        _code, payload = client.response("HIGHESTMODSEQ")
        return True, True, _first(payload)
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            client.logout()


def _append_probe_message(args: argparse.Namespace) -> str:
    message = EmailMessage()
    message["From"] = args.username
    message["To"] = args.username
    message["Subject"] = PROBE_SUBJECT
    message.set_content(PROBE_BODY)
    client = imaplib.IMAP4_SSL(args.host, args.port, ssl_context=_context(args.ca_file))
    try:
        client.login(args.username, args.password)
        status, data = client.append(args.folder, None, None, message.as_bytes())
        return status + " " + _first(data)
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            client.logout()


def _connection(args: argparse.Namespace) -> MailboxConnection:
    from mailhub.domain import MailboxConnection, ProviderName

    return MailboxConnection(
        connection_id=uuid4(),
        tenant_id="probe-tenant",
        subject_id="probe-subject",
        provider=ProviderName.IMAP_SMTP,
        email_address=args.username,
        credential_ref="probe-credential",
    )


def _sync(args: argparse.Namespace, cursor: str | None) -> str:
    from mailhub.connectors.imap_smtp import ImapSmtpConnector

    connector = ImapSmtpConnector(
        imap_host=args.host,
        smtp_host="unused.example.test",
        imap_port=args.port,
        folder=args.folder,
        timeout_seconds=args.timeout,
    )
    page = asyncio.run(
        connector.sync(
            _connection(args),
            cursor=cursor,
            limit=args.limit,
            credential={"username": args.username, "password": args.password},
        )
    )
    return str(page.next_cursor)


def validate_bundle(bundle: Mapping[str, Any]) -> list[str]:
    """Offline re-check of a recorded probe run.

    A bundle is only worth keeping if a reviewer can re-derive the verdict
    without a server, so the checks listed inside it are re-evaluated here
    rather than trusted.  A bundle that claims a three-field cursor while its own
    recorded checks say otherwise is rejected.
    """

    issues: list[str] = []
    if bundle.get("kind") != "mailhub.imap.condstore_live_probe":
        return ["kind_mismatch"]
    checks = bundle.get("checks")
    if not isinstance(checks, list) or not checks:
        return ["checks_missing"]
    names: set[str] = set()
    for entry in checks:
        if not isinstance(entry, Mapping):
            issues.append("check_not_an_object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            issues.append("check_without_a_name")
            continue
        names.add(name)
        if entry.get("ok") is not True:
            issues.append("check_failed:" + name)
    if "capability_retrieved" not in names:
        issues.append("capability_check_missing")
    if "connector_cursor_carries_modseq" not in names:
        issues.append("cursor_check_missing")
    first_cursor = bundle.get("first_cursor")
    if not isinstance(first_cursor, str) or len(first_cursor.split(":")) != 3:
        issues.append("first_cursor_is_not_a_modseq_cursor")
    second_cursor = bundle.get("second_cursor")
    if (
        isinstance(second_cursor, str)
        and second_cursor.split(":")
        and len(second_cursor.split(":")) != 3
    ):
        issues.append("second_cursor_is_not_a_modseq_cursor")
    if bundle.get("failures") not in ([], None):
        issues.append("failures_recorded")
    if bundle.get("passed") is not True:
        issues.append("not_passed")
    capabilities = bundle.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        issues.append("capabilities_missing")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Not "required": --validate must be usable without a server to point at.
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--ca-file", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--append-probe-message", action="store_true")
    parser.add_argument("--label", default="unknown")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--validate",
        type=Path,
        default=None,
        help="re-check a recorded bundle offline and exit",
    )
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

    if not args.host or not args.username or not args.password:
        print("--host, --username and --password are required to probe a server")
        return 2
    if args.ca_file is not None:
        os.environ["SSL_CERT_FILE"] = str(args.ca_file)

    evidence = Evidence()
    capabilities = _capabilities(args)
    evidence.check("capability_retrieved", bool(capabilities), " ".join(capabilities))
    evidence.check(
        "condstore_advertised",
        "CONDSTORE" in capabilities or "QRESYNC" in capabilities,
        "informational: a server may implement CONDSTORE without advertising it",
    )

    append_status = ""
    if args.append_probe_message:
        try:
            append_status = _append_probe_message(args)
            evidence.check("probe_message_appended", append_status.startswith("OK"), append_status)
        except (imaplib.IMAP4.error, OSError) as error:
            evidence.check("probe_message_appended", False, str(error))

    enable_advertised, enable_accepted, highest_modseq = _enable_probe(args)
    evidence.check("enable_advertised", enable_advertised, "")
    evidence.check(
        "enable_condstore_accepted",
        enable_accepted,
        "HIGHESTMODSEQ=" + highest_modseq if highest_modseq else "no HIGHESTMODSEQ",
    )

    first_cursor = _sync(args, None)
    fields = first_cursor.split(":")
    evidence.check("connector_cursor_carries_modseq", len(fields) == 3, first_cursor)

    second_cursor = ""
    try:
        second_cursor = _sync(args, first_cursor)
        evidence.check("connector_modseq_search_accepted", True, second_cursor)
    except Exception as error:  # noqa: BLE001 - the probe reports any failure verbatim
        evidence.check(
            "connector_modseq_search_accepted",
            False,
            type(error).__name__ + ": " + str(error),
        )

    if second_cursor:
        first_modseq = int(fields[2]) if len(fields) == 3 else -1
        second_fields = second_cursor.split(":")
        second_modseq = int(second_fields[2]) if len(second_fields) == 3 else -1
        evidence.check(
            "modseq_never_regresses",
            first_modseq >= 0 and second_modseq >= first_modseq,
            first_cursor + " -> " + second_cursor,
        )

    bundle: dict[str, Any] = {
        "kind": "mailhub.imap.condstore_live_probe",
        "label": args.label,
        "recorded_at": datetime.now(UTC).isoformat(),
        "host": args.host,
        "port": args.port,
        "folder": args.folder,
        "tls_trust": "private_ca" if args.ca_file else "system_store",
        "capabilities": list(capabilities),
        "append_status": append_status,
        "highest_modseq": highest_modseq,
        "first_cursor": first_cursor,
        "second_cursor": second_cursor,
        "checks": evidence.checks,
        "failures": evidence.failures,
        "passed": not evidence.failures,
    }

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(bundle, indent=2, ensure_ascii=False)
        args.json.write_text(payload + chr(10), encoding="utf-8")

    for item in evidence.checks:
        mark = "ok  " if item["ok"] else "FAIL"
        print(mark + " " + str(item["name"]) + " :: " + str(item["detail"]))
    print("")
    passed = len(evidence.checks) - len(evidence.failures)
    print("checks   : " + str(passed) + "/" + str(len(evidence.checks)))
    print("cursor   : " + first_cursor)
    if args.json is not None:
        print("evidence : " + str(args.json))
    return 0 if not evidence.failures else 1


if __name__ == "__main__":
    sys.exit(main())
