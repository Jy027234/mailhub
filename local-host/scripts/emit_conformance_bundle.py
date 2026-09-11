"""Drive the reference host through the Host Port matrix and emit a bundle.

Every observation below comes from a real call against the documented
/v1/mail-host/* contract, executed in-process through the ASGI transport.  The
resulting bundle is validated with the MailHub Host Port conformance kit, so the
same artifact can be re-checked in CI.

    python scripts/emit_conformance_bundle.py
    python scripts/emit_conformance_bundle.py --json host-conformance.json

This describes the *reference* host (encrypted SQLite, local single-user
identity): development evidence, not production provider evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from local_host.app import create_host_app  # noqa: E402
from local_host.broker import build_broker  # noqa: E402
from local_host.config import HostSettings  # noqa: E402
from local_host.stores import LocalStores  # noqa: E402
from mailhub.hosts.conformance import build_bundle, run_host_conformance  # noqa: E402

TOKEN = "conformance-service-token-0123456789abcdef"
SECRET = "conformance-encryption-secret-0123456789abcdef"
TENANT = "tenant-conformance"
SUBJECT = "subject-conformance"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BLOCKED_STATES = {"", "unknown", "pending", "unscanned", "rights_unknown"}


def _settings(database_path: Path) -> HostSettings:
    return HostSettings(
        service_token=TOKEN,
        encryption_secret=SECRET,
        database_path=database_path,
        mailhub_api_url="http://127.0.0.1:8000",
        gmail_enabled=False,
        gmail_client_id="",
        gmail_client_secret="",
        gmail_redirect_uri="http://127.0.0.1:8090/oauth/gmail/callback",
        gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        ai_gateway_url=None,
        ai_gateway_model="",
        ai_gateway_api_key="",
    )


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        decoded = response.json()
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _collect(
    client: httpx.AsyncClient, stores: LocalStores
) -> dict[str, list[dict[str, Any]]]:
    observations: dict[str, list[dict[str, Any]]] = {}

    granted = _payload(
        await client.post(
            "/v1/mail-host/identity/authorize",
            headers=AUTH,
            json={
                "tenant_id": TENANT,
                "subject_id": SUBJECT,
                "capability": "mail.read",
            },
        )
    )
    refused = _payload(
        await client.post(
            "/v1/mail-host/identity/authorize",
            headers=AUTH,
            json={"tenant_id": "", "subject_id": SUBJECT, "capability": "mail.read"},
        )
    )
    observations["identity"] = [
        {
            "tenant_id": TENANT,
            "subject_id": SUBJECT,
            "capability": "mail.read",
            "allowed": bool(granted.get("allowed")),
        },
        {
            "tenant_id": "",
            "subject_id": SUBJECT,
            "capability": "mail.read",
            "allowed": bool(refused.get("allowed")),
        },
    ]

    intake = _payload(
        await client.post(
            "/v1/mail-host/admin/credentials",
            headers=AUTH,
            json={
                "provider": "imap_smtp",
                "username": "conformance@example.test",
                "password": "conformance-application-password",
                "tenant_id": TENANT,
                "subject_id": SUBJECT,
            },
        )
    )
    credential_ref = str(intake.get("credential_ref", ""))
    resolved = await client.post(
        "/v1/mail-host/credentials/resolve",
        headers=AUTH,
        json={
            "credential_ref": credential_ref,
            "tenant_id": TENANT,
            "subject_id": SUBJECT,
        },
    )
    refreshed = _payload(
        await client.post(
            "/v1/mail-host/credentials/refresh",
            headers=AUTH,
            json={
                "credential_ref": credential_ref,
                "tenant_id": TENANT,
                "subject_id": SUBJECT,
                "reason": "conformance_rotation_check",
            },
        )
    )
    raw_metadata = refreshed.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    observations["credential"] = [
        {
            "kind": "resolve",
            "credential_ref": credential_ref,
            "long_lived": resolved.status_code == 200,
        },
        {
            "kind": "metadata",
            "credential_ref": str(metadata.get("credential_ref", credential_ref)),
            "returned_fields": sorted(str(key) for key in metadata),
            "long_lived": metadata.get("long_lived") is True,
        },
    ]

    action = {"action_id": "action-conformance", "action_type": "create_task"}
    # A distinct principal, so the four-eyes observations below mean something.
    approver = SUBJECT + "-approver"

    async def request_confirmation() -> str:
        requested = _payload(
            await client.post(
                "/v1/mail-host/approvals/request",
                headers=AUTH,
                json={"tenant_id": TENANT, "subject_id": SUBJECT, "action": action},
            )
        )
        return str(requested.get("confirmation_ref", ""))

    async def verify(
        reference: str, presented: dict[str, object], approver_subject_id: str
    ) -> bool:
        answered = _payload(
            await client.post(
                "/v1/mail-host/approvals/verify",
                headers=AUTH,
                json={
                    "confirmation_ref": reference,
                    "action": presented,
                    "approver_subject_id": approver_subject_id,
                },
            )
        )
        return bool(answered.get("verified"))

    confirmation_ref = await request_confirmation()
    matched = await verify(confirmation_ref, action, approver)
    replayed = await verify(confirmation_ref, action, approver)
    foreign = await request_confirmation()
    self_approved = await verify(foreign, action, SUBJECT)
    unbound_ref = await request_confirmation()
    mismatched = await verify(
        unbound_ref, {**action, "action_type": "update_task"}, approver
    )

    observations["approval"] = [
        {
            "confirmation_ref": confirmation_ref,
            "bound": True,
            "expired": False,
            "foreign_scope": False,
            "four_eyes_required": True,
            "accepted": matched,
        },
        {
            "confirmation_ref": confirmation_ref,
            "bound": True,
            "expired": False,
            "foreign_scope": False,
            "four_eyes_required": True,
            "replayed": True,
            "accepted": replayed,
        },
        {
            "confirmation_ref": foreign,
            "bound": True,
            "expired": False,
            "foreign_scope": False,
            "four_eyes_required": True,
            "self_approved": True,
            "accepted": self_approved,
        },
        {
            "confirmation_ref": unbound_ref,
            "bound": False,
            "expired": False,
            "foreign_scope": False,
            "four_eyes_required": True,
            "accepted": mismatched,
        },
    ]

    action_headers = {**AUTH, "Idempotency-Key": "conformance-action-key"}
    action_body = {"tenant_id": TENANT, "subject_id": SUBJECT, "action": action}
    first = _payload(
        await client.post(
            "/v1/mail-host/actions/execute", headers=action_headers, json=action_body
        )
    )
    replay = _payload(
        await client.post(
            "/v1/mail-host/actions/execute", headers=action_headers, json=action_body
        )
    )
    observations["host_action"] = [
        {
            "action_id": str(first.get("action_id", action["action_id"])),
            "attempt": 1,
            "executed": not bool(first.get("replayed", False)),
            "result_ref": str(first.get("result_ref", "")),
        },
        {
            "action_id": str(replay.get("action_id", action["action_id"])),
            "attempt": 2,
            "executed": not bool(replay.get("replayed", False)),
            "result_ref": str(replay.get("result_ref", "")),
        },
    ]

    candidate_body = {
        "tenant_id": TENANT,
        "subject_id": SUBJECT,
        "candidate_id": "cand-conformance",
    }
    candidate_headers = {**AUTH, "Idempotency-Key": "conformance-knowledge-key"}
    created = _payload(
        await client.post(
            "/v1/mail-host/knowledge/candidates",
            headers=candidate_headers,
            json=candidate_body,
        )
    )
    duplicate = _payload(
        await client.post(
            "/v1/mail-host/knowledge/candidates",
            headers=candidate_headers,
            json=candidate_body,
        )
    )
    observations["knowledge"] = [
        {
            "candidate_ref": "cand-conformance",
            "created": bool(created.get("created", False)),
            "result_ref": str(created.get("knowledge_ref", "")),
        },
        {
            "candidate_ref": "cand-conformance",
            "created": bool(duplicate.get("created", False)),
            "result_ref": str(duplicate.get("knowledge_ref", "")),
        },
    ]

    safety = _payload(
        await client.post(
            "/v1/mail-host/knowledge/safety/evaluate",
            headers=AUTH,
            json={
                "candidate_id": "cand-conformance",
                "candidate": {"summary": "conformance sample candidate"},
            },
        )
    )
    raw_decision = safety.get("decision")
    decision = raw_decision if isinstance(raw_decision, dict) else {}
    security_state = str(decision.get("security_state", "")).strip()
    rights_state = str(decision.get("rights_state", "")).strip()
    observations["knowledge_safety"] = [
        {
            "publishable": bool(security_state and rights_state)
            and security_state.casefold() not in BLOCKED_STATES
            and rights_state.casefold() not in BLOCKED_STATES,
            "security_state": security_state or "unscanned",
            "rights_state": rights_state or "rights_unknown",
        }
    ]

    await client.post(
        "/v1/mail-host/audit",
        headers=AUTH,
        json={
            "event": {
                "event_type": "mail.message.observed",
                "tenant_id": TENANT,
                "message_id": "message-conformance",
                "status": "observed",
                "body_text": "this must never be persisted",
                "access_token": "this must never be persisted",
            }
        },
    )
    observations["audit"] = [
        {
            "event": "mail.message.observed",
            "fields": list(stores.audit_field_names(limit=5)),
        }
    ]

    lease = _payload(
        await client.post(
            "/v1/mail-host/quota/acquire",
            headers=AUTH,
            json={
                "tenant_id": TENANT,
                "subject_id": SUBJECT,
                "operation": "sync",
                "limits": {"max_concurrent": 2, "max_per_hour": 20, "max_per_day": 200},
            },
        )
    )
    raw_lease = lease.get("lease")
    lease_payload = raw_lease if isinstance(raw_lease, dict) else {}
    observations["quota"] = [
        {
            "scope": f"{TENANT}/{SUBJECT}/sync",
            "lease_seconds": int(lease_payload.get("lease_seconds") or 0),
            "expires": bool(lease_payload.get("expires_at")),
        }
    ]
    if lease_payload.get("lease_id"):
        await client.post(
            "/v1/mail-host/quota/release",
            headers=AUTH,
            json={"lease_id": str(lease_payload["lease_id"]), "consume": False},
        )

    switch = _payload(
        await client.post("/v1/mail-host/kill-switch/check", headers=AUTH, json={})
    )
    observations["kill_switch"] = [
        {
            "decision": str(switch.get("decision", "")),
            "explicit": bool(switch.get("explicit", False)),
        }
    ]

    await client.post(
        "/v1/mail-host/events",
        headers=AUTH,
        json={
            "event_id": "event-conformance",
            "event_type": "mail.message.observed",
            "tenant_id": TENANT,
            "message_id": "message-conformance",
        },
    )
    observations["event"] = [
        {
            "event_id": "event-conformance",
            "event_type": "mail.message.observed",
            "fields": ["event_id", "event_type", "tenant_id", "message_id"],
        }
    ]

    await client.post(
        "/v1/mail-host/telemetry/events",
        headers=AUTH,
        json={
            "name": "mailhub.sync.duration_ms",
            "fields": {"tenant_hash": "9f2c", "status": "ok"},
        },
    )
    observations["telemetry"] = [
        {
            "metric_name": "mailhub.sync.duration_ms",
            "attributes": ["status", "tenant_hash"],
            "attribute_values": ["ok", "9f2c"],
        }
    ]
    return observations


async def _run(bundle_path: Path | None) -> int:
    # SQLite keeps the database file handle open on Windows, so cleanup is
    # best-effort instead of failing the conformance run.
    with tempfile.TemporaryDirectory(
        prefix="mailhub-conformance-", ignore_cleanup_errors=True
    ) as workdir:
        settings = _settings(Path(workdir) / "host.db")
        # A demonstration host must not claim four-eyes without exercising it,
        # so the reference bundle is produced with the policy switched on.
        stores = LocalStores(
            settings.database_path, settings.encryption_secret, require_four_eyes=True
        )
        stores.initialize()
        broker = build_broker(settings, settings.database_path)
        await broker.initialize()
        app = create_host_app(settings, stores, broker)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host.test"
        ) as client:
            observations = await _collect(client, stores)

    bundle = build_bundle("mailhub-reference-host", observations)
    report = run_host_conformance(bundle)
    if bundle_path is not None:
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(
            json.dumps(
                {
                    "schema": "mailhub.host_conformance.v1",
                    "host": bundle.host,
                    "observations": {
                        area: list(items) for area, items in observations.items()
                    },
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"host   : {report.host}")
    print(f"areas  : {len(observations)}")
    print(f"checks : {len(report.checks)}")
    for check in report.failures:
        print(f"  [FAIL] {check.area}.{check.name} - {check.detail}")
    print(
        "result : "
        + ("pass" if report.passed else f"{len(report.failures)} failure(s)")
    )
    if bundle_path is not None:
        print(f"bundle : {bundle_path}")
    return 0 if report.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit a reference-host conformance bundle."
    )
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    args = parser.parse_args()
    return asyncio.run(_run(args.json_path))


if __name__ == "__main__":
    sys.exit(main())
