"""Tests for the capacity drill's evidence contract.

The drill needs Docker and a migrated PostgreSQL; what is pinned here is what
makes its bundle usable as a baseline.  A bundle that lost the tenant-isolation
or the no-duplicate check would report a fast number while saying nothing about
correctness under load, and one that lost the measurements altogether would be
an assertion rather than evidence.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "capacity_drill.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("capacity_drill", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRILL = _module()

_CHECKS = (
    "container_ready",
    "schema_migrated",
    "no_write_errors_under_concurrency",
    "ingest_rate_meets_the_recorded_floor",
    "read_p95_within_the_recorded_ceiling",
    "reads_return_the_configured_page",
    "reingesting_the_same_message_does_not_duplicate",
    "another_tenant_sees_nothing",
    "every_seeded_message_is_stored",
)


def _bundle(**overrides: Any) -> dict[str, Any]:
    measured: dict[str, Any] = {
        "ingest_per_second": 350.0,
        "write_latency_ms": {"p50": 1.0, "p95": 5.0, "p99": 9.0},
        "read_latency_ms": {"p50": 1.0, "p95": 5.0, "p99": 9.0},
        "thread_read_latency_ms": {"p50": 1.0, "p95": 5.0, "p99": 9.0},
        "listed_per_read": 50,
        "foreign_tenant_rows": 0,
        "reingest_created_a_new_row": False,
        "rows_stored": 1000,
        "errors": [],
    }
    bundle: dict[str, Any] = {
        "schema": DRILL.SCHEMA,
        "measured": measured,
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
    checks[7]["ok"] = False
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_failed:another_tenant_sees_nothing" in issues


def test_losing_the_isolation_check_is_surfaced() -> None:
    checks = [item for item in _bundle()["checks"] if item["name"] != "another_tenant_sees_nothing"]
    issues = DRILL.validate_bundle(_bundle(checks=checks))
    assert "check_missing:another_tenant_sees_nothing" in issues


def test_a_measured_isolation_violation_is_rejected() -> None:
    """Belt and braces: the measurement alone can condemn the bundle."""

    measured = dict(_bundle()["measured"])
    measured["foreign_tenant_rows"] = 3
    assert "tenant_isolation_violated" in DRILL.validate_bundle(_bundle(measured=measured))


def test_missing_measurements_are_rejected() -> None:
    assert "measurements_missing" in DRILL.validate_bundle(_bundle(measured=None))


def test_a_missing_measurement_field_is_rejected() -> None:
    measured = dict(_bundle()["measured"])
    del measured["rows_stored"]
    assert "measurement_missing:rows_stored" in DRILL.validate_bundle(_bundle(measured=measured))


def test_recorded_failures_are_rejected() -> None:
    assert "failures_recorded" in DRILL.validate_bundle(_bundle(failures=["x"]))


def test_a_bundle_that_does_not_claim_pass_is_rejected() -> None:
    assert "not_passed" in DRILL.validate_bundle(_bundle(passed=False))
