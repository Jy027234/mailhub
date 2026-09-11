"""Assemble a MailHub release bundle plus an integrity manifest.

This script only **builds**.  It never publishes, because the public package
name (MAIL-ADOPT-002) and the outbound license model (MAIL-ADOPT-003) are still
open decisions.  Publishing is a separate, explicitly approved action handled by
publish_release.py, which refuses any manifest this script marks as not
publishable.

Usage::

    python scripts/build_release.py --gates none          # fast local bundle
    python scripts/build_release.py --gates full          # release-quality bundle

Output layout::

    dist/release/<version>/
        python/                  wheel + sdist
        typescript-sdk/          compiled SDK dist (or SKIPPED.txt)
        ui/                      compiled UI dist (or SKIPPED.txt)
        sbom.cdx.json            CycloneDX inventory
        release-manifest.json    versions, digests, gate results, blockers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
COMPATIBILITY_MATRIX = REPO_ROOT / "docs" / "compatibility" / "mailhub-v1.md"
PROVIDER_MATRIX = PACKAGE_ROOT / "docs" / "provider-compatibility.yaml"
TS_SDK_DIR = PACKAGE_ROOT / "sdk" / "typescript"
UI_DIR = PACKAGE_ROOT / "ui"

MANIFEST_SCHEMA = "mailhub.release_manifest.v1"

# Publishing stays blocked until these are decided and recorded as evidence.
# Empty as of 2026-08-19: the public package scope is decided (@fyjtech).
PUBLISH_BLOCKERS: tuple[str, ...] = ()

# Accepted, explicitly deferred decisions.  They stay visible in the manifest so
# the deferral is auditable, but they no longer block building or publishing.
PUBLISH_DEFERRALS = (
    "MAIL-ADOPT-003: outbound license model deferred by owner decision "
    "(2026-08-19); the package still ships as CAPlatform proprietary/UNLICENSED",
    "MAIL-ADOPT-002: npm scope @fyjtech is decided but not yet claimed on a "
    "public registry; publish to an internal registry until then",
)


@dataclass
class StepResult:
    name: str
    status: str  # ok | skipped | failed
    detail: str = ""


@dataclass
class ReleaseReport:
    steps: list[StepResult] = field(default_factory=list)

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(StepResult(name=name, status=status, detail=detail))

    @property
    def failed(self) -> list[StepResult]:
        return [step for step in self.steps if step.status == "failed"]


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _run(command: list[str], *, cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def _read_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = str(data["project"]["version"])
    if not version:
        raise ValueError("pyproject_version_missing")
    return version


def _git(*args: str) -> str | None:
    if _tool("git") is None:
        return None
    code, output = _run(["git", *args], cwd=REPO_ROOT)
    return output if code == 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_gates(report: ReleaseReport, mode: str) -> None:
    if mode == "none":
        report.record("gates", "skipped", "mode=none: bundle is not release quality")
        return
    python = sys.executable
    gate_commands: list[tuple[str, list[str]]] = [
        ("pytest", [python, "-m", "pytest", "-q"]),
        ("ruff-format", [python, "-m", "ruff", "format", "--check", "src", "tests", "scripts"]),
        ("ruff-check", [python, "-m", "ruff", "check", "src", "tests", "scripts"]),
        ("mypy", [python, "-m", "mypy", "src", "tests"]),
        ("import-boundaries", [python, "scripts/check_import_boundaries.py"]),
        ("secret-scan", [python, "scripts/check_secrets.py", "--path", "src"]),
        ("openapi-export", [python, "scripts/export_openapi.py"]),
        ("migration-contracts", [python, "scripts/test_migrations.py"]),
        ("provenance", [python, "scripts/check_provenance.py", "--ledger", "source-ledger.yaml"]),
        ("license-gate", [python, "scripts/check_license_gate.py"]),
        # Renders the release chart and asserts its fail-closed properties; a
        # missing helm is a failure, never a silent skip.
        ("helm-chart", [python, "scripts/check_helm_chart.py"]),
        # Contrast, target size, reflow and reduced-motion need a layout engine,
        # so this runs the UI in headless Chromium.  A missing npm, node_modules
        # or Playwright browser is a failure, never a silent skip.
        ("ui-browser-a11y", [python, "scripts/check_ui_browser_a11y.py"]),
        # The external review pack references evidence by path; a moved or
        # deleted file must fail the release rather than leave a pack that
        # promises a reviewer something that is no longer there.
        ("external-review-pack", [python, "scripts/check_external_review_pack.py"]),
    ]
    for name, command in gate_commands:
        code, output = _run(command, cwd=PACKAGE_ROOT)
        if code == 0:
            report.record(name, "ok")
        else:
            tail = "\n".join(output.splitlines()[-12:])
            report.record(name, "failed", tail)


def build_python(report: ReleaseReport, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "build",
        "--no-isolation",
        "--outdir",
        str(target),
        str(PACKAGE_ROOT),
    ]
    code, output = _run(command, cwd=PACKAGE_ROOT)
    if code != 0:
        # Fall back to an isolated build when the no-isolation build fails.
        code, output = _run(
            [sys.executable, "-m", "build", "--outdir", str(target), str(PACKAGE_ROOT)],
            cwd=PACKAGE_ROOT,
        )
    if code != 0:
        report.record("python-dist", "failed", "\n".join(output.splitlines()[-12:]))
        return
    produced = sorted(item.name for item in target.iterdir() if item.is_file())
    report.record("python-dist", "ok", ", ".join(produced) or "no artifacts")


def build_node_package(report: ReleaseReport, name: str, source: Path, target: Path) -> None:
    """Compile a Node package when its dependencies are installed.

    A missing node_modules directory is reported as skipped rather than faked:
    the manifest must never imply a compiled UI/SDK that does not exist.
    """

    if not (source / "package.json").is_file():
        report.record(name, "skipped", "package.json missing")
        return
    npm = _tool("npm.cmd") or _tool("npm")
    if npm is None:
        report.record(name, "skipped", "npm not available")
        return
    if not (source / "node_modules").is_dir():
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKIPPED.txt").write_text(
            "node_modules missing; run npm install in "
            + source.relative_to(REPO_ROOT).as_posix()
            + " then rebuild.\n",
            encoding="utf-8",
        )
        report.record(name, "skipped", "node_modules missing (run npm install)")
        return
    code, output = _run([npm, "run", "build"], cwd=source)
    if code != 0:
        report.record(name, "failed", "\n".join(output.splitlines()[-12:]))
        return
    dist = source / "dist"
    if not dist.is_dir():
        report.record(name, "failed", "npm build produced no dist/")
        return
    test_code, test_output = _run([npm, "run", "test", "--if-present"], cwd=source)
    if test_code != 0:
        tail = "\n".join(test_output.splitlines()[-12:])
        report.record(name, "failed", "package tests failed: " + tail)
        return
    report.record(name + "-tests", "ok", "npm test")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(dist, target)
    # A published npm package needs its manifest alongside the compiled output,
    # otherwise the bundle cannot be installed or published.
    for file_name in ("package.json", "README.md", "LICENSE", "NOTICE"):
        source_file = source / file_name
        if source_file.is_file():
            shutil.copy2(source_file, target / file_name)
    report.record(name, "ok", str(len(list(target.rglob("*")))) + " entries copied")


def build_sbom(report: ReleaseReport, target: Path) -> Path | None:
    code, output = _run(
        [sys.executable, "scripts/generate_sbom.py", "--output", str(target)],
        cwd=PACKAGE_ROOT,
    )
    if code != 0 or not target.is_file():
        report.record("sbom", "failed", "\n".join(output.splitlines()[-12:]))
        return None
    report.record("sbom", "ok", target.name)
    return target


def collect_artifacts(release_dir: Path, manifest_path: Path) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    for item in sorted(release_dir.rglob("*")):
        if not item.is_file() or item == manifest_path:
            continue
        artifacts.append(
            {
                "path": item.relative_to(release_dir).as_posix(),
                "size": item.stat().st_size,
                "sha256": _sha256(item),
            }
        )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a MailHub release bundle.")
    parser.add_argument("--gates", choices=("none", "full"), default="none")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--version", default=None, help="override the pyproject version")
    args = parser.parse_args()

    version = args.version or _read_version(PACKAGE_ROOT / "pyproject.toml")
    release_dir = (args.output or (PACKAGE_ROOT / "dist" / "release")) / version
    release_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = release_dir / "release-manifest.json"

    report = ReleaseReport()
    run_gates(report, args.gates)
    build_python(report, release_dir / "python")
    build_node_package(report, "typescript-sdk", TS_SDK_DIR, release_dir / "typescript-sdk")
    build_node_package(report, "ui", UI_DIR, release_dir / "ui")
    sbom_path = build_sbom(report, release_dir / "sbom.cdx.json")

    blockers: list[str] = list(PUBLISH_BLOCKERS)
    if args.gates != "full":
        blockers.append("release gates not run (--gates full required for publishing)")
    blockers.extend("build step failed: " + step.name for step in report.failed)

    artifacts = collect_artifacts(release_dir, manifest_path)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "package": "mailhub",
        "version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "gates": args.gates,
        "steps": [
            {"name": step.name, "status": step.status, "detail": step.detail}
            for step in report.steps
        ],
        "artifacts": artifacts,
        "sbom": (
            {"path": sbom_path.relative_to(release_dir).as_posix(), "sha256": _sha256(sbom_path)}
            if sbom_path is not None
            else None
        ),
        "references": {
            "compatibility_matrix": COMPATIBILITY_MATRIX.relative_to(REPO_ROOT).as_posix(),
            "provider_compatibility": PROVIDER_MATRIX.relative_to(REPO_ROOT).as_posix(),
            "release_standard": "docs/adr/0010-mailhub-development-release-standard.md",
        },
        "publishable": not blockers,
        "publish_blockers": blockers,
        "publish_deferrals": list(PUBLISH_DEFERRALS),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("release bundle : " + str(release_dir))
    print("artifacts      : " + str(len(artifacts)))
    print("gates          : " + args.gates)
    print("publishable    : " + str(manifest["publishable"]))
    for blocker in blockers:
        print("  blocked: " + blocker)
    for deferral in PUBLISH_DEFERRALS:
        print("  deferred: " + deferral)
    for step in report.steps:
        marker = {"ok": "OK", "skipped": "SKIP", "failed": "FAIL"}[step.status]
        print("  [" + marker + "] " + step.name + (" - " + step.detail if step.detail else ""))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
