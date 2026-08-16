"""B4 full-history backfill: bounded weekly windows, oldest to newest.

Each window is one durable backfill sync job (mode=backfill, folder INBOX,
date bounds, limit 100).  Backfill jobs never advance the incremental cursor,
and projection identity is deterministic, so windows may overlap safely.
Usage:

    python scripts/b4_backfill.py --start 2025-01-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

MAILHUB_URL = "http://127.0.0.1:8000"
TENANT = "b4-tenant"
SUBJECT = "b4-user"
WINDOW_DAYS = 7


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=None, help="回填起始日期，如 2025-01-01")
    args = parser.parse_args()
    load_env()
    headers = {"X-MailHub-Tenant": TENANT, "X-MailHub-Subject": SUBJECT}

    start = datetime.fromisoformat(args.start) if args.start else datetime(2025, 1, 1)
    end = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    totals = {"fetched": 0, "saved": 0, "duplicate": 0, "windows": 0, "failed": 0}
    state_path = Path(__file__).resolve().parents[1] / "b4-backfill-state.json"

    with httpx.Client(timeout=180.0, follow_redirects=False) as client:
        connections = client.get(f"{MAILHUB_URL}/v1/mail/connections", headers=headers)
        if connections.status_code != 200:
            print(f"[FAIL] list connections: {connections.status_code}")
            return 2
        active = [
            item
            for item in _json(connections).get("data", [])
            if item.get("status") == "active"
        ]
        if not active:
            print("[FAIL] 没有 active 连接")
            return 2
        connection_id = str(active[0]["connection_id"])

        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=WINDOW_DAYS), end)
            key = f"b4-backfill-{cursor:%Y%m%d}"
            totals["windows"] += 1
            job = client.post(
                f"{MAILHUB_URL}/v1/mail/connections/{connection_id}/sync-jobs",
                headers={**headers, "Idempotency-Key": key},
                json={
                    "mode": "backfill",
                    "folder_ref": "INBOX",
                    "limit": 100,
                    "received_after": f"{cursor:%Y-%m-%d}T00:00:00Z",
                    "received_before": f"{window_end:%Y-%m-%d}T00:00:00Z",
                },
            )
            if job.status_code != 200:
                print(f"[FAIL] {key} enqueue: {job.status_code} {job.text[:200]}")
                totals["failed"] += 1
                cursor = window_end
                continue
            job_ref = str(_data(job)["job_ref"])
            run = client.post(
                f"{MAILHUB_URL}/v1/mail/sync-jobs/{job_ref}:run", headers=headers
            )
            if run.status_code != 200:
                print(f"[FAIL] {key} run: {run.status_code}")
                totals["failed"] += 1
                cursor = window_end
                continue
            finished = _data(run)
            if finished.get("status") != "succeeded":
                print(
                    f"[FAIL] {key}: status={finished.get('status')} "
                    f"error={finished.get('error_code')}"
                )
                totals["failed"] += 1
                cursor = window_end
                continue
            fetched = int(finished.get("fetched_count", 0))
            saved = int(finished.get("saved_count", 0))
            dup = int(finished.get("duplicate_count", 0))
            totals["fetched"] += fetched
            totals["saved"] += saved
            totals["duplicate"] += dup
            print(
                f"[OK] {cursor:%Y-%m-%d}..{window_end:%Y-%m-%d}: "
                f"fetched={fetched} saved={saved} dup={dup} "
                f"(累计 saved={totals['saved']})"
            )
            state_path.write_text(
                json.dumps({**totals, "window": f"{cursor:%Y-%m-%d}"}, indent=2),
                encoding="utf-8",
            )
            cursor = window_end
            time.sleep(1)

    print(f"\n回填完成: {json.dumps(totals, ensure_ascii=False)}")
    print(f"进度证据: {state_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
