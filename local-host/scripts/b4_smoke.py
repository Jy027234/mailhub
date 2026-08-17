"""B4 wiring preflight: verify the local host and durable MailHub graph.

Runs against the live services started by scripts/run_host.ps1 and
scripts/run_mailhub.ps1.  It exercises the documented host contract with the
shared service token and confirms the durable MailHub graph is ready.  It
never contacts a Provider and produces no activation evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

HOST_URL = "http://127.0.0.1:8090"
MAILHUB_URL = "http://127.0.0.1:8000"


def _api_auth_header() -> dict[str, str]:
    token = os.environ.get("MAILHUB_API_AUTH_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def load_env() -> None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if value.strip():
            os.environ.setdefault(name.strip(), value.strip())


def _check(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def _bearer() -> dict[str, str]:
    token = os.environ.get("MAILHUB_HOST_SERVICE_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}


def _json(response: httpx.Response) -> dict[str, Any]:
    value: Any = response.json()
    return value if isinstance(value, dict) else {}


def main() -> int:
    load_env()
    results: list[bool] = []
    host_headers = _bearer()
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        try:
            health = client.get(f"{HOST_URL}/health")
            results.append(
                _check(
                    "local-host /health",
                    health.status_code == 200,
                    str(health.status_code),
                )
            )
        except httpx.HTTPError as exc:
            results.append(_check("local-host /health", False, f"unreachable: {exc}"))
            results.append(
                _check("MailHub /health/ready", False, "skipped: host unreachable")
            )
            return 1 if not all(results) else 0

        identity = client.post(
            f"{HOST_URL}/v1/mail-host/identity/authorize",
            headers=host_headers,
            json={
                "tenant_id": "b4-tenant",
                "subject_id": "b4-user",
                "capability": "mail.read",
            },
        )
        results.append(
            _check(
                "host identity authorize",
                identity.status_code == 200 and _json(identity).get("allowed") is True,
                f"status={identity.status_code}",
            )
        )

        content = "b4-smoke-object"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        put = client.post(
            f"{HOST_URL}/v1/mail-host/objects",
            headers=host_headers,
            json={
                "tenant_id": "b4-tenant",
                "subject_id": "b4-user",
                "purpose": "smoke",
                "content": content,
                "content_sha256": digest,
            },
        )
        object_ok = put.status_code == 200
        object_ref = _json(put).get("object_ref", "") if object_ok else ""
        if object_ok and isinstance(object_ref, str):
            read = client.post(
                f"{HOST_URL}/v1/mail-host/objects/read",
                headers=host_headers,
                json={
                    "tenant_id": "b4-tenant",
                    "subject_id": "b4-user",
                    "object_ref": object_ref,
                },
            )
            object_ok = (
                read.status_code == 200 and _json(read).get("content") == content
            )
            client.post(
                f"{HOST_URL}/v1/mail-host/objects/delete",
                headers=host_headers,
                json={
                    "tenant_id": "b4-tenant",
                    "subject_id": "b4-user",
                    "object_ref": object_ref,
                },
            )
        results.append(
            _check("host object store roundtrip", object_ok, "put/read/delete")
        )

        quota = client.post(
            f"{HOST_URL}/v1/mail-host/quota/acquire",
            headers=host_headers,
            json={
                "tenant_id": "b4-tenant",
                "subject_id": "b4-user",
                "account_id": None,
                "operation": "smoke",
                "limits": {
                    "max_concurrent": 1,
                    "max_per_hour": 100,
                    "max_per_day": 1000,
                },
            },
        )
        lease = _json(quota).get("lease") if quota.status_code == 200 else None
        quota_ok = isinstance(lease, dict) and isinstance(lease.get("lease_id"), str)
        if quota_ok:
            client.post(
                f"{HOST_URL}/v1/mail-host/quota/release",
                headers=host_headers,
                json={"lease_id": lease["lease_id"], "consume": False},
            )
        results.append(
            _check("host quota lease", quota_ok, f"status={quota.status_code}")
        )

        event = client.post(
            f"{HOST_URL}/v1/mail-host/events",
            headers={**host_headers, "Idempotency-Key": "mailhub-event:b4-smoke-event"},
            json={
                "event_id": "b4-smoke-event",
                "event_type": "mail.sync_job.completed",
                "occurred_at": "2026-08-15T00:00:00Z",
                "tenant_id": "b4-tenant",
            },
        )
        results.append(
            _check(
                "host event publisher", event.status_code == 200, str(event.status_code)
            )
        )

        try:
            health = client.get(f"{MAILHUB_URL}/health")
            results.append(
                _check(
                    "MailHub /health",
                    health.status_code == 200,
                    str(health.status_code),
                )
            )
        except httpx.HTTPError as exc:
            results.append(_check("MailHub /health", False, f"unreachable: {exc}"))
            return 1 if not all(results) else 0

        gmail_enabled = os.environ.get(
            "MAILHUB_GMAIL_ENABLED", ""
        ).strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        ready = client.get(f"{MAILHUB_URL}/health/ready")
        ready_payload = _json(ready)
        status_ok = ready_payload.get("status") == "ready" or (
            not gmail_enabled
            and ready_payload.get("status") == "sandbox"
            and ready_payload.get("missing") == []
        )
        results.append(
            _check(
                "MailHub /health/ready",
                ready.status_code == 200 and status_ok,
                json.dumps(ready_payload, ensure_ascii=False)[:300],
            )
        )

        mailhub_headers = {
            "X-MailHub-Tenant": "b4-tenant",
            "X-MailHub-Subject": "b4-user",
            **_api_auth_header(),
        }
        capabilities = client.get(
            f"{MAILHUB_URL}/v1/mail/providers/capabilities", headers=mailhub_headers
        )
        providers = _json(capabilities).get("data", [])
        gmail = next(
            (
                item
                for item in providers
                if isinstance(item, dict) and item.get("provider") == "gmail"
            ),
            None,
        )
        if isinstance(gmail, dict):
            results.append(
                _check(
                    "MailHub gmail connector",
                    gmail.get("capabilities", {}).get("supports_incremental") is True,
                    json.dumps(gmail, ensure_ascii=False)[:300],
                )
            )
        elif gmail_enabled:
            results.append(
                _check("MailHub gmail connector", False, "expected but not registered")
            )
        else:
            print(
                "[INFO] MailHub gmail connector: not registered "
                "(MAILHUB_GMAIL_ENABLED=false; expected until the OAuth client is configured)"
            )

        connections = client.get(
            f"{MAILHUB_URL}/v1/mail/connections", headers=mailhub_headers
        )
        results.append(
            _check(
                "MailHub connections list",
                connections.status_code == 200,
                f"status={connections.status_code}",
            )
        )

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
