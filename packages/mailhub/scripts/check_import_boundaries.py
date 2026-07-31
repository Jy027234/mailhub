"""Fail closed when the MailHub core imports framework, host, or provider code."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

CORE_FILES = (
    "domain.py",
    "errors.py",
    "ports.py",
    "rules.py",
    "intelligence.py",
    "security.py",
    "oauth.py",
    "observability.py",
    "service.py",
    "storage.py",
    "worker.py",
)

FORBIDDEN_TOP_LEVEL = {
    "aiprojectops",
    "caplatform",
    "caa",
    "fastapi",
    "httpx",
    "imaplib",
    "pydantic",
    "smtplib",
    "sqlalchemy",
}

FORBIDDEN_PREFIXES = (
    "mailhub.connectors",
    "mailhub.hosts",
    "mailhub.infrastructure",
)


def _imports(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def find_violations(package_root: Path) -> tuple[str, ...]:
    source_root = package_root / "src" / "mailhub"
    violations: list[str] = []
    for filename in CORE_FILES:
        path = source_root / filename
        if not path.is_file():
            violations.append(f"missing_core_file:{path}")
            continue
        try:
            imports = _imports(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError as exc:
            violations.append(f"syntax_error:{path}:{exc.lineno}")
            continue
        for module in sorted(imports):
            top_level = module.split(".", 1)[0]
            if top_level in FORBIDDEN_TOP_LEVEL or module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(package_root)} imports {module}")
    return tuple(violations)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, default=Path(__file__).parents[1])
    args = parser.parse_args()
    violations = find_violations(args.package_root.resolve())
    if violations:
        raise SystemExit("import boundary violations:\n" + "\n".join(violations))
    print("import boundary gate: ok")


if __name__ == "__main__":
    main()
