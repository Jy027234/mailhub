"""Generate a minimal CycloneDX-style dependency inventory for a release.

This inventory is intentionally generated from the resolved environment rather
than hand-maintained. A release pipeline should augment it with image layers,
signatures and vulnerability results from its own scanner.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from importlib import metadata
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    components: list[dict[str, str]] = []
    for distribution in sorted(
        metadata.distributions(), key=lambda item: item.metadata.get("Name", "").casefold()
    ):
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or not version:
            continue
        components.append({"type": "library", "name": name, "version": version})
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "mailhub", "version": "0.1.0"},
            "tools": [{"vendor": "fyjtech", "name": "mailhub.generate_sbom", "version": "1"}],
            "properties": [{"name": "python.version", "value": platform.python_version()}],
        },
        "components": components,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    sys.exit(main())
