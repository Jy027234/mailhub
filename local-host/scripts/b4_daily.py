"""B4 daily digest: autonomy cycle -> readable report -> review commands.

Runs one recommend-only autonomy cycle, then writes an HTML digest with new
mail, AI-extracted facts and the review queue (with copy-paste commands for
b4_apply.py).  Designed to be scheduled (see scripts/install_daily_task.ps1).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import httpx

MAILHUB_URL = "http://127.0.0.1:8000"
TENANT = "b4-tenant"
SUBJECT = "b4-user"

_STYLE = """
body{font-family:system-ui,'Segoe UI',sans-serif;max-width:52rem;margin:2rem auto;
padding:0 1rem;color:#1a1a1a;line-height:1.6}
h1{border-bottom:2px solid #2563eb;padding-bottom:.4rem}
h2{margin-top:2rem;color:#2563eb}
.card{border:1px solid #e5e7eb;border-radius:8px;padding:.8rem 1rem;margin:.6rem 0;
background:#fafafa}
.meta{color:#6b7280;font-size:.85rem}
.cmd{font-family:Consolas,monospace;background:#1f2937;color:#e5e7eb;padding:.3rem .6rem;
border-radius:4px;font-size:.85rem}
.tag{display:inline-block;background:#dbeafe;color:#1d4ed8;border-radius:4px;
padding:0 .5rem;font-size:.8rem;margin-right:.4rem}
.warn{color:#b45309}
"""


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


def run_autonomy(
    client: httpx.Client, headers: dict[str, str], connection_id: str
) -> dict[str, Any]:
    replay_key = f"daily-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
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
        return {"status": f"enqueue_failed_{created.status_code}"}
    run_id = str(_data(created)["run_id"])
    run = client.post(
        f"{MAILHUB_URL}/v1/mail/autonomy/runs/{run_id}:run", headers=headers
    )
    if run.status_code != 200:
        return {"status": f"run_failed_{run.status_code}", "run_id": run_id}
    return _data(run)


def main() -> int:
    load_env()
    headers = {"X-MailHub-Tenant": TENANT, "X-MailHub-Subject": SUBJECT}
    today = datetime.now(UTC)
    today_str = today.strftime("%Y-%m-%d")

    with httpx.Client(timeout=900.0, follow_redirects=False) as client:
        print("[1] 每日自主周期")
        connections = client.get(f"{MAILHUB_URL}/v1/mail/connections", headers=headers)
        if connections.status_code != 200:
            print(f"[FAIL] connections: {connections.status_code}")
            return 2
        active = [
            item
            for item in _json(connections).get("data", [])
            if item.get("status") == "active"
        ]
        if not active:
            print("[FAIL] 没有 active 连接；先跑 b4_imap.py")
            return 2
        cycle = run_autonomy(client, headers, str(active[0]["connection_id"]))
        print(
            f"[1] status={cycle.get('status')} "
            f"analyzed={len(cycle.get('analyzed_message_ids', []))} "
            f"candidates={len(cycle.get('candidate_ids', []))}"
        )

        print("[2] 收集今日数据")
        messages = client.get(
            f"{MAILHUB_URL}/v1/mail/messages?limit=200", headers=headers
        )
        message_items = (
            _json(messages).get("data", []) if messages.status_code == 200 else []
        )
        today_messages = [
            item
            for item in message_items
            if str(item.get("received_at", ""))[:10] == today_str
        ]
        candidates = client.get(f"{MAILHUB_URL}/v1/mail/candidates", headers=headers)
        candidate_items = (
            _json(candidates).get("data", []) if candidates.status_code == 200 else []
        )
        proposed = [
            item for item in candidate_items if item.get("status") == "proposed"
        ]
        decided = [
            item
            for item in candidate_items
            if item.get("status") in {"applied", "rejected"}
            and str(item.get("updated_at", ""))[:10] == today_str
        ]

        facts: dict[str, list[str]] = {
            "risks": [],
            "commitments": [],
            "decisions": [],
            "due_dates": [],
        }
        # Facts come from the cycle's candidate payloads (no extra model calls).
        for item in proposed + decided:
            payload = item.get("payload", {})
            facts["risks"].extend(str(risk) for risk in payload.get("risks", []))
            facts["commitments"].extend(
                str(commit) for commit in payload.get("commitments", [])
            )
            facts["decisions"].extend(
                str(decision) for decision in payload.get("decisions", [])
            )
            facts["due_dates"].extend(
                str(date) for date in payload.get("due_date_refs", [])
            )

        print("[3] 生成摘要报告")
        rows = []
        rows.append("<h1>MailHub 每日摘要</h1>")
        rows.append(
            f'<p class="meta">生成时间: {today.strftime("%Y-%m-%d %H:%M")} UTC · '
            f"自主周期: {escape(str(cycle.get('status')))}</p>"
        )
        rows.append(f"<h2>今日新邮件（{len(today_messages)} 封）</h2>")
        for item in today_messages[:20]:
            rows.append(
                f'<div class="card"><strong>{escape(str(item.get("subject", "(无主题)")))}</strong>'
                f'<br><span class="meta">来自 {escape(str(item.get("sender_address")))}</span></div>'
            )
        if not today_messages:
            rows.append('<p class="meta">今日没有新邮件。</p>')

        rows.append("<h2>AI 提取的事实</h2>")
        labels = {
            "risks": "风险",
            "commitments": "承诺",
            "decisions": "决定",
            "due_dates": "截止日期",
        }
        for key, label in labels.items():
            values = list(dict.fromkeys(facts[key]))[:10]
            if values:
                rows.append(
                    f"<p><span class='tag'>{label}</span> "
                    + "；".join(escape(v) for v in values)
                    + "</p>"
                )
        if not any(facts.values()):
            rows.append('<p class="meta">今日邮件中没有提取到风险/承诺/决定。</p>')

        rows.append(f"<h2>待审核候选（{len(proposed)} 条）</h2>")
        for item in proposed:
            payload = item.get("payload", {})
            title = str(payload.get("title") or payload.get("summary") or "(无标题)")[
                :80
            ]
            candidate_type = str(item.get("candidate_type"))
            candidate_id = str(item.get("candidate_id"))
            rows.append(
                f'<div class="card"><span class="tag">{escape(candidate_type)}</span>'
                f"<strong>{escape(title)}</strong>"
                f'<br><span class="meta">confidence={item.get("confidence")}</span>'
                f'<br><span class="cmd">python scripts\\b4_apply.py --candidate {candidate_id}</span>'
                f'　<span class="cmd">python scripts\\b4_apply.py --candidate {candidate_id} --reject</span>'
                "</div>"
            )
        if not proposed:
            rows.append('<p class="meta">没有待审核候选。</p>')

        rows.append(f"<h2>今日已处理（{len(decided)} 条）</h2>")
        for item in decided[:20]:
            payload = item.get("payload", {})
            title = str(payload.get("title") or payload.get("summary") or "(无标题)")[
                :60
            ]
            rows.append(
                f'<div class="card"><span class="tag">{escape(str(item.get("status")))}</span>'
                f"{escape(title)}</div>"
            )

        reports_dir = Path(__file__).resolve().parents[1] / "reports"
        reports_dir.mkdir(exist_ok=True)
        report_path = reports_dir / f"daily-{today_str}.html"
        report_path.write_text(
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>MailHub 每日摘要 {today_str}</title><style>{_STYLE}</style></head>"
            f"<body>{''.join(rows)}</body></html>",
            encoding="utf-8",
        )
        print(f"[3] 报告: {report_path}")
        print(
            f"摘要: 新邮件 {len(today_messages)} · 待审核候选 {len(proposed)} · "
            f"今日已处理 {len(decided)} · 自主周期 {cycle.get('status')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
