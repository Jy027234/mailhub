"""Tests for the backup/restore drill harness.

The drill itself needs Docker and PostgreSQL; these tests pin the evidence
contract and the ledger parsing, which is where a silent mistake would turn a
failed rollback into an apparent pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "dr_drill.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("dr_drill", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRILL = _module()


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": DRILL.SCHEMA_VERSION,
        "passed": True,
        "checks": {"restore_completed": True, "rollback_removed_later_schema": True},
    }
    bundle.update(overrides)
    return bundle


def test_valid_bundle_passes() -> None:
    assert DRILL.validate_bundle(_bundle()) == []


def test_module_imports_without_docker() -> None:
    """Docker is only invoked at run time, never at import time."""

    source = _SCRIPT.read_text(encoding="utf-8")
    head = source.split("def _docker", 1)[0]

    assert "subprocess.run" not in head


def test_failed_check_is_detected() -> None:
    bundle = _bundle()
    bundle["checks"]["restore_completed"] = False

    assert "checks_failed:restore_completed" in DRILL.validate_bundle(bundle)


@pytest.mark.parametrize("value", [False, None, "yes"])
def test_passed_flag_must_be_true(value: Any) -> None:
    assert "not_passed" in DRILL.validate_bundle(_bundle(passed=value))


def test_missing_checks_are_rejected() -> None:
    assert "checks_missing" in DRILL.validate_bundle(_bundle(checks={}))


@pytest.mark.parametrize(
    "leaky",
    [{"database_url": "postgresql://u:p@h/db"}, {"password": "x"}, {"secret": "x"}],
)
def test_forbidden_keys_are_detected(leaky: dict[str, Any]) -> None:
    bundle = _bundle()
    bundle.update(leaky)

    issues = DRILL.validate_bundle(bundle)

    assert any(issue.startswith("forbidden_key:") for issue in issues), issues


def test_ledger_numbers_parse_file_stems() -> None:
    """The ledger stores file stems, so prefix parsing is what makes the
    rollback comparison correct (string order would drop the target itself)."""

    numbers = DRILL._ledger_numbers(["0001_mailhub_core", "0016_x", "0021_y"])

    assert numbers == [1, 16, 21]
    assert "0016_x" > "0016"  # the trap the numeric parse avoids


def test_ledger_numbers_reject_unparsable_entries() -> None:
    with pytest.raises(RuntimeError, match="ledger_entry_unparsable"):
        DRILL._ledger_numbers(["not-a-version"])


def test_rollback_target_is_a_pinned_chain_position() -> None:
    assert DRILL.ROLLBACK_TARGET.isdigit()
    assert DRILL.LATER_COLUMN == ("mail_messages", "cc_addresses")
