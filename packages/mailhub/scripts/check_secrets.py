"""Scan release source for accidentally committed credential material.

This is intentionally conservative: provider fixtures and schema field names are
not secrets, while literal values attached to credential-shaped keys are release
blocking. Runtime SecretRef values are injected by the host and never scanned in.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----")
TOKEN_RE = re.compile(
    r"(?i)(?:access_token|refresh_token|client_secret|api_key|private_key|password|secret)"
    r"\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
)
HIGH_RISK_RE = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|(?:ghp|github_pat|xox[baprs]-|sk-[A-Za-z0-9]{20,})[A-Za-z0-9_-]*)"
)

PLACEHOLDER_PREFIXES = (
    "replace-",
    "your-",
    "example-",
    "test-",
    "dummy-",
    "<",
    "${",
)


def find_findings(paths: tuple[Path, ...]) -> tuple[str, ...]:
    findings: list[str] = []
    for root in paths:
        candidates = (root,) if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file() or path.suffix not in {".py", ".json", ".yaml", ".yml", ".toml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if PEM_RE.search(line) or HIGH_RISK_RE.search(line):
                    findings.append(f"{path}:{line_number}:credential_pattern")
                    continue
                match = TOKEN_RE.search(line)
                if match and not match.group(1).casefold().startswith(PLACEHOLDER_PREFIXES):
                    findings.append(f"{path}:{line_number}:credential_literal")
    return tuple(findings)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", action="append", type=Path, dest="paths")
    args = parser.parse_args()
    paths = tuple(path.resolve() for path in (args.paths or [Path(__file__).parents[1] / "src"]))
    findings = find_findings(paths)
    if findings:
        raise SystemExit("secret scan failed:\n" + "\n".join(findings))
    print("secret scan gate: ok")


if __name__ == "__main__":
    main()
