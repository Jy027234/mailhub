"""Tests for the fault injection drill's evidence contract.

The drill kills backends, pauses a database and stops it accepting writes; what
is pinned here is what makes the result evidence rather than a story.  Each
scenario has to have surfaced a failure and left nothing partial behind, and the
validator must refuse a bundle where a scenario quietly did not inject anything.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "fault_drill.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("fault_drill", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRILL = _module()

_CHECKS = (
    "container_ready",
    "schema_migrated",
    "terminated_backend_surfaces_the_failure",
    "terminated_backend_leaves_nothing_behind",
    "paused_database_surfaces_the_failure",
    "pool_recovers_after_the_partition",
    "paused_write_left_nothing_behind",
    "read_only_database_refuses_writes",
    "writes_resume_once_storage_recovers",
    "refused_write_left_nothing_behind",
    "crashed_worker_blocks_the_next_lease",
    "crashed_worker_leaves_an_unknown_outcome",
    "crashed_worker_releases_its_lease",
)


def _scenarios(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        name: {"failure_surfaced": True} for name in ("process", "network", "storage", "crash")
    }
    for name, value in overrides.items():
        base[name] = value
    return base


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": DRILL.SCHEMA,
        "scenarios": _scenarios(),
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


def test_a_bundle_without_checks_is_rejected() -> None:
    assert DRILL.validate_bundle(_bundle(checks=[])) == ["checks_missing"]


def test_a_failed_check_is_surfaced() -> None:
    checks = [dict(item) for item in _bundle()["checks"]]
    checks[3]["ok"] = False
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_failed:terminated_backend_leaves_nothing_behind" in issues


def test_a_scenario_that_injected_nothing_is_rejected() -> None:
    """A bundle where the pause never happened must not read as a pass."""

    issues = DRILL.validate_bundle(
        _bundle(scenarios=_scenarios(network={"failure_surfaced": False}))
    )
    assert "scenario_did_not_surface_a_failure:network" in issues


def test_a_missing_scenario_is_rejected() -> None:
    scenarios = _scenarios()
    del scenarios["storage"]
    assert "scenario_missing:storage" in DRILL.validate_bundle(_bundle(scenarios=scenarios))


def test_missing_scenarios_block_is_rejected() -> None:
    assert "scenarios_missing" in DRILL.validate_bundle(_bundle(scenarios=None))


def test_dropping_the_crash_outcome_check_is_rejected() -> None:
    checks = [
        item
        for item in _bundle()["checks"]
        if item["name"] != "crashed_worker_leaves_an_unknown_outcome"
    ]
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_missing:crashed_worker_leaves_an_unknown_outcome" in issues


def test_recorded_failures_are_rejected() -> None:
    assert "failures_recorded" in DRILL.validate_bundle(_bundle(failures=["x"]))


def test_a_bundle_that_does_not_claim_pass_is_rejected() -> None:
    assert "not_passed" in DRILL.validate_bundle(_bundle(passed=False))
