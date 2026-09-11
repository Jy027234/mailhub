"""End-to-end evidence for OUTCOME_UNKNOWN reconciliation.

An unknown outcome is not a theory to be asserted; it is a state to be produced
and then resolved.  This script produces it with a real SMTP conversation and
resolves it with the real reconciler and the real host probe:

  1. a raw TLS SMTP server accepts the message and then drops the connection
     without replying -- the connector cannot tell whether the message was
     accepted, so it raises OutcomeUnknownError;
  2. the accepted message is appended to a real IMAP mailbox (the fixture);
  3. the host probe searches that mailbox by Message-ID and reports what it
     finds;
  4. MailService.reconcile_outbound_outcome turns the observation into a status.

Three cases, one per branch of the three-state contract:

  delivered   connection dropped after DATA, message appended  -> RECONCILED_SUCCEEDED
  absent      connection dropped before DATA, nothing appended -> RETRY_WAIT
  indeterminate  mailbox unreachable                           -> OUTCOME_UNKNOWN

Nothing here contacts a real provider: the SMTP peer is a local socket and the
mailbox is the local Dovecot fixture.

    python scripts/outcome_unknown_reconciliation.py \
        --host 127.0.0.1 --port 10993 --username probe@example.test \
        --password dovecot-test-password \
        --ca-file /tmp/mh-dovecot/conf/ca.pem \
        --json ../../docs/reports/mailhub-outcome-unknown-reconciliation.json
    python scripts/outcome_unknown_reconciliation.py --validate <json>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

LOCAL_HOST_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LOCAL_HOST_ROOT.parent
MAILHUB_SRC = REPO_ROOT / "packages" / "mailhub" / "src"
MAILHUB_SCRIPTS = REPO_ROOT / "packages" / "mailhub" / "scripts"
for candidate in (str(MAILHUB_SRC), str(LOCAL_HOST_ROOT), str(MAILHUB_SCRIPTS)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

SCHEMA = "mailhub.outcome_unknown_reconciliation.v1"
REQUIRED_CASES = ("delivered", "absent", "indeterminate")


def _wire_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "smtp_wire_conformance", MAILHUB_SCRIPTS / "smtp_wire_conformance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class DroppingSmtpServer:
    """A TLS SMTP peer that takes the message and then goes silent.

    Two modes, because the reconciler has to tell them apart later: the message
    really was accepted (drop after DATA) or it never was (drop at DATA).
    """

    def __init__(self, *, mode: str, key_path: Path, cert_path: Path) -> None:
        assert mode in {"after_data", "at_data"}
        self.mode = mode
        self.key_path = key_path
        self.cert_path = cert_path
        self.port = free_port()
        self.received: list[bytes] = []
        self.logins: list[str] = []
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(1)
        listener.settimeout(30.0)
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # -- protocol ----------------------------------------------------------

    def _serve(self) -> None:
        assert self._listener is not None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=str(self.cert_path), keyfile=str(self.key_path)
        )
        try:
            connection, _ = self._listener.accept()
        except (TimeoutError, OSError):
            return
        try:
            with context.wrap_socket(connection, server_side=True) as tls:
                self._converse(tls)
        except (ssl.SSLError, OSError):
            return

    def _converse(self, tls: ssl.SSLSocket) -> None:
        tls.sendall(b"220 capture ESMTP\r\n")
        stream = tls.makefile("rb")
        while True:
            line = stream.readline()
            if not line:
                return
            command = line.strip().upper()
            if command.startswith(b"EHLO") or command.startswith(b"HELO"):
                tls.sendall(b"250-capture\r\n250-AUTH LOGIN\r\n250 OK\r\n")
            elif command.startswith(b"AUTH LOGIN"):
                tls.sendall(b"334 " + base64.b64encode(b"Username:") + b"\r\n")
                user = stream.readline().strip()
                tls.sendall(b"334 " + base64.b64encode(b"Password:") + b"\r\n")
                stream.readline()
                self.logins.append(base64.b64decode(user).decode("utf-8", "replace"))
                tls.sendall(b"235 authenticated\r\n")
            elif command.startswith(b"MAIL FROM") or command.startswith(b"RCPT TO"):
                tls.sendall(b"250 OK\r\n")
            elif command.startswith(b"DATA"):
                if self.mode == "at_data":
                    # Never accepted, so there is nothing for the mailbox to hold.
                    tls.close()
                    return
                tls.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                payload = self._read_data(stream)
                self.received.append(payload)
                # Accepted, then the peer disappears before saying so.
                tls.close()
                return
            elif command.startswith(b"QUIT"):
                tls.sendall(b"221 bye\r\n")
                return
            else:
                tls.sendall(b"250 OK\r\n")

    @staticmethod
    def _read_data(stream: Any) -> bytes:
        chunks: list[bytes] = []
        while True:
            line = stream.readline()
            if not line:
                break
            if line in (b".\r\n", b".\n"):
                break
            chunks.append(line)
        return b"".join(chunks)


class HostProbe:
    """The real host-side probe, wrapped in the port the service expects."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        folders: tuple[str, ...],
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.folders = folders
        self.observations: list[dict[str, Any]] = []

    async def observe_outbound(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: Any,
        internet_message_id: str,
    ) -> Any:
        del tenant_id, subject_id, connection
        from local_host.outbound import observe_outbound_in_mailbox
        from mailhub.ports import OutboundObservation

        result = await _to_thread(
            observe_outbound_in_mailbox,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            folders=self.folders,
            internet_message_id=internet_message_id,
        )
        self.observations.append({"message_id": internet_message_id, **result})
        return OutboundObservation(
            found=result.get("found"),
            provider_message_ref=result.get("provider_message_ref"),
            mailbox=result.get("mailbox"),
            detail=str(result.get("detail", "")),
        )


