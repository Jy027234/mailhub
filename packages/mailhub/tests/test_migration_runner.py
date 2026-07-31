from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _runner_module() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "apply_migrations.py"
    spec = importlib.util.spec_from_file_location("mailhub_apply_migrations", path)
    if spec is None or spec.loader is None:
        raise AssertionError("migration_runner_import_spec_missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_migration_runner_discovers_checksums_and_order() -> None:
    runner = _runner_module()
    migrations = runner.discover_migrations(Path(__file__).parents[1] / "migrations")

    assert [item.version for item in migrations] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
    ]
    assert all(len(item.checksum) == 64 for item in migrations)
    assert "BEGIN;" not in runner.migration_sql(migrations[1].up_path)
    assert "COMMIT;" not in runner.migration_sql(migrations[1].up_path)


def test_migration_runner_plans_upgrade_and_rollback_without_sqlite_fallback() -> None:
    runner = _runner_module()
    migrations = runner.discover_migrations(Path(__file__).parents[1] / "migrations")

    upgrade = runner.migration_plan(
        migrations,
        applied_versions={1, 2},
        direction="upgrade",
        target=4,
    )
    rollback = runner.migration_plan(
        migrations,
        applied_versions={1, 2, 3, 4, 5, 6},
        direction="down",
        target=4,
    )

    assert [item.version for item in upgrade] == [3, 4]
    assert [item.version for item in rollback] == [6, 5]


def test_migration_runner_rejects_unknown_or_gapped_rollback_ledger() -> None:
    runner = _runner_module()
    migrations = runner.discover_migrations(Path(__file__).parents[1] / "migrations")

    for applied in ({1, 2, 99}, {1, 3, 4}):
        try:
            runner.migration_plan(
                migrations,
                applied_versions=applied,
                direction="down",
                target=1,
            )
        except RuntimeError as exc:
            assert str(exc) in {
                "migration_ledger_unknown_version",
                "migration_ledger_gap_or_out_of_order",
            }
        else:
            raise AssertionError("migration_ledger_invalid_state_accepted")
