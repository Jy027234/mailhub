"""Tests for the browser accessibility gate.

The gate runs a real Chromium, which a unit test cannot do portably.  What is
pinned here is the part that decides whether the run counts as evidence at all:
the pass-count floor, the presence of all three measured views, and the
WCAG 2.5.8 target-size floor read back out of the browser process.  A silent
mistake in any of these would let an unaudited UI ship as audited.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_ui_browser_a11y.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("check_ui_browser_a11y", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _module()


def _measurement_line(label: str, width: float, height: float) -> str:
    payload = {
        "inbox": {"count": 13, "smallest": {"label": label, "width": width, "height": height}},
        "candidates": {"count": 6, "smallest": {"label": "button", "width": 48.67, "height": 33}},
        "connections": {"count": 5, "smallest": {"label": "button", "width": 48.67, "height": 33}},
    }
    return str(GATE.MEASUREMENT_MARKER) + " " + json.dumps(payload)


def _contrast_lines(*view_counts: tuple[str, int]) -> list[str]:
    return [
        str(GATE.CONTRAST_MARKER) + " " + label + " " + str(count) for label, count in view_counts
    ]


def _run_output(
    *,
    passed: int = 17,
    measurement: str | None = None,
    contrast: list[str] | None = None,
) -> str:
    lines = ["", " Test Files  1 passed (1)", "      Tests  " + str(passed) + " passed (17)"]
    if measurement is not None:
        lines.insert(0, measurement)
    if contrast is None:
        contrast = _contrast_lines(
            ("inbox", 42), ("candidates", 12), ("connections", 9), ("en-US", 42), ("standalone", 30)
        )
    lines[0:0] = contrast
    return "\n".join(lines)


def test_accepts_a_run_that_measured_every_view() -> None:
    output = _run_output(measurement=_measurement_line('button "全部"', 40, 26))
    assert GATE.check(output) == []


def test_rejects_a_run_without_a_summary() -> None:
    failures = GATE.check("no vitest output at all")
    assert any(item.startswith("no_test_summary") for item in failures)


def test_rejects_an_empty_suite_that_still_exits_zero() -> None:
    output = _run_output(passed=0, measurement=_measurement_line('button "全部"', 40, 26))
    failures = GATE.check(output)
    assert any(item.startswith("too_few_tests") for item in failures)


def test_rejects_a_run_that_never_measured_target_size() -> None:
    failures = GATE.check(_run_output())
    assert any(item.startswith("target_size_unmeasured") for item in failures)


def test_rejects_a_control_below_the_wcag_minimum() -> None:
    output = _run_output(measurement=_measurement_line('button "全部"', 40, 18))
    failures = GATE.check(output)
    assert any(item.startswith("target_size_below_minimum") for item in failures)
    assert any("18.0" in item for item in failures)


def test_rejects_a_measurement_line_that_cannot_be_parsed() -> None:
    output = _run_output(measurement=GATE.MEASUREMENT_MARKER + " {not json}")
    with pytest.raises(SystemExit) as error:
        GATE.check(output)
    assert "measurement_line_unparsable" in str(error.value)


def test_rejects_a_view_without_a_smallest_control() -> None:
    output = _run_output(
        measurement=GATE.MEASUREMENT_MARKER + " " + json.dumps({"inbox": {"count": 3}})
    )
    with pytest.raises(SystemExit) as error:
        GATE.check(output)
    assert "measurement_view_missing_smallest" in str(error.value)


def test_rejects_a_run_that_never_measured_contrast() -> None:
    output = _run_output(measurement=_measurement_line('button "全部"', 40, 26), contrast=[])
    failures = GATE.check(output)
    assert any(item.startswith("contrast_unmeasured") for item in failures)


def test_rejects_a_vacuous_contrast_result() -> None:
    contrast = _contrast_lines(
        ("inbox", 0), ("candidates", 12), ("connections", 9), ("en-US", 42), ("standalone", 30)
    )
    output = _run_output(measurement=_measurement_line('button "全部"', 40, 26), contrast=contrast)
    failures = GATE.check(output)
    assert any(item.startswith("contrast_vacuous") and "inbox" in item for item in failures)


def test_rejects_an_unparsable_contrast_line() -> None:
    output = _run_output(
        measurement=_measurement_line('button "全部"', 40, 26),
        contrast=[GATE.CONTRAST_MARKER + " inbox lots"],
    )
    with pytest.raises(SystemExit) as error:
        GATE.check(output)
    assert "contrast_line_unparsable" in str(error.value)


def test_short_side_uses_the_smaller_dimension() -> None:
    assert GATE.Measurement(view="inbox", label="b", width=40, height=18).short_side == 18
