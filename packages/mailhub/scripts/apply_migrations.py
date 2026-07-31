"""Apply or roll back MailHub PostgreSQL migrations with a durable ledger.

This is deliberately an operator-facing command rather than an API startup
hook.  It requires PostgreSQL/asyncpg, takes a database advisory lock, checks
the checksum of every already-applied migration, and runs one migration per
transaction.  The SQL files contain explicit transaction wrappers for review;
the runner removes only those wrapper lines so the schema change and ledger
row share the same transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>.+)\.sql$")
_WRAPPER = re.compile(r"(?im)^\s*(?:BEGIN|COMMIT)\s*;\s*$", re.MULTILINE)
_LOCK_KEY = "mailhub:migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    up_path: Path
    down_path: Path
    checksum: str


def discover_migrations(directory: Path) -> tuple[Migration, ...]:
    values: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None or match.group("name").endswith(".down"):
            continue
        version = int(match.group("version"))
        name = match.group("name")
        down_path = directory / f"{version:04d}_{name}.down.sql"
        if not down_path.is_file():
            raise RuntimeError(f"migration_down_missing:{path.name}")
        values.append(
            Migration(
                version=version,
                name=name,
                up_path=path,
                down_path=down_path,
                checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    if not values:
        raise RuntimeError("migration_files_missing")
    versions = [item.version for item in values]
    if versions != sorted(set(versions)):
        raise RuntimeError("migration_versions_duplicate_or_unsorted")
    return tuple(values)


def migration_sql(path: Path) -> str:
    """Return SQL with only top-level transaction wrapper lines removed."""

    return _WRAPPER.sub("", path.read_text(encoding="utf-8"))


def migration_plan(
    migrations: tuple[Migration, ...],
    *,
    applied_versions: set[int],
    direction: str,
    target: int | None,
) -> tuple[Migration, ...]:
    known_versions = {item.version for item in migrations}
    if any(version not in known_versions for version in applied_versions):
        raise RuntimeError("migration_ledger_unknown_version")
    applied_order = [item.version for item in migrations if item.version in applied_versions]
    if direction == "upgrade":
        upper = target if target is not None else migrations[-1].version
        expected = [item.version for item in migrations if item.version <= upper]
        if applied_order != expected[: len(applied_order)]:
            raise RuntimeError("migration_ledger_gap_or_out_of_order")
        return tuple(
            item
            for item in migrations
            if item.version <= upper and item.version not in applied_versions
        )

    lower = target if target is not None else 0
    if applied_order:
        expected = [item.version for item in migrations if item.version <= applied_order[-1]]
        if applied_order != expected:
            raise RuntimeError("migration_ledger_gap_or_out_of_order")
    return tuple(
        item
        for item in reversed(migrations)
        if item.version > lower and item.version in applied_versions
    )


async def apply(
    *,
    database_url: str,
    directory: Path,
    direction: str,
    target: int | None,
    dry_run: bool,
) -> tuple[int, ...]:
    migrations = discover_migrations(directory)
    if direction not in {"upgrade", "down"}:
        raise ValueError("migration_direction_invalid")
    if target is not None and target < 0:
        raise ValueError("migration_target_invalid")
    if dry_run:
        # A dry run is intentionally offline and safe to use in CI.
        plan = migration_plan(
            migrations,
            applied_versions=set(),
            direction=direction,
            target=target,
        )
        return tuple(item.version for item in plan)

    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("migration_runner_requires_asyncpg") from exc

    normalized_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if not normalized_url.startswith("postgresql://"):
        raise RuntimeError("migration_runner_requires_postgresql")
    connection = await asyncpg.connect(normalized_url)
    try:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mailhub_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await connection.execute("SELECT pg_advisory_lock(hashtext($1))", _LOCK_KEY)
        try:
            rows = await connection.fetch(
                "SELECT version, name, checksum FROM mailhub_schema_migrations ORDER BY version"
            )
            applied: dict[int, tuple[str, str]] = {
                int(row["version"]): (str(row["name"]), str(row["checksum"])) for row in rows
            }
            for migration in migrations:
                existing = applied.get(migration.version)
                if existing is None:
                    continue
                if existing != (migration.name, migration.checksum):
                    raise RuntimeError(f"migration_checksum_mismatch:{migration.version:04d}")
            plan = migration_plan(
                migrations,
                applied_versions=set(applied),
                direction=direction,
                target=target,
            )
            for migration in plan:
                path = migration.up_path if direction == "upgrade" else migration.down_path
                async with connection.transaction():
                    await connection.execute(migration_sql(path))
                    if direction == "upgrade":
                        await connection.execute(
                            """
                            INSERT INTO mailhub_schema_migrations(version, name, checksum)
                            VALUES ($1, $2, $3)
                            """,
                            migration.version,
                            migration.name,
                            migration.checksum,
                        )
                    else:
                        await connection.execute(
                            "DELETE FROM mailhub_schema_migrations WHERE version = $1",
                            migration.version,
                        )
            return tuple(item.version for item in plan)
        finally:
            await connection.execute("SELECT pg_advisory_unlock(hashtext($1))", _LOCK_KEY)
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("MAILHUB_DATABASE_URL"),
        help="PostgreSQL URL; defaults to MAILHUB_DATABASE_URL",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "migrations",
    )
    parser.add_argument("--direction", choices=("upgrade", "down"), default="upgrade")
    parser.add_argument("--target", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.database_url:
        raise SystemExit("migration_runner_requires_database_url")
    try:
        result = asyncio.run(
            apply(
                database_url=args.database_url or "",
                directory=args.migrations_dir,
                direction=args.direction,
                target=args.target,
                dry_run=args.dry_run,
            )
        )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    action = "would_apply" if args.dry_run else "applied"
    print(f"{action}:{','.join(f'{version:04d}' for version in result) or 'none'}")


if __name__ == "__main__":
    main()
