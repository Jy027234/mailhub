"""B4 daily autonomy cycle: sync -> AI analysis -> today's candidate digest.

Runs one recommend-only autonomy cycle against the real mailbox through the
durable graph.  The cycle never sends or applies anything: every output is a
reviewable candidate (task / knowledge / follow-up).  Requires the local host
(with or without the model gateway), the durable MailHub graph and an active
connection.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

MAILHUB_URL = "http://127.0.0.1:8000"
TENANT = "b4-tenant"
SUBJECT = "b4-user"


def _api_auth_header() -> dict[str, str]:
    token = os.environ.get("MAILHUB_API_AUTH_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


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
    load_env()
    headers = {
        "X-MailHub-Tenant": TENANT,
        "X-MailHub-Subject": SUBJECT,
        **_api_auth_header(),
    }
    replay_key = f"b4-autonomy-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

    with httpx.Client(timeout=900.0, follow_redirects=False) as client:
        connections = client.get(f"{MAILHUB_URL}/v1/mail/connections", headers=headers)
        if connections.status_code != 200:
            _fail("list connections", connections)
        active = [
            item
            for item in _json(connections).get("data", [])
            if item.get("status") == "active"
        ]
        if not active:
            print("[FAIL] 没有 active 连接")
            return 2
        connection_id = str(active[0]["connection_id"])

        print("[1] 创建自主运行（recommend_only）")
        created = client.post(
            f"{MAILHUB_URL}/v1/mail/autonomy/runs",
            headers=headers,
            json={
                "connection_id": connection_id,
                "limit": 10,
                "message_limit": 10,
                "replay_key": replay_key,
            },
        )
        if created.status_code != 202:
            _fail("create autonomy run", created)
        run_id = str(_data(created)["run_id"])
        print(f"[1] run={run_id}")

        print("[2] 执行自主周期（同步→分析→候选）")
        run = client.post(
            f"{MAILHUB_URL}/v1/mail/autonomy/runs/{run_id}:run", headers=headers
        )
        if run.status_code != 200:
            _fail("run autonomy", run)
        run_data = _data(run)
        print(
            f"[2] status={run_data.get('status')} "
            f"analyzed={len(run_data.get('analyzed_message_ids', []))} "
            f"candidates={len(run_data.get('candidate_ids', []))}"
        )

        print("[3] 今日候选清单")
        candidates = client.get(f"{MAILHUB_URL}/v1/mail/candidates", headers=headers)
        if candidates.status_code != 200:
            _fail("list candidates", candidates)
        items = [
            item
            for item in _json(candidates).get("data", [])
            if item.get("status") == "proposed"
        ]
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_type.setdefault(str(item.get("candidate_type")), []).append(item)
        for candidate_type, group in sorted(by_type.items()):
            print(f"  [{candidate_type}] {len(group)} 条")
            for item in group[:5]:
                payload = item.get("payload", {})
                title = payload.get("title") or payload.get("summary") or ""
                confidence = item.get("confidence")
                print(
                    f"    - {str(title)[:70]} (confidence={confidence}, "
                    f"requires_review={item.get('requires_review')})"
                )

        evidence = {
            "schema_version": "local.b4_autonomy.v1",
            "environment": "test",
            "provider": "imap_smtp",
            "connection_id": connection_id,
            "run_id": run_id,
            "replay_key": replay_key,
            "status": run_data.get("status"),
            "analyzed_count": len(run_data.get("analyzed_message_ids", [])),
            "candidate_count": len(run_data.get("candidate_ids", [])),
            "proposed_candidates": len(items),
            "model_ref": run_data.get("model_ref"),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        path = Path(__file__).resolve().parents[1] / "b4-autonomy-state.json"
        path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n自主周期完成，证据写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
