import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1]


def test_domain_is_framework_and_host_independent() -> None:
    source = (PACKAGE_ROOT / "src" / "mailhub" / "domain.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported.isdisjoint({"fastapi", "aiprojectops", "caplatform", "caa"})


def test_postgres_migration_does_not_store_credentials_or_raw_body() -> None:
    migration = (
        (PACKAGE_ROOT / "migrations" / "0001_mailhub_core.sql")
        .read_text(encoding="utf-8")
        .casefold()
    )
    assert "password" not in migration
    assert "access_token" not in migration
    assert "refresh_token" not in migration
    assert "raw_mime" not in migration
