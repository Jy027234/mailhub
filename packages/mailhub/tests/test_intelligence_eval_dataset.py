from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mailhub.domain import MailMessageProjection, digest_text
from mailhub.intelligence import analyze_message
from scripts.evaluate_synthetic import evaluate_dataset


def test_synthetic_intelligence_dataset_is_safe_and_reproducible() -> None:
    path = Path(__file__).parents[1] / "evals" / "synthetic_mail_eval.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 8
    assert all(
        "synthetic" in row["body"].casefold() or row["case_id"] != "knowledge_value_007"
        for row in rows
    )

    injection_cases = 0
    knowledge_cases = 0
    for row in rows:
        body = str(row["body"])
        message = MailMessageProjection(
            message_id=uuid4(),
            tenant_id="synthetic-tenant",
            connection_id=uuid4(),
            thread_id=uuid4(),
            provider_message_ref=f"synthetic:{row['case_id']}",
            internet_message_id=None,
            sender_address="sender@example.test",
            recipient_addresses=("user@example.test",),
            subject=str(row["subject"]),
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            body_text=body,
            content_sha256=digest_text(body),
        )
        result = analyze_message(message)
        expected = row["expected"]
        assert result.injection_detected is bool(expected["injection_detected"])
        assert result.project_refs == tuple(expected.get("project_refs", []))
        assert result.task_refs == tuple(expected.get("task_refs", []))
        assert result.date_refs == tuple(expected.get("date_refs", []))
        if expected["injection_detected"]:
            injection_cases += 1
            assert result.knowledge_candidate is None
        if expected.get("knowledge_candidate"):
            knowledge_cases += 1
            assert result.knowledge_candidate is not None
        if expected.get("knowledge_candidate") is False:
            assert result.knowledge_candidate is None
    assert injection_cases == 1
    assert knowledge_cases == 1


def test_synthetic_evaluator_reports_full_contract_match() -> None:
    path = Path(__file__).parents[1] / "evals" / "synthetic_mail_eval.jsonl"
    report = evaluate_dataset(path)
    assert report["schema_version"] == "mailhub.synthetic_eval.v1"
    assert report["cases"] == 8
    assert report["all_cases_match"] is True
