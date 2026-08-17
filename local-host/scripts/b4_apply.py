"""B4 candidate review/apply: the human confirmation step of the daily loop.

`review` marks a candidate approved/rejected; `apply` executes the approved
candidate against the host action/knowledge ledgers through the approval port
(the local host's single-user confirmation).  Applying without review or
without a confirmation fails closed.  Usage:

    python scripts/b4_apply.py --candidate <id>          # 批准并应用
    python scripts/b4_apply.py --candidate <id> --reject # 拒绝
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

HOST_URL = "http://127.0.0.1:8090"
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="候选 ID")
    parser.add_argument("--reject", action="store_true", help="拒绝而不是批准")
    parser.add_argument("--reason", default="digest_review", help="审核备注")
    args = parser.parse_args()
    load_env()

    host_headers = {
        "Authorization": f"Bearer {os.environ['MAILHUB_HOST_SERVICE_TOKEN']}"
    }
    mail_headers = {
        "X-MailHub-Tenant": TENANT,
        "X-MailHub-Subject": SUBJECT,
        **_api_auth_header(),
    }

    with httpx.Client(timeout=90.0, follow_redirects=False) as client:
        candidate_id = args.candidate
        current = client.get(
            f"{MAILHUB_URL}/v1/mail/candidates/{candidate_id}", headers=mail_headers
        )
        if current.status_code != 200:
            _fail("get candidate", current)
        candidate = _data(current)
        revision = int(candidate["revision"])
        candidate_type = str(candidate.get("candidate_type"))
        print(
            f"[1] candidate={candidate_id} type={candidate_type} "
            f"status={candidate.get('status')} revision={revision}"
        )

        if args.reject:
            print("[2] 拒绝候选")
            reviewed = client.post(
                f"{MAILHUB_URL}/v1/mail/candidates/{candidate_id}:review",
                headers=mail_headers,
                json={
                    "approved": False,
                    "expected_revision": revision,
                    "review_reason": args.reason,
                },
            )
            if reviewed.status_code != 200:
                _fail("review", reviewed)
            print(f"[2] status={_data(reviewed).get('status')}")
            return 0

        print("[2] 宿主确认（单用户本地审批）")
        approval = client.post(
            f"{HOST_URL}/v1/mail-host/approvals/request",
            headers=host_headers,
            json={"tenant_id": TENANT, "subject_id": SUBJECT},
        )
        if approval.status_code != 200:
            _fail("approval request", approval)
        approval_ref = _json(approval)["confirmation_ref"]

        print("[3] 批准候选")
        reviewed = client.post(
            f"{MAILHUB_URL}/v1/mail/candidates/{candidate_id}:review",
            headers=mail_headers,
            json={
                "approved": True,
                "expected_revision": revision,
                "review_reason": args.reason,
            },
        )
        if reviewed.status_code != 200:
            _fail("review", reviewed)
        reviewed_data = _data(reviewed)
        print(f"[3] status={reviewed_data.get('status')}")

        print("[4] 应用候选（写入宿主账本）")
        applied = client.post(
            f"{MAILHUB_URL}/v1/mail/candidates/{candidate_id}:apply",
            headers=mail_headers,
            json={"approval_ref": approval_ref},
        )
        if applied.status_code != 200:
            _fail("apply", applied)
        applied_data = _data(applied)
        print(f"[4] status={applied_data.get('status')}")
        evidence = applied_data.get("payload", {}).get("application_result")
        if evidence:
            print(f"[4] 宿主账本证据: {json.dumps(evidence, ensure_ascii=False)[:300]}")
        print(
            "    - 任务/项目候选 → 宿主动作账本 (host_actions)；知识候选 → 宿主知识账本 "
            "(host_knowledge)，均幂等可审计。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
