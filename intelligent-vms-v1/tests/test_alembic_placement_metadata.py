"""Alembic metadata must include placement tables so autogenerate cannot drop them.

``migrations/env.py`` builds ``target_metadata`` from ``Base.metadata``. Placement
models register their tables only when ``app.models.placement`` is imported. The
compare below runs in a fresh interpreter: pytest collection imports those
models for other tests, and that registration would hide a missing env.py import.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "migrations" / "versions"
PLACEMENT_TABLES = frozenset(
    {
        "infrastructure_nodes",
        "site_regions",
        "placement_assignments",
        "placement_revocations",
    }
)
DESTRUCTIVE_DIFF_KINDS = frozenset({"remove_table", "remove_index"})
COMPARISON_TIMEOUT_SECONDS = 60
TEST_ONLY_SECRET_KEY = "ci-test-key"


def _migration_version_names() -> tuple[str, ...]:
    """Return committed revision filenames so the compare cannot add one."""
    return tuple(sorted(path.name for path in VERSIONS.glob("*.py")))


def _run_comparison(database: Path) -> subprocess.CompletedProcess[str]:
    """Run the autogenerate compare against one disposable SQLite file.

    Args:
        database: File path created by the child process. It must stay outside
            the product tree.

    Returns:
        Completed child process. stdout is a single JSON object. stderr is the
        Alembic log.
    """
    resolved = database.resolve()
    url = "sqlite+aiosqlite:///" + resolved.as_posix()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "DATABASE_URL": url,
        "VMS_SECRET_KEY": TEST_ONLY_SECRET_KEY,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(resolved)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=COMPARISON_TIMEOUT_SECONDS,
    )


def _placement_destructive_diffs(diffs: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    """Return drop-table and drop-index diffs aimed at placement tables."""
    return [
        diff
        for diff in diffs
        if diff.get("kind") in DESTRUCTIVE_DIFF_KINDS and diff.get("table") in PLACEMENT_TABLES
    ]


def test_comparison_refuses_a_database_inside_the_product_tree() -> None:
    """Refuse to migrate a SQLite file that resolves inside the repository."""
    with pytest.raises(RuntimeError, match="product tree"):
        _refuse_unless_disposable(ROOT / "migrations" / "not-a-real-database.db")


def test_mapped_tables_are_in_alembic_metadata_and_placement_is_not_dropped(tmp_path: Path) -> None:
    """Require every mapped table in metadata and no placement drop ops.

    The disposable database is migrated to the current Alembic head first.
    Remaining non-drop drift is reported by the child payload and is not
    treated as success criteria; closing it would need a new revision.
    """
    database = tmp_path / "alembic-placement.db"
    versions_before = _migration_version_names()
    completed = _run_comparison(database)
    versions_after = _migration_version_names()
    assert completed.returncode == 0, completed.stderr
    comparison = json.loads(completed.stdout)
    diffs = comparison["diffs"]
    missing_mapped = sorted(set(comparison["mapped_tables"]) - set(comparison["metadata_tables"]))
    missing_placement = sorted(PLACEMENT_TABLES - set(comparison["metadata_tables"]))
    remove_tables = sorted(
        diff["table"] for diff in diffs if diff["kind"] == "remove_table" and diff["table"]
    )
    destructive = _placement_destructive_diffs(diffs)
    problems: list[str] = []
    if versions_after != versions_before:
        problems.append("autogenerate wrote a revision file")
    if comparison["database_dialect"] != "sqlite":
        problems.append("comparison did not stay on disposable sqlite")
    if comparison["script_heads"] != [comparison["database_revision"]]:
        problems.append(
            "disposable database revision "
            f"{comparison['database_revision']} is not the script head {comparison['script_heads']}"
        )
    if not set(PLACEMENT_TABLES) <= set(comparison["database_tables"]):
        problems.append("placement tables were not present after upgrade head")
    if missing_mapped:
        problems.append(f"mapped tables missing from target_metadata: {missing_mapped}")
    if missing_placement:
        problems.append(f"placement tables missing from target_metadata: {missing_placement}")
    if remove_tables:
        problems.append(f"autogenerate remove_table ops: {remove_tables}")
    if destructive:
        problems.append(f"destructive placement diffs: {destructive}")
    assert not problems, "\n".join(problems) + "\n" + json.dumps(comparison, indent=2)


def _refuse_unless_disposable(database: Path) -> str:
    """Return the async SQLite URL for a file outside the product tree.

    Args:
        database: Candidate database path supplied by the parent test.

    Returns:
        SQLAlchemy URL that points only at that file.

    Raises:
        RuntimeError: If the path is inside the repository or is not a database file.
    """
    resolved = database.resolve()
    if resolved.suffix != ".db":
        raise RuntimeError("disposable comparison requires a .db path")
    if ROOT.resolve() in resolved.parents:
        raise RuntimeError("refusing to migrate a database inside the product tree")
    return "sqlite+aiosqlite:///" + resolved.as_posix()


def _describe_diff(diff: tuple) -> dict[str, str | None]:
    """Reduce one Alembic autogenerate tuple to kind, table, and object name."""
    kind = str(diff[0]) if diff else ""
    table_name = None
    object_name = None
    if len(diff) > 1:
        subject = diff[1]
        object_name = getattr(subject, "name", None)
        if kind in {"remove_table", "add_table"}:
            table_name = object_name
        else:
            table = getattr(subject, "table", None)
            table_name = getattr(table, "name", None)
    return {"kind": kind, "table": table_name, "name": object_name}


def _compare_disposable_database(database: Path) -> dict:
    """Upgrade a throwaway database to head and record the autogenerate diff.

    This follows ``alembic check``: configure from ``migrations/env.py`` and
    read ``upgrade_ops.as_diffs()``. It does not call ``revision``, so no
    migration file is written.

    Args:
        database: Disposable SQLite file. The parent creates it under tmp_path.

    Returns:
        JSON-ready metadata tables, mapped tables, database tables, and diffs.

    Raises:
        RuntimeError: If settings would migrate any database other than this file.
    """
    url = _refuse_unless_disposable(database)
    os.environ["DATABASE_URL"] = url
    os.environ["VMS_SECRET_KEY"] = TEST_ONLY_SECRET_KEY
    control_api = ROOT / "services" / "control-api"
    if str(control_api) not in sys.path:
        sys.path.insert(0, str(control_api))

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.util import AutogenerateDiffsDetected

    from app.core.config import settings

    if settings.database_url != url:
        raise RuntimeError("refusing to migrate a database that is not the disposable sqlite file")

    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    try:
        command.check(config)
        raw_diffs: list[tuple] = []
    except AutogenerateDiffsDetected as detected:
        raw_diffs = list(detected.diffs)

    from app.db.base import Base

    # Snapshot before these imports. They register any mapped table env.py
    # forgot, which is how a missing placement import fails the parent test.
    metadata_tables = sorted(Base.metadata.tables)
    import app.models.entities  # noqa: F401
    import app.models.placement  # noqa: F401

    mapped_tables = sorted(Base.metadata.tables)
    with sqlite3.connect(database) as connection:
        database_tables = sorted(
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        )
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    heads = ScriptDirectory.from_config(config).get_heads()
    return {
        "database_dialect": "sqlite",
        "database_revision": None if version_row is None else version_row[0],
        "script_heads": heads,
        "metadata_tables": metadata_tables,
        "mapped_tables": mapped_tables,
        "database_tables": database_tables,
        "diffs": [_describe_diff(diff) for diff in raw_diffs],
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: test_alembic_placement_metadata.py DISPOSABLE_SQLITE_PATH")
    payload = _compare_disposable_database(Path(sys.argv[1]))
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
