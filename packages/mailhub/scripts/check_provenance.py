"""Fail-closed source provenance gate for release builds."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=Path("source-ledger.yaml"))
    args = parser.parse_args()
    document = yaml.safe_load(args.ledger.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "mailhub.source_ledger.v1"
    ):
        raise SystemExit("source_ledger_schema_invalid")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("source_ledger_entries_missing")
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit("source_ledger_entry_invalid")
        level = str(entry.get("reuse_level", ""))
        status = str(entry.get("status", ""))
        if level in {"A", "B"} and status != "approved":
            raise SystemExit(f"source_ledger_unapproved:{entry.get('entry_id', 'unknown')}")
        if level in {"A", "B"} and entry.get("source_kind") != "new":
            for key in ("source_repo", "source_commit", "source_paths"):
                if not entry.get(key):
                    entry_id = entry.get("entry_id", "unknown")
                    raise SystemExit(f"source_ledger_traceability_missing:{entry_id}:{key}")
    print("source provenance gate: ok")


if __name__ == "__main__":
    main()
