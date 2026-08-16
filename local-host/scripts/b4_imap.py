"""B4 IMAP walk: real mailbox slice through the durable graph without OAuth.

Requires the local host and durable MailHub graph (run_host.ps1 /
run_mailhub.ps1) and an IMAP application password for the isolated mailbox
(HOST_IMAP_APP_PASSWORD env or interactive prompt).  Flow:

    intake credential -> create/activate connection -> bounded backfill sync
    -> idempotent replay -> incremental sync -> projections -> analysis

This is local engineering evidence for the IMAP transport; it does not close
the Gmail REST (M2) gate and produces no Provider REST activation bundle.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

HOST_URL = "http://127.0.0.1:8090"
MAILHUB_URL = "http://127.0.0.1:8000"
TENANT = "b4-tenant"
SUBJECT = "b4-user"


def load_env() -> None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        print("local-host/.env not found; copy .env.example first")
        sys.exit(2)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if value.strip():
            os.environ.setdefault(name.strip(), value.strip())


def _json(response: httpx.Response) -> dict[str, Any]:
    value: Any = response.json()
    return value if isinstance(value, dict) else {}


def _data(response: httpx.Response) -> dict[str, Any]:
    value = _json(response).get("data", {})
    return value if isinstance(value, dict) else {}


def _fail(step: str, response: httpx.Response) -> None:
    print(f"[FAIL] {step}: {response.status_code} {response.text[:400]}")
    sys.exit(1)


def wait_for_job(
    client: httpx.Client, headers: dict[str, str], job_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = client.get(
            f"{MAILHUB_URL}/v1/mail/sync-jobs/{job_id}", headers=headers
        )
        if response.status_code != 200:
            _fail("poll sync job", response)
        job = _data(response)
        if job.get("status") in {"completed", "failed", "cancelled", "dead_letter"}:
            return job
        time.sleep(2)
    print(f"[FAIL] sync job {job_id} did not reach a terminal state in 120s")
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("HOST_IMAP_USERNAME", ""))
    parser.add_argument(
        "--received-after", default=None, help="ISO UTC, e.g. 2026-07-01T00:00:00Z"
    )
    parser.add_argument(
        "--received-before", default=None, help="ISO UTC, e.g. 2026-08-31T00:00:00Z"
    )
    parser.add_argument("--skip-analyze", action="store_true")
    args = parser.parse_args()
    load_env()

    enabled = os.environ.get("MAILHUB_IMAP_ENABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        print("MAILHUB_IMAP_ENABLED 不是 true；先在 .env 启用 IMAP 并填入主机配置")
        return 2
    username = args.username or input("隔离邮箱地址: ").strip()
    password = os.environ.get("HOST_IMAP_APP_PASSWORD", "")
    if not password:
        password = getpass.getpass(f"{username} 的 IMAP 应用密码: ")

    host_headers = {
        "Authorization": f"Bearer {os.environ['MAILHUB_HOST_SERVICE_TOKEN']}"
    }
    mail_headers = {"X-MailHub-Tenant": TENANT, "X-MailHub-Subject": SUBJECT}

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        ready = client.get(f"{MAILHUB_URL}/health/ready")
        if ready.status_code != 200 or _json(ready).get("status") not in {
            "ready",
            "sandbox",
        }:
            _fail("MailHub readiness", ready)

        print(f"[1] 宿主凭据入库（{username}）")
        intake = client.post(
            f"{HOST_URL}/v1/mail-host/admin/credentials",
            headers=host_headers,
            json={
                "provider": "imap_smtp",
                "tenant_id": TENANT,
                "subject_id": SUBJECT,
                "username": username,
                "password": password,
            },
        )
        if intake.status_code != 200:
            _fail("credential intake", intake)
        credential_ref = _json(intake)["credential_ref"]
        print(f"[1] credential_ref={credential_ref}")

        print("[2] 创建连接")
        created = client.post(
            f"{MAILHUB_URL}/v1/mail/connections",
            headers=mail_headers,
            json={
                "provider": "imap_smtp",
                "email_address": username,
                "credential_ref": credential_ref,
                "granted_scopes": [],
            },
        )
        if created.status_code != 201:
            _fail("create connection", created)
        connection = _data(created)
        connection_id = str(connection["connection_id"])
        revision = int(connection["revision"])
        print(f"[2] connection={connection_id} status={connection.get('status')}")

        print("[3] 激活连接")
        activated = client.post(
            f"{MAILHUB_URL}/v1/mail/connections/{connection_id}:activate",
            headers=mail_headers,
            json={"expected_revision": revision},
        )
        if activated.status_code != 200:
            _fail("activate connection", activated)
        activated_conn = _data(activated)
        revision = int(activated_conn["revision"])
        print(f"[3] status={activated_conn.get('status')}")

        sync_body: dict[str, Any] = {
            "mode": "backfill",
            "folder_ref": "INBOX",
            "limit": 50,
        }
        if args.received_after:
            sync_body["received_after"] = args.received_after
        if args.received_before:
            sync_body["received_before"] = args.received_before
        idempotency_key = (
            f"b4-imap-backfill-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        )

        print("[4] 有界 backfill 同步（幂等键重放）")
        first_job_ref = ""
        for replay in (False, True):
            response = client.post(
                f"{MAILHUB_URL}/v1/mail/connections/{connection_id}/sync-jobs",
                headers={**mail_headers, "Idempotency-Key": idempotency_key},
                json=sync_body,
            )
            if response.status_code != 200:
                _fail("enqueue backfill", response)
            job = _data(response)
            if replay:
                if job.get("job_ref") != first_job_ref:
                    print("[FAIL] idempotent replay produced a different job_ref")
                    sys.exit(1)
            else:
                first_job_ref = str(job.get("job_ref"))
        run = client.post(
            f"{MAILHUB_URL}/v1/mail/sync-jobs/{first_job_ref}:run", headers=mail_headers
        )
        if run.status_code != 200:
            _fail("run backfill", run)
        finished = wait_for_job(client, mail_headers, first_job_ref)
        print(
            f"[4] backfill {finished.get('status')}: fetched={finished.get('fetched_count')} "
            f"saved={finished.get('saved_count')} deleted={finished.get('deleted_count')}"
        )
        if finished.get("status") != "completed":
            print(f"[FAIL] backfill error_code={finished.get('error_code')}")
            sys.exit(1)

        print("[5] 增量同步（不推进 backfill 游标之外的新邮件重复投影）")
        incremental = client.post(
            f"{MAILHUB_URL}/v1/mail/connections/{connection_id}/sync-jobs",
            headers={
                **mail_headers,
                "Idempotency-Key": f"{idempotency_key}:incremental",
            },
            json={"mode": "incremental", "folder_ref": "INBOX", "limit": 50},
        )
        if incremental.status_code != 200:
            _fail("enqueue incremental", incremental)
        incremental_job = _data(incremental)
        run = client.post(
            f"{MAILHUB_URL}/v1/mail/sync-jobs/{incremental_job['job_ref']}:run",
            headers=mail_headers,
        )
        if run.status_code != 200:
            _fail("run incremental", run)
        incremental_done = wait_for_job(
            client, mail_headers, str(incremental_job["job_ref"])
        )
        print(
            f"[5] incremental {incremental_done.get('status')}: "
            f"saved={incremental_done.get('saved_count')} "
            f"duplicate={incremental_done.get('duplicate_count')}"
        )

        print("[6] 投影检查")
        threads = client.get(f"{MAILHUB_URL}/v1/mail/threads", headers=mail_headers)
        if threads.status_code != 200:
            _fail("list threads", threads)
        thread_items = _json(threads).get("data", [])
        print(f"[6] threads={len(thread_items)}")
        messages = client.get(f"{MAILHUB_URL}/v1/mail/messages", headers=mail_headers)
        message_items = (
            _json(messages).get("data", []) if messages.status_code == 200 else []
        )
        print(f"[6] messages={len(message_items)}")
        for item in message_items[:5]:
            print(
                f"    - {item.get('subject', '(no subject)')[:60]} "
                f"from={item.get('sender_address')}"
            )

        analyzed = []
        if message_items and not args.skip_analyze:
            print("[7] 智能分析（规则模式）")
            for item in message_items[:3]:
                response = client.post(
                    f"{MAILHUB_URL}/v1/mail/messages/{item['message_id']}:analyze",
                    headers=mail_headers,
                )
                if response.status_code != 200:
                    _fail("analyze message", response)
                result = _data(response)
                analyzed.append(result)
                print(
                    f"    - {item.get('subject', '(no subject)')[:50]}: "
                    f"candidates={len(result.get('candidates', []))}"
                )

        evidence = {
            "schema_version": "local.b4_imap.v1",
            "environment": "test",
            "provider": "imap_smtp",
            "tenant_id": TENANT,
            "subject_id": SUBJECT,
            "connection_id": connection_id,
            "username_sha256": _sha256(username),
            "backfill": {
                "job_ref": first_job_ref,
                "status": finished.get("status"),
                "fetched_count": finished.get("fetched_count"),
                "saved_count": finished.get("saved_count"),
                "filters": {
                    key: sync_body[key]
                    for key in ("received_after", "received_before")
                    if key in sync_body
                },
            },
            "incremental": {
                "job_ref": str(incremental_job.get("job_ref")),
                "status": incremental_done.get("status"),
                "saved_count": incremental_done.get("saved_count"),
                "duplicate_count": incremental_done.get("duplicate_count"),
            },
            "projection": {
                "threads": len(thread_items),
                "messages": len(message_items),
            },
            "analysis": analyzed,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        state_path = Path(__file__).resolve().parents[1] / "b4-imap-state.json"
        state_path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nIMAP 走查完成，证据写入 {state_path}")
        print("说明：这是 IMAP 传输的本地工程证据，不构成 Gmail REST (M2) 激活证据。")
    return 0


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
