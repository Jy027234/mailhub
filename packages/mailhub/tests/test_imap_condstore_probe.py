"""Tests for the offline re-check of a CONDSTORE probe bundle.

The probe needs a live server; the bundle is what survives, so a reviewer has to
be able to re-derive the verdict from the file alone.  These tests pin that: a
bundle whose own recorded checks disagree with its summary must be rejected
rather than read as evidence.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "imap_condstore_live_probe.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("imap_condstore_live_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _module()


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "kind": "mailhub.imap.condstore_live_probe",
        "capabilities": ["IMAP4REV1", "CONDSTORE"],
        "checks": [
            {"name": "capability_retrieved", "ok": True, "detail": "IMAP4REV1 CONDSTORE"},
            {"name": "connector_cursor_carries_modseq", "ok": True, "detail": "77:1:2"},
        ],
        "failures": [],
        "first_cursor": "77:1:2",
        "second_cursor": "77:1:2",
        "passed": True,
    }
    bundle.update(overrides)
    return bundle


def test_a_consistent_bundle_has_no_issues() -> None:
    assert PROBE.validate_bundle(_bundle()) == []


def test_a_bundle_from_another_probe_is_rejected() -> None:
    assert PROBE.validate_bundle(_bundle(kind="something.else")) == ["kind_mismatch"]


def test_a_bundle_without_checks_is_rejected() -> None:
    assert PROBE.validate_bundle(_bundle(checks=[])) == ["checks_missing"]


def test_a_recorded_failure_is_surfaced() -> None:
    checks = [
        {"name": "capability_retrieved", "ok": True},
        {"name": "connector_cursor_carries_modseq", "ok": False},
    ]
    issues = PROBE.validate_bundle(_bundle(checks=checks))
    assert "check_failed:connector_cursor_carries_modseq" in issues


def test_a_missing_required_check_is_surfaced() -> None:
    issues = PROBE.validate_bundle(_bundle(checks=[{"name": "capability_retrieved", "ok": True}]))
    assert "cursor_check_missing" in issues


def test_a_two_field_cursor_is_rejected() -> None:
    """A UID-only cursor is the symptom the probe exists to catch."""

    issues = PROBE.validate_bundle(_bundle(first_cursor="77:1"))
    assert "first_cursor_is_not_a_modseq_cursor" in issues


def test_a_bundle_that_does_not_claim_pass_is_rejected() -> None:
    assert "not_passed" in PROBE.validate_bundle(_bundle(passed=False))


def test_recorded_failures_are_rejected() -> None:
    issues = PROBE.validate_bundle(_bundle(failures=["connector_cursor_carries_modseq"]))
    assert "failures_recorded" in issues


def test_an_empty_capability_list_is_rejected() -> None:
    assert "capabilities_missing" in PROBE.validate_bundle(_bundle(capabilities=[]))