async def _to_thread(function: Any, **kwargs: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(lambda: function(**kwargs))


# -- case runner -----------------------------------------------------------


def _append_message(args: argparse.Namespace, raw: bytes) -> str:
    """Put the accepted message into the mailbox, as a receiving MTA would."""

    import imaplib

    context = ssl.create_default_context(cafile=str(args.ca_file))
    client = imaplib.IMAP4_SSL(args.host, args.port, ssl_context=context)
    try:
        client.login(args.username, args.password)
        status, _ = client.append(args.folder, None, None, raw)
        return status
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _probe_port_for(args: argparse.Namespace) -> int:
    """A port nothing is listening on, so the mailbox is genuinely unreachable."""

    return free_port()


def _run_case(args: argparse.Namespace, case: str) -> dict[str, Any]:
    import asyncio

    from mailhub.domain import (
        DeliveryStatus,
        MailboxConnection,
        MailOutboxOperation,
        ProviderName,
        outbound_internet_message_id,
    )
    from mailhub.errors import OutcomeUnknownError
    from mailhub.service import MailService
    from mailhub.storage import InMemoryMailRepository

    mode = "at_data" if case == "absent" else "after_data"
    append = case == "delivered"
    probe_port = _probe_port_for(args) if case == "indeterminate" else args.port

    with tempfile.TemporaryDirectory(
        prefix="mh-outcome-", ignore_cleanup_errors=True
    ) as work:
        workdir = Path(work)
        key_path, cert_path = _wire_module().generate_certificate(workdir)
        os.environ["SSL_CERT_FILE"] = str(cert_path)
        server = DroppingSmtpServer(mode=mode, key_path=key_path, cert_path=cert_path)
        server.start()
        try:
            connection_id = uuid4()
            operation_id = uuid4()
            from mailhub.connectors.imap_smtp import ImapSmtpConnector
            from mailhub.ports import ProviderSendRequest

            connector = ImapSmtpConnector(
                imap_host="127.0.0.1",
                imap_port=probe_port,
                smtp_host="127.0.0.1",
                smtp_port=server.port,
                send_enabled=True,
                timeout_seconds=20.0,
            )
            request = ProviderSendRequest(
                operation_id=operation_id,
                connection_id=connection_id,
                thread_ref="thread-outcome",
                recipient_addresses=(args.username,),
                cc_addresses=(),
                bcc_addresses=(),
                subject="mailhub outcome reconciliation probe",
                body_text="probe body",
                content_sha256="a" * 64,
                idempotency_key="outcome-" + str(operation_id),
                in_reply_to_message_ref=None,
            )
            connection = MailboxConnection(
                connection_id=connection_id,
                tenant_id="probe-tenant",
                subject_id="probe-subject",
                provider=ProviderName.IMAP_SMTP,
                email_address=args.username,
                credential_ref="probe-credential",
            )
            raised: str | None = None
            try:
                asyncio.run(
                    connector.send(
                        request,
                        credential={
                            "username": args.username,
                            "password": args.password,
                        },
                    )
                )
            except OutcomeUnknownError as exc:
                raised = exc.message
            except Exception as exc:  # noqa: BLE001 - reported verbatim
                raised = type(exc).__name__ + ": " + str(exc)

            message_id = outbound_internet_message_id(operation_id)
            accepted = bool(server.received)
            append_status = ""
            if append and accepted:
                append_status = _append_message(args, server.received[0])

            repository = InMemoryMailRepository()
            asyncio.run(repository.save_connection(connection))
            asyncio.run(
                repository.create_or_get_operation(
                    MailOutboxOperation(
                        operation_id=operation_id,
                        tenant_id="probe-tenant",
                        subject_id="probe-subject",
                        draft_id=uuid4(),
                        connection_id=connection_id,
                        idempotency_key="outcome-" + str(operation_id),
                        status=DeliveryStatus.OUTCOME_UNKNOWN,
                    )
                )
            )
            probe = HostProbe(
                host=args.host,
                port=probe_port,
                username=args.username,
                password=args.password,
                folders=(args.folder,),
            )
            service = MailService(
                repository, connectors={}, outbound_reconciliation_port=probe
            )
            reconciled = asyncio.run(
                service.reconcile_outbound_outcome(
                    tenant_id="probe-tenant",
                    subject_id="probe-subject",
                    operation_id=operation_id,
                )
            )
            return {
                "case": case,
                "smtp_mode": mode,
                "connector_error": raised,
                "message_id": message_id,
                "peer_accepted_data": accepted,
                "message_id_present_in_payload": bool(
                    server.received and message_id.encode() in server.received[0]
                ),
                "append_status": append_status,
                "probe_observations": probe.observations,
                "final_status": reconciled.status.value,
            }
        finally:
            server.stop()


# -- evidence contract -----------------------------------------------------

EXPECTED: dict[str, dict[str, Any]] = {
    "delivered": {"final_status": "reconciled_succeeded", "found": True},
    "absent": {"final_status": "retry_wait", "found": False},
    "indeterminate": {"final_status": "outcome_unknown", "found": None},
}


def evaluate(cases: list[dict[str, Any]]) -> list[str]:
    """Every way this evidence could be weaker than it looks."""

    issues: list[str] = []
    seen = {str(entry.get("case")) for entry in cases}
    for missing in REQUIRED_CASES:
        if missing not in seen:
            issues.append("case_missing:" + missing)
    for entry in cases:
        case = str(entry.get("case"))
        expected = EXPECTED.get(case)
        if expected is None:
            issues.append("case_unknown:" + case)
            continue
        if entry.get("connector_error") != "smtp_outcome_unknown":
            issues.append(case + ":connector_did_not_report_an_unknown_outcome")
        observations = entry.get("probe_observations") or []
        if len(observations) != 1:
            issues.append(
                case + ":probe_consulted_" + str(len(observations)) + "_times"
            )
        elif observations[0].get("found") is not expected["found"]:
            issues.append(
                case + ":observation_was_" + repr(observations[0].get("found"))
            )
        if entry.get("final_status") != expected["final_status"]:
            issues.append(case + ":final_status_" + str(entry.get("final_status")))
        if case == "delivered":
            if not entry.get("peer_accepted_data"):
                issues.append("delivered:the_peer_never_accepted_the_message")
            if not entry.get("message_id_present_in_payload"):
                issues.append("delivered:the_sent_message_id_is_not_in_the_payload")
            if not str(entry.get("append_status", "")).startswith("OK"):
                issues.append("delivered:the_message_never_reached_the_mailbox")
        if case == "absent" and entry.get("peer_accepted_data"):
            issues.append("absent:the_peer_accepted_a_message_it_should_not_have")
    return issues


def _redacted_account(username: str) -> dict[str, str]:
    domain = username.rpartition("@")[2] or "unknown"
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]
    return {"account_domain": domain, "account_digest": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10993)
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--ca-file", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    args = parser.parse_args()

    if args.validate is not None:
        bundle: Any = json.loads(args.validate.read_text(encoding="utf-8"))
        if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA:
            print("evidence is not a " + SCHEMA + " bundle")
            return 1
        issues = evaluate(list(bundle.get("cases") or []))
        if issues:
            print("INVALID")
            for issue in issues:
                print("  - " + issue)
            return 1
        print("evidence ok: " + str(len(bundle.get("cases") or [])) + " cases")
        return 0

    if not args.username or not args.password:
        print("--username and --password are required to produce evidence")
        return 2
    if args.ca_file is not None:
        os.environ["SSL_CERT_FILE"] = str(args.ca_file)

    cases = [_run_case(args, case) for case in REQUIRED_CASES]
    issues = evaluate(cases)
    bundle = {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(),
        "host": args.host,
        "port": args.port,
        "folder": args.folder,
        "tls_trust": "private_ca" if args.ca_file else "system_store",
        "smtp_peer": "local_dropping_tls_capture",
        "mailbox": "local_dovecot_fixture",
        **_redacted_account(args.username),
        "cases": cases,
        "issues": issues,
        "passed": not issues,
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8"
        )
    for entry in cases:
        mark = "ok  " if entry["case"] not in " ".join(issues) else "FAIL"
        print(
            mark
            + " "
            + str(entry["case"])
            + " :: connector="
            + str(entry["connector_error"])
            + " observation="
            + str((entry.get("probe_observations") or [{}])[0].get("found"))
            + " status="
            + str(entry["final_status"])
        )
    print("")
    print("cases    : " + str(len(cases)))
    print("issues   : " + (", ".join(issues) if issues else "none"))
    if args.json is not None:
        print("evidence : " + str(args.json))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
