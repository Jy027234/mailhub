"""Verify release provenance/notice prerequisites without pretending to be a scanner."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, default=Path(__file__).parents[1])
    args = parser.parse_args()
    package_root = args.package_root.resolve()
    repository_root = package_root.parents[1]
    required = (
        package_root / "LICENSE",
        package_root / "NOTICE",
        package_root / "scripts" / "generate_sbom.py",
        repository_root / "LICENSE",
        repository_root / "docs" / "provenance" / "source-ledger.yaml",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("license_gate_missing:" + ",".join(missing))
    package_license = (package_root / "LICENSE").read_text(encoding="utf-8")
    if "CAPlatform Proprietary License Notice" not in package_license:
        raise SystemExit("license_gate_package_license_invalid")
    document = yaml.safe_load((package_root / "source-ledger.yaml").read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != (
        "mailhub.source_ledger.v1"
    ):
        raise SystemExit("license_gate_package_ledger_invalid")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("license_gate_package_entries_missing")
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit("license_gate_entry_invalid")
        level = str(entry.get("reuse_level", ""))
        if level in {"A", "B"} and (
            entry.get("status") != "approved" or entry.get("license_verified") is not True
        ):
            raise SystemExit(f"license_gate_unapproved:{entry.get('entry_id', 'unknown')}")
        if entry.get("source_kind") == "behavior_reference":
            if not entry.get("source_repo") or not entry.get("source_commit"):
                raise SystemExit(
                    f"license_gate_reference_unpinned:{entry.get('entry_id', 'unknown')}"
                )
            if entry.get("status") != "not_imported":
                raise SystemExit(
                    f"license_gate_reference_imported:{entry.get('entry_id', 'unknown')}"
                )
    print("license/provenance prerequisite gate: ok")


if __name__ == "__main__":
    main()
