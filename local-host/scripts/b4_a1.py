"""B4 A1 walk: real Gmail authorization, refresh, replay rejection and revoke.

Requires the local host and the durable MailHub graph to be running (see
scripts/run_host.ps1 / scripts/run_mailhub.ps1) and a Google Cloud OAuth
client configured in .env.  The script drives the browser consent through the
host's /oauth/gmail/start page, then performs the A1 observations against
MailHub.  It never touches provider tokens; those remain inside the host
credential broker.
"""

from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
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


def mailhub_headers() -> dict[str, str]:
    return {"X-MailHub-Tenant": TENANT, "X-MailHub-Subject": SUBJECT}


def _json(response: httpx.Response) -> dict[str, Any]:
    value: Any = response.json()
    return value if isinstance(value, dict) else {}


def list_connections(client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(
        f"{MAILHUB_URL}/v1/mail/connections", headers=mailhub_headers()
    )
    if response.status_code != 200:
        print(f"list connections failed: {response.status_code} {response.text[:300]}")
        sys.exit(1)
    data = _json(response).get("data", [])
    return [item for item in data if isinstance(item, dict)]


def wait_for_connection(
    client: httpx.Client, known: set[str], timeout: int = 600
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for connection in list_connections(client):
            connection_id = str(connection.get("connection_id", ""))
            if (
                connection_id
                and connection_id not in known
                and connection.get("provider") == "gmail"
            ):
                if connection.get("status") == "active":
                    return connection
                print(
                    f"connection {connection_id} status={connection.get('status')} ..."
                )
        time.sleep(5)
    print(f"timed out after {timeout}s waiting for an active gmail connection")
    sys.exit(1)


def open_consent() -> None:
    start_url = f"{HOST_URL}/oauth/gmail/start?tenant={TENANT}&subject={SUBJECT}"
    print(f"\n[1] 在浏览器完成 Gmail 授权: {start_url}")
    webbrowser.open(start_url)


def refresh_connection(
    client: httpx.Client, connection: dict[str, Any]
) -> dict[str, Any]:
    connection_id = str(connection["connection_id"])
    revision = int(connection["revision"])
    response = client.post(
        f"{MAILHUB_URL}/v1/mail/connections/{connection_id}:refresh",
        headers=mailhub_headers(),
        json={"expected_revision": revision, "reason": "b4_activation_refresh"},
    )
    if response.status_code != 200:
        print(f"refresh failed: {response.status_code} {response.text[:400]}")
        sys.exit(1)
    refreshed = _json(response).get("data", {})
    print(f"[2] refresh 完成: credential_version={refreshed.get('credential_version')}")
    return refreshed if isinstance(refreshed, dict) else connection


def revoke_connection(client: httpx.Client, connection: dict[str, Any]) -> None:
    connection_id = str(connection["connection_id"])
    revision = int(connection["revision"])
    response = client.post(
        f"{MAILHUB_URL}/v1/mail/connections/{connection_id}:revoke",
        headers=mailhub_headers(),
        json={"expected_revision": revision},
    )
    if response.status_code != 200:
        print(f"revoke failed: {response.status_code} {response.text[:400]}")
        sys.exit(1)
    revoked = _json(response).get("data", {})
    print(f"[3] revoke 完成: status={revoked.get('status')}")


def main() -> int:
    load_env()
    if os.environ.get("HOST_GMAIL_ENABLED", "").strip().casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(
            "HOST_GMAIL_ENABLED 不是 true；先在 .env 中启用 Gmail 并填入 OAuth 客户端"
        )
        return 2

    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        ready = client.get(f"{MAILHUB_URL}/health/ready")
        if ready.status_code != 200 or _json(ready).get("status") != "ready":
            print(f"MailHub not ready: {ready.status_code} {ready.text[:300]}")
            return 2

        known = {
            str(item.get("connection_id", "")) for item in list_connections(client)
        }
        open_consent()
        first = wait_for_connection(client, known)
        known.add(str(first["connection_id"]))
        print(f"[1] A1 授权完成: connection={first['connection_id']}")

        input(
            "\n请在回调成功页点击【故意重放同一 state】链接，回车确认已生成拒绝证据: "
        )

        first = refresh_connection(client, first)
        revoke_connection(client, first)

        print("\n[4] 第二次授权（为 A2 只读同步准备活动连接）")
        open_consent()
        second = wait_for_connection(client, known)
        known.add(str(second["connection_id"]))
        print(f"[4] A2 连接就绪: connection={second['connection_id']}")

        state = {
            "environment": "test",
            "provider": "gmail",
            "tenant_id": TENANT,
            "subject_id": SUBJECT,
            "a1_connection_id": str(first["connection_id"]),
            "a2_connection_id": str(second["connection_id"]),
            "evidence_root": str(
                Path(__file__).resolve().parents[1] / "provider-activation"
            ),
        }
        state_path = Path(__file__).resolve().parents[1] / "b4-state.json"
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"\nA1 阶段完成，状态已写入 {state_path}")
        print("下一步（A2 有界只读同步）:")
        print(
            "  $env:MAILHUB_ACTIVATION_ALLOW_NETWORK='true'; "
            "cd packages/mailhub; python scripts/run_provider_activation.py "
            "--provider gmail --mode backfill --folder-ref INBOX "
            "--api-url http://127.0.0.1:8000 --tenant-id b4-tenant --subject-id b4-user "
            f"--connection-id {second['connection_id']} "
            "--evidence-root ../local-host/provider-activation --confirm-real-provider"
        )
        print(
            "A2 后再运行 collect_oauth_evidence.py 生成 oauth-consent.json，"
            "并用 validate_provider_bundle.py --require-real 校验完整证据包。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
