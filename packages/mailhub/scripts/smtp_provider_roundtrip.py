"""End-to-end SMTP round trip against a real provider mailbox.

The wire harness proves what MailHub puts on the wire; this proves what a real
provider actually accepts and hands back.  `MAIL-SMTP-001/002` ask for that
provider-side evidence.

It sends **one self-addressed message** through the real
`ImapSmtpConnector.send()`, then reads it back through the same connector's
incremental sync and checks the received message is the one that was sent:

* the provider returned the Message-ID the connector recorded as its receipt;
* the marker survived in the subject and the body;
* the recipient set survived the round trip;
* a second full sync does not deliver the same message twice.

The message is addressed to the mailbox that sends it, so nothing leaves the
operator's own account.  It does stay in that mailbox: the subject carries a
`[mailhub-roundtrip <marker>]` prefix so it can be filtered or deleted.

Settings come from the environment (or an env file); credentials never reach the
evidence bundle, and the account address is stored as domain plus digest.

    python scripts/smtp_provider_roundtrip.py --env-file ../local-host/.env \
        --json evidence.json
    python scripts/smtp_provider_roundtrip.py --validate evidence.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SCHEMA_VERSION = "mailhub.smtp_provider_roundtrip.v1"
MARKER_PREFIX = "mailhub-roundtrip"
POLL_ATTEMPTS = 12
POLL_SECONDS = 6.0

FORBIDDEN_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "body_text",
)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value.strip().strip('"').strip("'")
    return values


def resolve_settings(env_file: Path | None) -> dict[str, str]:
    file_values = parse_env_file(env_file) if env_file is not None else {}

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            candidate = os.environ.get(name) or file_values.get(name)
            if candidate:
                return candidate
        return default

    settings = {
        "imap_host": pick("MAILHUB_IMAP_HOST"),
        "imap_port": pick("MAILHUB_IMAP_PORT", default="993"),
        "smtp_host": pick("MAILHUB_SMTP_HOST"),
        "smtp_port": pick("MAILHUB_SMTP_PORT", default="465"),
        "folder": pick("MAILHUB_IMAP_FOLDER", default="INBOX"),
        "username": pick("HOST_IMAP_USERNAME", "MAILHUB_IMAP_USERNAME"),
        "password": pick(
            "HOST_IMAP_APP_PASSWORD", "MAILHUB_IMAP_APP_PASSWORD", "HOST_IMAP_PASSWORD"
        ),
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise SystemExit("roundtrip_settings_missing: " + ", ".join(missing))
    return settings


def account_facts(address: str) -> dict[str, str]:
    """Store the account as a domain plus digest, never the full address."""

    _, _, domain = address.partition("@")
    return {
        "account_domain": domain or "unknown",
        "account_digest": hashlib.sha256(address.encode("utf-8")).hexdigest()[:12],
    }


def _iter_keys(node: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            keys.append(str(key))
            keys.extend(_iter_keys(value))
    elif isinstance(node, list | tuple):
        for item in node:
            keys.extend(_iter_keys(item))
    return keys


def find_forbidden_keys(bundle: Mapping[str, Any]) -> list[str]:
    offenders: list[str] = []
    for key in _iter_keys(bundle):
        folded = key.strip().casefold()
        if any(marker in folded for marker in FORBIDDEN_KEY_MARKERS):
            offenders.append(key)
    return sorted(set(offenders))


def validate_bundle(bundle: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if bundle.get("schema") != SCHEMA_VERSION:
        issues.append("schema_mismatch")
    offenders = find_forbidden_keys(bundle)
    if offenders:
        issues.append("forbidden_keys:" + ",".join(offenders))
    if bundle.get("passed") is not True:
        issues.append("roundtrip_not_passed")
    checks = bundle.get("checks")
    if not isinstance(checks, Mapping) or not checks:
        issues.append("checks_missing")
    elif not all(value is True for value in checks.values()):
        failed = sorted(str(name) for name, value in checks.items() if value is not True)
        issues.append("checks_failed:" + ",".join(failed))
    received = bundle.get("received")
    if not isinstance(received, Mapping) or not received.get("message_id"):
        issues.append("received_message_missing")
    return issues


def build_connection(username: str) -> Any:
    from mailhub.domain import ConnectionStatus, MailboxConnection, ProviderName

    return MailboxConnection(
        connection_id=uuid4(),
        tenant_id="roundtrip-tenant",
        subject_id="roundtrip-subject",
        provider=ProviderName.IMAP_SMTP,
        email_address=username,
        credential_ref="roundtrip-credential-ref",
        granted_scopes=("mail.read",),
        status=ConnectionStatus.ACTIVE,
    )


def build_request(*, to: str, subject: str, body: str) -> Any:
    from mailhub.ports import ProviderSendRequest

    return ProviderSendRequest(
        operation_id=UUID(int=uuid4().int),
        connection_id=uuid4(),
        thread_ref=None,
        recipient_addresses=(to,),
        subject=subject,
        body_text=body,
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        idempotency_key=f"roundtrip-{uuid4()}",
    )


async def run(*, settings: Mapping[str, str], timeout: float) -> dict[str, Any]:
    from mailhub.connectors.imap_smtp import ImapSmtpConnector

    username = settings["username"]
    credential = {"username": username, "password": settings["password"]}
    marker = uuid4().hex[:12]
    subject = f"[{MARKER_PREFIX} {marker}] MailHub round-trip check"
    body = (
        "This message was sent by MailHub to its own mailbox to prove the "
        f"provider round trip. Marker: {marker}"
    )

    connector = ImapSmtpConnector(
        imap_host=settings["imap_host"],
        imap_port=int(settings["imap_port"]),
        smtp_host=settings["smtp_host"],
        smtp_port=int(settings["smtp_port"]),
        folder=settings["folder"],
        timeout_seconds=timeout,
        send_enabled=True,
    )
    connection = build_connection(username)

    request = build_request(to=username, subject=subject, body=body)
    started = time.monotonic()
    receipt = await connector.send(request, credential=credential)

    attempts = 0
    found: dict[str, Any] | None = None
    duplicate_count = 0
    while attempts < POLL_ATTEMPTS and found is None:
        attempts += 1
        page = await connector.sync(connection, cursor=None, limit=5, credential=credential)
        matches = [
            message
            for message in page.messages
            if marker in (message.subject or "")
            or message.internet_message_id == receipt.provider_message_ref
        ]
        duplicate_count = len(matches)
        if matches:
            message = matches[-1]
            # The receipt's ref *is* the Message-ID MailHub generated, so the
            # provider's own header is what proves the round trip; the connector
            # ref is a server-side locator in a different namespace.
            found = {
                "message_id": message.internet_message_id or "",
                "provider_message_ref": message.provider_message_ref,
                "subject_has_marker": marker in (message.subject or ""),
                "body_has_marker": marker in (message.body_text or ""),
                "recipients_match": username.casefold()
                in {address.casefold() for address in message.recipient_addresses},
                "sender_matches": username.casefold() == message.sender_address.casefold(),
                "attachment_count": message.attachment_count,
            }
        else:
            await asyncio.sleep(POLL_SECONDS)

    # A second full pass must not deliver the same message as a new one.
    second = await connector.sync(connection, cursor=None, limit=5, credential=credential)
    second_matches = [
        message
        for message in second.messages
        if marker in (message.subject or "")
        or message.internet_message_id == receipt.provider_message_ref
    ]

    checks: dict[str, bool] = {
        "receipt_message_id_present": bool(receipt.provider_message_ref),
        "received_within_budget": found is not None,
        "received_message_id_matches_receipt": bool(
            found and found["message_id"] == receipt.provider_message_ref
        ),
        "subject_marker_survived": bool(found and found["subject_has_marker"]),
        "body_marker_survived": bool(found and found["body_has_marker"]),
        "recipient_set_survived": bool(found and found["recipients_match"]),
        "sender_address_matches_account": bool(found and found["sender_matches"]),
        "no_duplicate_on_single_pass": duplicate_count <= 1,
        "second_pass_sees_it_once": len(second_matches) <= 1,
    }
    return {
        "schema": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "provider": "imap_smtp",
        "imap_host": settings["imap_host"],
        "smtp_host": settings["smtp_host"],
        "folder": settings["folder"],
        **account_facts(username),
        "marker": marker,
        "sent": {
            "message_id": receipt.provider_message_ref,
            "subject_bytes": len(subject.encode("utf-8")),
            "body_bytes": len(body.encode("utf-8")),
        },
        "received": found or {},
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "poll_seconds": POLL_SECONDS,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-provider SMTP round trip.")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.validate is not None:
        bundle: Any = json.loads(args.validate.read_text(encoding="utf-8"))
        if not isinstance(bundle, Mapping):
            print("evidence is not a JSON object")
            return 1
        issues = validate_bundle(bundle)
        for issue in issues:
            print("  [FAIL] " + issue)
        print("roundtrip validation: " + ("ok" if not issues else f"{len(issues)} issue(s)"))
        return 0 if not issues else 1

    settings = resolve_settings(args.env_file)
    bundle = asyncio.run(run(settings=settings, timeout=args.timeout))
    issues = validate_bundle(bundle)

    print(f"account     : {bundle['account_domain']} ({bundle['account_digest']})")
    print(f"marker      : {bundle['marker']}")
    print(f"sent        : {bundle['sent']['message_id']}")
    received = bundle["received"]
    print(f"received    : {received.get('message_id', '(not found)')}")
    print(f"attempts    : {bundle['attempts']} in {bundle['elapsed_seconds']}s")
    for name, value in bundle["checks"].items():
        print(f"  [{'PASS' if value else 'FAIL'}] {name}")
    for issue in issues:
        print("  [FAIL] " + issue)
    print("roundtrip   : " + ("pass" if not issues else f"{len(issues)} issue(s)"))
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
