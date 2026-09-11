"""Tests for the failover drill's evidence contract.

The drill needs two real PostgreSQL containers; what is pinned here is what
makes its bundle count as failover evidence.  A bundle that lost the timeline
check would also be produced by a drill that merely restarted a second cluster,
which is exactly what this file must not let pass as a promotion.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "failover_drill.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("failover_drill", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRILL = _module()

_ALL_CHECKS = (
    "primary_ready",
    "primary_accepts_replication",
    "standby_base_backup_taken",
    "standby_started",
    "standby_is_in_recovery",
    "primary_timeline_is_initial",
    "walsender_reports_streaming",
    "live_write_reached_the_standby",
    "primary_killed",
    "standby_promote_issued",
    "promoted_node_accepts_writes",
    "promotion_advanced_the_timeline",
    "no_data_lost_across_the_failover",
    "promoted_node_takes_new_writes",
)


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": DRILL.SCHEMA,
        "timeline_before": "00000001",
        "timeline_after": "00000002",
        "checks": [{"name": name, "ok": True, "detail": ""} for name in _ALL_CHECKS],
        "failures": [],
        "passed": True,
    }
    bundle.update(overrides)
    return bundle


def _without(name: str) -> list[dict[str, Any]]:
    return [item for item in _bundle()["checks"] if item["name"] != name]


def test_a_complete_bundle_has_no_issues() -> None:
    assert DRILL.validate_bundle(_bundle()) == []


def test_another_bundle_format_is_rejected() -> None:
    assert DRILL.validate_bundle(_bundle(schema="other")) == ["schema_mismatch"]


def test_a_bundle_without_checks_is_rejected() -> None:
    assert DRILL.validate_bundle(_bundle(checks=[])) == ["checks_missing"]


def test_a_failed_check_is_surfaced() -> None:
    checks = [dict(item) for item in _bundle()["checks"]]
    checks[9]["ok"] = False
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_failed:standby_promote_issued" in issues


def test_a_stalled_timeline_is_rejected() -> None:
    """The one check that separates a promotion from a restart."""

    issues = DRILL.validate_bundle(_bundle(timeline_after="00000001"))
    assert "timeline_did_not_advance" in issues


def test_dropping_the_kill_check_is_surfaced() -> None:
    """Without it nothing proves the primary was ever gone."""

    issues = DRILL.validate_bundle(_bundle(checks=_without("primary_killed")))
    assert "check_missing:primary_killed" in issues


def test_dropping_the_data_survival_check_is_surfaced() -> None:
    issues = DRILL.validate_bundle(_bundle(checks=_without("no_data_lost_across_the_failover")))
    assert "check_missing:no_data_lost_across_the_failover" in issues


def test_recorded_failures_are_rejected() -> None:
    assert "failures_recorded" in DRILL.validate_bundle(_bundle(failures=["x"]))


def test_a_bundle_that_does_not_claim_pass_is_rejected() -> None:
    assert "not_passed" in DRILL.validate_bundle(_bundle(passed=False))
