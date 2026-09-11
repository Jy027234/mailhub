"""Fail-closed browser accessibility gate for the React workspace.

Contrast, target size, reflow, resize-text and reduced-motion can only be judged
by a layout engine, so this gate runs the Vitest browser suite in headless
Playwright Chromium against the *shipped* stylesheet.  The suite carries its own
liveness probes that make the same axe rules fail on purpose, so a green run
cannot come from rules that quietly stopped running.

The gate refuses to trust the exit code alone:

* the summary must report at least MIN_PASSED passing tests, so an empty suite
  (bad glob, excluded file) fails instead of "passing";
* the measured target-size floor is read back out of the run and re-checked
  against WCAG 2.5.8 here, so the number quoted in the audit report is verified
  by the gate rather than asserted only inside the browser process.

A missing npm, missing node_modules or a missing Playwright browser is a
failure, never a skip: "the UI was not audited" must not look like a pass.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = PACKAGE_ROOT / "ui"
MIN_PASSED = 15
MIN_TARGET_SIZE = 24.0
MEASUREMENT_MARKER = "MAILHUB_MEASURED_CONTROLS"
CONTRAST_MARKER = "MAILHUB_CONTRAST_EVALUATED"
MIN_CONTRAST_VIEWS = 5
SUMMARY_PATTERN = re.compile(r"Tests\s+(\d+) passed")


@dataclass(frozen=True, slots=True)
class Measurement:
    view: str
    label: str
    width: float
    height: float

    @property
    def short_side(self) -> float:
        return min(self.width, self.height)


def _npm() -> str:
    for candidate in ("npm.cmd", "npm"):
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit(
        "npm_not_available: install Node.js and npm; the browser accessibility "
        "gate refuses to report an unaudited UI as passed"
    )


def _run(npm: str, ui_root: Path) -> str:
    completed = subprocess.run(
        [npm, "run", "test:browser"],
        cwd=ui_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise SystemExit(
            "browser_accessibility_suite_failed:\n" + "\n".join(output.splitlines()[-25:])
        )
    return output


def parse_measurements(output: str) -> list[Measurement]:
    """Read the target-size evidence back out of the browser run."""

    measurements: list[Measurement] = []
    for line in output.splitlines():
        _, separator, payload = line.partition(MEASUREMENT_MARKER)
        if not separator:
            continue
        try:
            parsed = json.loads(payload.strip())
        except json.JSONDecodeError as error:
            raise SystemExit(
                "measurement_line_unparsable: " + payload.strip() + " (" + str(error) + ")"
            ) from error
        if not isinstance(parsed, dict) or not parsed:
            raise SystemExit("measurement_line_empty: the suite reported no views")
        for view, entry in parsed.items():
            smallest = entry.get("smallest") if isinstance(entry, dict) else None
            if not isinstance(smallest, dict):
                raise SystemExit("measurement_view_missing_smallest: " + str(view))
            measurements.append(
                Measurement(
                    view=str(view),
                    label=str(smallest.get("label", "")),
                    width=float(smallest["width"]),
                    height=float(smallest["height"]),
                )
            )
    return measurements


def parse_contrast_counts(output: str) -> dict[str, int]:
    """Read how many elements axe measured for contrast, per rendered view."""

    counts: dict[str, int] = {}
    for line in output.splitlines():
        _, separator, payload = line.partition(CONTRAST_MARKER)
        if not separator:
            continue
        fields = payload.split()
        if len(fields) != 2:
            raise SystemExit("contrast_line_unparsable: " + payload.strip())
        label, raw = fields
        try:
            counts[label] = int(raw)
        except ValueError as error:
            raise SystemExit("contrast_line_unparsable: " + payload.strip()) from error
    return counts


def check(output: str) -> list[str]:
    """Return every reason the run is not acceptable evidence, if any."""

    failures: list[str] = []
    summary = SUMMARY_PATTERN.search(output)
    if summary is None:
        failures.append("no_test_summary: the suite did not report a pass count")
    elif int(summary.group(1)) < MIN_PASSED:
        failures.append(
            "too_few_tests: "
            + summary.group(1)
            + " passed, expected at least "
            + str(MIN_PASSED)
            + " (an empty or mis-globbed suite must not pass)"
        )
    measurements = parse_measurements(output)
    if len(measurements) < 3:
        failures.append(
            "target_size_unmeasured: the suite reported "
            + str(len(measurements))
            + " views, expected inbox/candidates/connections"
        )
    contrast = parse_contrast_counts(output)
    if len(contrast) < MIN_CONTRAST_VIEWS:
        failures.append(
            "contrast_unmeasured: axe reported contrast for "
            + str(len(contrast))
            + " views, expected at least "
            + str(MIN_CONTRAST_VIEWS)
            + " (an unmeasured clean result is not evidence)"
        )
    for label, count in sorted(contrast.items()):
        if count <= 0:
            failures.append("contrast_vacuous: " + label + " measured 0 elements")
    for measurement in measurements:
        if measurement.short_side < MIN_TARGET_SIZE:
            failures.append(
                "target_size_below_minimum: "
                + measurement.view
                + " "
                + measurement.label
                + " is "
                + str(measurement.short_side)
                + " CSS px, WCAG 2.5.8 requires "
                + str(MIN_TARGET_SIZE)
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-root", type=Path, default=UI_ROOT)
    parser.add_argument("--print-output", action="store_true")
    args = parser.parse_args()

    ui_root: Path = args.ui_root
    if not (ui_root / "package.json").is_file():
        raise SystemExit("ui_package_missing: " + str(ui_root / "package.json"))
    if not (ui_root / "node_modules").is_dir():
        raise SystemExit(
            "node_modules_missing: run npm install in "
            + ui_root.relative_to(PACKAGE_ROOT).as_posix()
            + " before the browser accessibility gate"
        )
    output = _run(_npm(), ui_root)
    if args.print_output:
        print(output)
    failures = check(output)
    if failures:
        print("ui-browser-a11y: FAILED")
        for failure in failures:
            print("  - " + failure)
        return 1
    for measurement in parse_measurements(output):
        print(
            "ui-browser-a11y: "
            + measurement.view
            + " smallest control "
            + str(measurement.short_side)
            + " CSS px ("
            + measurement.label
            + ")"
        )
    for label, count in sorted(parse_contrast_counts(output).items()):
        print("ui-browser-a11y: " + label + " contrast-measured elements " + str(count))
    print("ui-browser-a11y: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
