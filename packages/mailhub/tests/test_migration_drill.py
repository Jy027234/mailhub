"""Tests for the migration drill's evidence contract.

The drill runs against a labelled fixture rather than a customer system; what is
pinned here is that the bundle keeps saying so.  A bundle that dropped the
fixture label would read as a real cutover, and one that dropped the "no message
left the legacy sender" check would report a clean cutover without having looked
at whether anything was sent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "migration_drill.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("migration_drill", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRILL = _module()

_CHECKS = (
    "container_ready",
    "schema_migrated",
    "shadow_comparison_is_authority_safe",
    "shadow_comparison_can_fail",
    "every_legacy_message_reached_mailhub",
    "cursor_taken_over_from_the_legacy_position",
    "a_second_takeover_is_refused",
    "mailhub_claimed_send_authority",
    "legacy_sender_is_refused_during_cutover",
    "no_message_left_the_legacy_sender_during_cutover",
    "rollback_is_recorded",
    "mailhub_cannot_send_after_rollback",
    "legacy_sender_works_again_after_rollback",
    "rollback_does_not_silently_rewrite_the_cursor",
)


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": DRILL.SCHEMA,
        "legacy_module": "fixture (explicitly labelled, not a customer system)",
        "remaining": "a real adopter's cutover is the operator's step",
        "checks": [{"name": name, "ok": True, "detail": ""} for name in _CHECKS],
        "failures": [],
        "passed": True,
    }
    bundle.update(overrides)
    return bundle


def test_a_complete_bundle_has_no_issues() -> None:
    assert DRILL.validate_bundle(_bundle()) == []


def test_another_bundle_format_is_rejected() -> None:
    assert DRILL.validate_bundle(_bundle(schema="other")) == ["schema_mismatch"]


def test_an_unlabelled_legacy_module_is_rejected() -> None:
    """The bundle must not be readable as a customer migration."""

    issues = DRILL.validate_bundle(_bundle(legacy_module="legacy-mail"))
    assert "legacy_module_not_labelled_as_a_fixture" in issues


def test_missing_remaining_work_is_rejected() -> None:
    assert "remaining_work_not_recorded" in DRILL.validate_bundle(_bundle(remaining="  "))


def test_a_failed_check_is_surfaced() -> None:
    checks = [dict(item) for item in _bundle()["checks"]]
    checks[9]["ok"] = False
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_failed:no_message_left_the_legacy_sender_during_cutover" in issues


def test_dropping_the_dual_send_check_is_rejected() -> None:
    checks = [
        item
        for item in _bundle()["checks"]
        if item["name"] != "no_message_left_the_legacy_sender_during_cutover"
    ]
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_missing:no_message_left_the_legacy_sender_during_cutover" in issues


def test_dropping_the_rollback_check_is_rejected() -> None:
    checks = [
        item
        for item in _bundle()["checks"]
        if item["name"] != "legacy_sender_works_again_after_rollback"
    ]
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_missing:legacy_sender_works_again_after_rollback" in issues


def test_recorded_failures_are_rejected() -> None:
    assert "failures_recorded" in DRILL.validate_bundle(_bundle(failures=["x"]))


def test_a_bundle_that_does_not_claim_pass_is_rejected() -> None:
    assert "not_passed" in DRILL.validate_bundle(_bundle(passed=False))
