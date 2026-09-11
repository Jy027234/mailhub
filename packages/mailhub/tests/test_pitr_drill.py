"""Tests for the point-in-time recovery drill's evidence contract.

The drill needs Docker and a throwaway PostgreSQL; what is pinned here is what
makes its bundle worth keeping.  A bundle that lost the "the write after the
target is gone" check would still look like a successful recovery while proving
no point-in-time behaviour at all, which is the failure mode this file exists to
prevent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "pitr_drill.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("pitr_drill", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRILL = _module()


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": DRILL.SCHEMA,
        "archived_wal_segments": 5,
        "checks": [
            {"name": name, "ok": True, "detail": ""}
            for name in (
                "container_ready",
                "archive_mode_enabled",
                "base_backup_taken",
                "wal_segments_archived",
                "recovery_completed",
                "write_before_target_recovered",
                "write_after_target_excluded",
                "recovery_is_point_in_time",
                "recovery_restored_from_archive",
                "recovery_stopped_at_target",
            )
        ],
        "failures": [],
        "passed": True,
    }
    bundle.update(overrides)
    return bundle


def test_a_complete_bundle_has_no_issues() -> None:
    assert DRILL.validate_bundle(_bundle()) == []


def test_another_bundle_format_is_rejected() -> None:
    assert DRILL.validate_bundle(_bundle(schema="other")) == ["schema_mismatch"]


def test_a_bundle_without_checks_is_rejected() -> None:
    assert DRILL.validate_bundle(_bundle(checks=[])) == ["checks_missing"]


def test_a_failed_check_is_surfaced() -> None:
    checks = [dict(item) for item in _bundle()["checks"]]
    checks[6]["ok"] = False
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_failed:write_after_target_excluded" in issues


def test_dropping_the_point_in_time_check_is_surfaced() -> None:
    """Without it the bundle proves a restore, not a point-in-time restore."""

    checks = [item for item in _bundle()["checks"] if item["name"] != "write_after_target_excluded"]
    assert "check_missing:write_after_target_excluded" in DRILL.validate_bundle(
        _bundle(checks=checks)
    )


def test_an_empty_archive_is_rejected() -> None:
    assert "no_wal_segments_archived" in DRILL.validate_bundle(_bundle(archived_wal_segments=0))


def test_recorded_failures_are_rejected() -> None:
    assert "failures_recorded" in DRILL.validate_bundle(_bundle(failures=["x"]))


def test_a_bundle_that_does_not_claim_pass_is_rejected() -> None:
    assert "not_passed" in DRILL.validate_bundle(_bundle(passed=False))
