"""Tests for the external review pack and its checker.

Nothing in this repository can perform a security, privacy or SRE review, so the
risk is not a missing review -- it is a pack that reads as though one happened.
These tests pin the rules that prevent that, and then assert the real pack obeys
them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_external_review_pack.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("check_external_review_pack", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _module()

#: A file that exists, so evidence checks exercise the resolution rather than
#: failing for an unrelated reason.
REAL_EVIDENCE = "packages/mailhub/README.md"


def _entry(identifier: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": identifier,
        "kind": "external_review",
        "owner": "security",
        "title": "something a human must review",
        "status": "not_started",
        "local_evidence": [],
        "reviewer_actions": ["do the review"],
    }
    entry.update(overrides)
    return entry


def _pack(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema": CHECK.SCHEMA, "reviews": list(entries)}


def _full_pack(**overrides: Any) -> dict[str, Any]:
    """Every required id, all blocked, so only the injected problem is reported."""

    entries = [_entry(identifier, **overrides) for identifier in CHECK.REQUIRED_IDS]
    return _pack(*entries)


def test_a_complete_pack_with_no_claims_passes() -> None:
    issues, summaries = CHECK.check_pack(_full_pack())
    assert issues == []
    assert len(summaries) == len(CHECK.REQUIRED_IDS)


def test_another_schema_is_rejected() -> None:
    issues, _ = CHECK.check_pack({"schema": "other", "reviews": []})
    assert issues == ["schema_mismatch"]


def test_a_missing_item_is_reported() -> None:
    entries = [_entry(identifier) for identifier in CHECK.REQUIRED_IDS[:-1]]
    issues, _ = CHECK.check_pack(_pack(*entries))
    assert "entry_missing:" + CHECK.REQUIRED_IDS[-1] in issues


def test_claiming_a_signature_without_a_signer_is_rejected() -> None:
    issues, _ = CHECK.check_pack(_full_pack(status="signed_off"))
    assert "MAIL-SEC-001:signed_without_a_signer" in issues
    assert "MAIL-SEC-001:signed_without_a_date" in issues


def test_a_dated_signed_entry_is_accepted() -> None:
    issues, _ = CHECK.check_pack(
        _full_pack(status="signed_off", signed_by="security-lead", signed_at="2026-09-11")
    )
    assert issues == []


def test_a_blocked_entry_must_say_what_is_needed() -> None:
    issues, _ = CHECK.check_pack(
        _pack(
            *[
                _entry(i, status="blocked_on_external", reviewer_actions=[])
                for i in CHECK.REQUIRED_IDS
            ]
        )
    )
    assert "MAIL-SEC-001:blocked_without_saying_what_is_needed" in issues


def test_evidence_that_does_not_exist_is_rejected() -> None:
    issues, _ = CHECK.check_pack(
        _full_pack(status="evidence_ready", local_evidence=["packages/mailhub/does-not-exist.md"])
    )
    assert "MAIL-SEC-001:evidence_missing:packages/mailhub/does-not-exist.md" in issues


def test_evidence_ready_requires_evidence() -> None:
    issues, _ = CHECK.check_pack(_full_pack(status="evidence_ready"))
    assert "MAIL-SEC-001:claims_evidence_but_lists_none" in issues


def test_not_started_with_evidence_is_rejected() -> None:
    """If there is something to read, the item is not "not started"."""

    issues, _ = CHECK.check_pack(_full_pack(local_evidence=[REAL_EVIDENCE]))
    assert "MAIL-SEC-001:not_started_but_lists_evidence" in issues


def test_an_unknown_kind_is_rejected() -> None:
    issues, _ = CHECK.check_pack(_full_pack(kind="something_else"))
    assert "MAIL-SEC-001:unknown_kind:something_else" in issues


def test_the_real_pack_obeys_every_rule() -> None:
    loaded = yaml.safe_load(CHECK.PACK.read_text(encoding="utf-8"))
    issues, summaries = CHECK.check_pack(loaded)
    assert issues == [], issues
    counts = CHECK.summarise(summaries)
    # The pack exists to track reviews that have not happened; if one is ever
    # signed it must be signed by a named person with a date, which the rules
    # above already enforce.
    assert counts["signed_off"] == 0
    assert counts["evidence_ready"] + counts["blocked_on_external"] + counts["not_started"] == len(
        CHECK.REQUIRED_IDS
    )
    # Both families are present: a review somebody performs, and a verification
    # only a human or a real environment can carry out.
    assert counts["kind:external_review"] > 0
    assert counts["kind:manual_verification"] > 0
    assert "MAIL-UX-010-SCREEN-READER" in {entry["id"] for entry in summaries}
    assert "MAIL-SMTP-163-CLEANUP" in {entry["id"] for entry in summaries}
