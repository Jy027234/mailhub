"""Run the deterministic, synthetic intelligence contract evaluation.

The evaluator is deliberately limited to the repository-authored dataset. It
never accepts a mailbox export or prints message bodies. A release gate can use
the JSON result as a small regression signal; it is not a substitute for a
redacted production-quality offline evaluation or model canary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from mailhub.domain import MailMessageProjection, digest_text
from mailhub.intelligence import analyze_message


def evaluate_dataset(path: Path) -> dict[str, object]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not rows:
        raise ValueError("synthetic_eval_dataset_empty")
    field_hits: dict[str, int] = {
        "project_refs": 0,
        "task_refs": 0,
        "date_refs": 0,
        "injection_detected": 0,
        "knowledge_candidate": 0,
        "requires_review": 0,
    }
    field_total: dict[str, int] = {key: 0 for key in field_hits}
    field_cases: dict[str, int] = {key: 0 for key in field_hits}
    exact_matches: dict[str, int] = {key: 0 for key in field_hits}
    case_results: list[dict[str, object]] = []

    for row in rows:
        case_id = _required_text(row, "case_id")
        expected = row.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"synthetic_eval_expected_invalid:{case_id}")
        message = _message_for_case(case_id, row)
        result = analyze_message(message)
        actual = {
            "project_refs": list(result.project_refs),
            "task_refs": list(result.task_refs),
            "date_refs": list(result.date_refs),
            "injection_detected": result.injection_detected,
            "knowledge_candidate": result.knowledge_candidate is not None,
            "requires_review": result.injection_detected
            or bool(result.action_candidates)
            or result.knowledge_candidate is not None,
        }
        comparisons: dict[str, bool] = {}
        for key in field_hits:
            if key in _BOOLEAN_FIELDS and key not in expected:
                continue
            field_cases[key] += 1
            expected_value = expected.get(key, False if key in _BOOLEAN_FIELDS else [])
            if key in _BOOLEAN_FIELDS:
                expected_value = bool(expected_value)
                actual_value = bool(actual[key])
                comparisons[key] = expected_value == actual_value
                field_total[key] += 1
            else:
                expected_items = tuple(str(item) for item in _as_sequence(expected_value))
                actual_items = tuple(str(item) for item in cast(Iterable[object], actual[key]))
                expected_set = set(expected_items)
                actual_set = set(actual_items)
                field_total[key] += len(expected_set)
                field_hits[key] += len(expected_set & actual_set)
                comparisons[key] = expected_set == actual_set
                exact_matches[key] += int(comparisons[key])
                continue
            field_hits[key] += int(comparisons[key])
            exact_matches[key] += int(comparisons[key])
        case_results.append({"case_id": case_id, "matches": comparisons})

    metrics = {
        key: {
            "micro_recall": round(field_hits[key] / field_total[key], 4)
            if field_total[key]
            else 1.0,
            "exact_match_rate": round(exact_matches[key] / field_cases[key], 4)
            if field_cases[key]
            else 1.0,
            "expected_count": field_total[key],
            "asserted_cases": field_cases[key],
        }
        for key in field_hits
    }
    return {
        "schema_version": "mailhub.synthetic_eval.v1",
        "dataset_sha256": _sha256(path),
        "cases": len(rows),
        "metrics": metrics,
        "all_cases_match": all(
            all(bool(match) for match in cast(Mapping[str, bool], result["matches"]).values())
            for result in case_results
        ),
        "case_results": case_results,
    }


_BOOLEAN_FIELDS = frozenset({"injection_detected", "knowledge_candidate", "requires_review"})


def _message_for_case(case_id: str, row: Mapping[str, object]) -> MailMessageProjection:
    body = _required_text(row, "body")
    subject = _required_text(row, "subject")
    return MailMessageProjection(
        message_id=uuid5(NAMESPACE_URL, f"mailhub.synthetic:{case_id}"),
        tenant_id="synthetic-tenant",
        connection_id=uuid5(NAMESPACE_URL, "mailhub.synthetic:connection"),
        thread_id=uuid5(NAMESPACE_URL, f"mailhub.synthetic:thread:{case_id}"),
        provider_message_ref=f"synthetic:{case_id}",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject=subject,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        body_text=body,
        content_sha256=digest_text(body),
    )


def _as_sequence(value: object) -> Iterable[object]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    return ()


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"synthetic_eval_{key}_invalid")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).parents[1] / "evals" / "synthetic_mail_eval.jsonl",
    )
    parser.add_argument("--json", action="store_true", help="emit the full JSON report")
    args = parser.parse_args(argv)
    report = evaluate_dataset(args.dataset)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        metrics = cast(Mapping[str, Mapping[str, object]], report["metrics"])
        print(
            f"synthetic_eval cases={report['cases']} all_cases_match={report['all_cases_match']} "
            f"injection_recall={metrics['injection_detected']['micro_recall']} "
            f"knowledge_exact={metrics['knowledge_candidate']['exact_match_rate']}"
        )
    return 0 if bool(report["all_cases_match"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
