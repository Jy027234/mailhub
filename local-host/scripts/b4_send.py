"""B4 controlled send test: draft -> host confirmation -> approval-bound outbox.

Requires outbound switches in .env (MAILHUB_OUTBOUND_ENABLED=true,
MAILHUB_SMTP_SEND_ENABLED=true), the local host, the durable MailHub graph
and the local worker (scripts/run_worker.ps1).  Sends one REAL email from
the connected mailbox to itself through the approval-bound outbox, then
polls the durable operation to a terminal state.
"""

from __future__ import annotations

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
        print("local-host/.env not found")
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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subject", default=None, help="邮件主题（缺省用时间戳测试主题）"
    )
    parser.add_argument("--body", default=None, help="邮件正文（缺省用发送测试正文）")
    args = parser.parse_args()
    load_env()
    enabled = os.environ.get("MAILHUB_OUTBOUND_ENABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        print("MAILHUB_OUTBOUND_ENABLED 不是 true；先在 .env 开启出站并重启 MailHub")
        return 2

    host_headers = {
        "Authorization": f"Bearer {os.environ['MAILHUB_HOST_SERVICE_TOKEN']}"
    }
    mail_headers = {"X-MailHub-Tenant": TENANT, "X-MailHub-Subject": SUBJECT}
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        print("[1] 连接与出站开关检查")
        connections = client.get(
            f"{MAILHUB_URL}/v1/mail/connections", headers=mail_headers
        )
        if connections.status_code != 200:
            _fail("list connections", connections)
        active = [
            item
            for item in _json(connections).get("data", [])
            if item.get("status") == "active"
        ]
        if not active:
            print("[FAIL] 没有 active 连接；先跑 b4_imap.py 建连")
            return 2
        connection = active[0]
        connection_id = str(connection["connection_id"])
        mailbox = str(connection["email_address"])
        print(f"[1] connection={connection_id} mailbox={mailbox}")

        print("[2] 宿主确认（本地单用户审批）")
        approval = client.post(
            f"{HOST_URL}/v1/mail-host/approvals/request",
            headers=host_headers,
            json={"tenant_id": TENANT, "subject_id": SUBJECT},
        )
        if approval.status_code != 200:
            _fail("approval request", approval)
        confirmation_ref = _json(approval)["confirmation_ref"]
        print(f"[2] confirmation_ref={confirmation_ref}")

        print("[3] 选择本邮箱参与的线程并创建 Agent 策略/委托")
        threads = client.get(f"{MAILHUB_URL}/v1/mail/threads", headers=mail_headers)
        if threads.status_code != 200:
            _fail("list threads", threads)
        target_thread = next(
            (
                item
                for item in _json(threads).get("data", [])
                if mailbox.casefold()
                in {
                    str(address).casefold()
                    for address in item.get("participant_addresses", [])
                }
            ),
            None,
        )
        if target_thread is None:
            print("[FAIL] 没有本邮箱参与的线程；先跑 b4_imap.py 同步")
            return 2
        thread_id = str(target_thread["thread_id"])
        print(f"[3] thread={thread_id}")

        policy = client.post(
            f"{MAILHUB_URL}/v1/mail/agent-policies",
            headers=mail_headers,
            json={
                "allowed_connection_ids": [connection_id],
                "allowed_actions": ["send_reply"],
                "allowed_domains": [mailbox.rsplit("@", 1)[-1]],
                "thread_only": True,
                "allowed_automation_level": "l3b_bounded_reply",
                "max_per_hour": 5,
                "max_per_day": 10,
                "valid_days": 7,
            },
        )
        if policy.status_code != 201:
            _fail("create policy", policy)
        policy_id = str(_data(policy)["policy_id"])
        grant = client.post(
            f"{MAILHUB_URL}/v1/mail/delegations",
            headers=mail_headers,
            json={
                "policy_id": policy_id,
                "agent_subject_id": SUBJECT,
                "capability_ids": ["send_reply"],
                "valid_days": 7,
            },
        )
        if grant.status_code != 201:
            _fail("create grant", grant)
        grant_id = str(_data(grant)["grant_id"])
        print(f"[3] policy={policy_id} grant={grant_id}")

        print("[4] 创建草稿（线程内回复，发给自己）")
        draft = client.post(
            f"{MAILHUB_URL}/v1/mail/drafts",
            headers={**mail_headers, "Idempotency-Key": f"b4-send-draft-{timestamp}"},
            json={
                "connection_id": connection_id,
                "thread_id": thread_id,
                "recipient_addresses": [mailbox],
                "subject": args.subject or f"MailHub B4 发送测试 {timestamp}",
                "body_text": args.body
                or (
                    "这是一封由 MailHub 受控出站链路发送的测试邮件：\n"
                    "草稿 -> 宿主确认 -> 审批绑定出站 -> SMTP 发送。\n"
                    "如收到本邮件，说明发送闭环正常。"
                ),
            },
        )
        if draft.status_code != 201:
            _fail("create draft", draft)
        draft_data = _data(draft)
        draft_id = str(draft_data["draft_id"])
        revision = int(draft_data["revision"])
        content_sha256 = str(draft_data["content_sha256"])
        recipient_digest = str(draft_data["recipient_digest"])
        print(f"[4] draft={draft_id} revision={revision}")

        print("[5] 审批绑定发送入队（策略+委托+确认）")
        queued = client.post(
            f"{MAILHUB_URL}/v1/mail/drafts/{draft_id}:send",
            headers={**mail_headers, "Idempotency-Key": f"b4-send-{timestamp}"},
            json={
                "expected_revision": revision,
                "expected_content_sha256": content_sha256,
                "expected_recipient_digest": recipient_digest,
                "confirmation_ref": confirmation_ref,
                "policy_id": policy_id,
                "grant_id": grant_id,
                "agent_subject_id": SUBJECT,
            },
        )
        if queued.status_code != 200:
            _fail("queue draft send", queued)
        queued_data = _data(queued)
        operation_id = str(queued_data["operation_id"])
        print(
            f"[5] operation={operation_id} status={queued_data.get('status')} "
            f"decision={queued_data.get('decision')}"
        )

        print("[6] 等待本地 worker 发送（轮询 durable operation）")
        deadline = time.monotonic() + 180
        final: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = client.get(
                f"{MAILHUB_URL}/v1/mail/actions/{operation_id}", headers=mail_headers
            )
            if response.status_code != 200:
                _fail("poll operation", response)
            final = _data(response)
            status_value = final.get("status")
            print(f"    status={status_value} error_code={final.get('error_code')}")
            if status_value in {
                "succeeded",
                "dead_letter",
                "outcome_unknown",
                "failed",
            }:
                break
            time.sleep(5)
        if not final or final.get("status") != "succeeded":
            print(f"[FAIL] 发送未成功: {json.dumps(final, ensure_ascii=False)[:400]}")
            return 1
        print("[6] 发送成功")

        evidence = {
            "schema_version": "local.b4_send.v1",
            "environment": "test",
            "provider": "imap_smtp",
            "connection_id": connection_id,
            "thread_id": thread_id,
            "draft_id": draft_id,
            "operation_id": operation_id,
            "policy_id": policy_id,
            "grant_id": grant_id,
            "confirmation_ref_sha256": _sha256(confirmation_ref),
            "subject": f"MailHub B4 发送测试 {timestamp}",
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        path = Path(__file__).resolve().parents[1] / "b4-send-state.json"
        path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n发送测试完成，证据写入 {path}")
        print("验证收件：邮件到达后重跑 b4_imap.py 增量同步，应能看到这封测试邮件。")
    return 0


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
