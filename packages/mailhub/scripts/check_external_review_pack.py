"""Fail-closed check of the external review pack.

The pack lists what a security, SRE, privacy or release owner has to sign off
before this module is used in production, and what a reviewer can read today.

Nothing in this repository can perform those reviews, so the risk is not that a
review is missing -- it is that the pack quietly reads as though one happened.

This check exists for that: an entry may not claim to be signed without a named
signer and a date, an entry may not be blocked without saying what the reviewer
has to do, a claimed piece of evidence must exist on disk, and the required set
of items may not shrink.

    python scripts/check_external_review_pack.py
    python scripts/check_external_review_pack.py \\
        --json ../../docs/reports/mailhub-external-review-pack.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
PACK = PACKAGE_ROOT / "docs" / "external-review-pack.yaml"
SCHEMA = "mailhub.external_review_pack.v1"

#: Every item the module must answer for.  Removing one is a failure, not a
#: reduction in scope: silence about a review is how it gets forgotten.
REQUIRED_IDS: tuple[str, ...] = (
    "MAIL-SEC-001",
    "MAIL-SEC-002",
    "MAIL-SEC-003",
    "MAIL-SEC-004",
    "MAIL-SEC-005",
    "MAIL-SEC-006",
    "MAIL-SEC-007",
    "MAIL-OPS-001",
    "MAIL-OPS-002",
    "MAIL-OPS-003",
    "MAIL-OPS-004",
    "MAIL-OPS-005",
    "MAIL-OPS-006",
    "MAIL-REL-001",
    "MAIL-REL-006",
    "MAIL-REL-007",
    "MAIL-REL-008",
    "MAIL-REL-009",
    "MAIL-REL-011",
)

STATUSES = ("not_started", "blocked_on_external", "evidence_ready", "signed_off")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _exists(path: str) -> bool:
    candidate = Path(path)
    resolved = candidate if candidate.is_absolute() else REPO_ROOT / candidate
    return resolved.is_file()


def check_pack(pack: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (issues, per-entry summaries)."""

    issues: list[str] = []
    if pack.get("schema") != SCHEMA:
        return (["schema_mismatch"], [])
    entries = pack.get("reviews")
    if not isinstance(entries, list) or not entries:
        return (["reviews_missing"], [])

    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            issues.append("entry_not_an_object")
            continue
        identifier = str(entry.get("id", ""))
        if not identifier:
            issues.append("entry_without_an_id")
            continue
        if identifier in seen:
            issues.append("duplicate_entry:" + identifier)
            continue
        seen.add(identifier)

        status = str(entry.get("status", ""))
        if status not in STATUSES:
            issues.append(identifier + ":unknown_status:" + status)
            continue

        evidence = entry.get("local_evidence") or []
        if not isinstance(evidence, list):
            issues.append(identifier + ":local_evidence_not_a_list")
            evidence = []
        for path in evidence:
            if not isinstance(path, str) or not _exists(path):
                issues.append(identifier + ":evidence_missing:" + str(path))

        actions = entry.get("reviewer_actions") or []
        if not isinstance(actions, list):
            issues.append(identifier + ":reviewer_actions_not_a_list")
            actions = []

        if status == "signed_off":
            signer = entry.get("signed_by")
            signed_at = entry.get("signed_at")
            if not isinstance(signer, str) or not signer.strip():
                issues.append(identifier + ":signed_without_a_signer")
            if not isinstance(signed_at, str) or not _DATE_RE.fullmatch(signed_at):
                issues.append(identifier + ":signed_without_a_date")
        if status in {"blocked_on_external", "not_started"} and not actions:
            issues.append(identifier + ":blocked_without_saying_what_is_needed")
        if status == "evidence_ready" and not evidence:
            issues.append(identifier + ":claims_evidence_but_lists_none")
        if status == "not_started" and evidence:
            issues.append(identifier + ":not_started_but_lists_evidence")

        summaries.append(
            {
                "id": identifier,
                "owner": str(entry.get("owner", "")),
                "title": str(entry.get("title", "")),
                "status": status,
                "evidence_count": len(evidence),
                "reviewer_actions": [str(action) for action in actions],
            }
        )

    for missing in REQUIRED_IDS:
        if missing not in seen:
            issues.append("entry_missing:" + missing)
    return (issues, summaries)


def summarise(summaries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(STATUSES, 0)
    for entry in summaries:
        counts[str(entry["status"])] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=PACK)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    if not args.pack.is_file():
        print("review_pack_missing: " + str(args.pack))
        return 1
    loaded: Any = yaml.safe_load(args.pack.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        print("review_pack_not_a_mapping")
        return 1

    issues, summaries = check_pack(loaded)
    counts = summarise(summaries)
    bundle = {
        "schema": "mailhub.external_review_pack_check.v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "pack": str(args.pack.relative_to(REPO_ROOT)),
        "entries": summaries,
        "counts": counts,
        "signed": counts["signed_off"],
        "issues": issues,
        "passed": not issues,
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8"
        )

    if issues:
        print("external-review-pack: FAILED")
        for issue in issues:
            print("  - " + issue)
        return 1
    print(
        "external-review-pack: ok ("
        + str(len(summaries))
        + " items: "
        + str(counts["evidence_ready"])
        + " evidence_ready, "
        + str(counts["blocked_on_external"])
        + " blocked_on_external, "
        + str(counts["not_started"])
        + " not_started, "
        + str(counts["signed_off"])
        + " signed)"
    )
    if counts["signed_off"] == 0:
        print(
            "  note: no external review has been signed; this pack only records "
            "what a reviewer can read today"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
