"""Concurrent-migration regression tests (#456).

Two uvicorn workers can race inside a check-then-act `PRAGMA table_info` →
`ALTER TABLE ADD COLUMN` migration: both pass the PRAGMA before either
commits, and the loser hits `sqlite3.OperationalError: duplicate column
name: ...` and crashes its child process.

These tests pin the swallow-duplicate-column behavior of `_safe_add_column`
and confirm `_migrate_sync_health` (the migration the bug was filed against)
is now safe to run on already-migrated schemas.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_migrations = _load("db/migrations.py", "_migrations_concurrent")


pytestmark = pytest.mark.unit


class _LyingCursor:
    """Cursor proxy that lies on the next PRAGMA table_info call (returns []).

    Reproduces the race: the worker's PRAGMA returned no column (because the
    other worker had not yet committed), but by the time we issue ALTER the
    column is present and SQLite raises 'duplicate column name'.
    """

    def __init__(self, real: sqlite3.Cursor) -> None:
        self._real = real
        self._lying = False

    def execute(self, sql: str, *args, **kwargs):
        if sql.strip().upper().startswith("PRAGMA TABLE_INFO"):
            self._lying = True
            return self
        return self._real.execute(sql, *args, **kwargs)

    def fetchall(self):
        if self._lying:
            self._lying = False
            return []
        return self._real.fetchall()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_safe_add_column_swallows_duplicate_column_race(tmp_path):
    """Loser of the PRAGMA→ALTER race must not crash."""
    db_path = tmp_path / "race.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    cur.execute("ALTER TABLE t ADD COLUMN foo INTEGER DEFAULT 0")
    conn.commit()

    # PRAGMA lies: the helper believes foo is absent, ALTER then hits
    # 'duplicate column name', which the helper must swallow.
    lying = _LyingCursor(cur)
    result = _migrations._safe_add_column(
        lying, "t", "foo", "ALTER TABLE t ADD COLUMN foo INTEGER DEFAULT 0"
    )
    assert result is False

    # Schema is intact.
    cur.execute("PRAGMA table_info(t)")
    cols = {row[1] for row in cur.fetchall()}
    assert cols == {"id", "foo"}
    conn.close()


def test_safe_add_column_propagates_other_errors(tmp_path):
    """Non-duplicate-column errors must still raise."""
    db_path = tmp_path / "err.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # No table named t — ALTER will fail with 'no such table: t'.
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _migrations._safe_add_column(
            cur, "t", "foo", "ALTER TABLE t ADD COLUMN foo INTEGER"
        )
    conn.close()


def test_safe_add_column_returns_true_when_added(tmp_path):
    db_path = tmp_path / "add.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()

    added = _migrations._safe_add_column(
        cur, "t", "foo", "ALTER TABLE t ADD COLUMN foo INTEGER DEFAULT 0"
    )
    assert added is True
    cur.execute("PRAGMA table_info(t)")
    cols = {row[1] for row in cur.fetchall()}
    assert cols == {"id", "foo"}
    conn.close()


def test_run_all_migrations_under_concurrent_workers(tmp_path):
    """End-to-end stress: 6 connections all run `run_all_migrations` against a
    fresh DB at the same instant. None must crash with `duplicate column name`
    or any other exception (#456).

    Mirrors the production cold-start race that motivated the helper sweep.
    """
    import threading

    schema_mod = _load("db/schema.py", "_schema_concurrent")

    db = tmp_path / "concurrent.db"
    init = sqlite3.connect(str(db))
    schema_mod.init_schema(init.cursor(), init)
    init.close()

    N = 6
    errors: list[tuple[int, BaseException]] = []
    barrier = threading.Barrier(N)

    def worker(i: int) -> None:
        try:
            c = sqlite3.connect(str(db), timeout=10)
            c.execute("PRAGMA busy_timeout = 30000")
            cur = c.cursor()
            barrier.wait()
            _migrations.run_all_migrations(cur, c)
            c.close()
        except BaseException as e:  # noqa: BLE001 — capture for assertion
            errors.append((i, e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, (
        f"{len(errors)} of {N} concurrent workers crashed: "
        + ", ".join(f"worker {i}={type(e).__name__}: {e}" for i, e in errors[:3])
    )

    final = sqlite3.connect(str(db))
    fcur = final.cursor()
    fcur.execute("PRAGMA table_info(agent_git_config)")
    cols = {row[1] for row in fcur.fetchall()}
    assert "auto_sync_enabled" in cols
    assert "freeze_schedules_if_sync_failing" in cols
    fcur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_sync_state'"
    )
    assert fcur.fetchone() is not None
    final.close()


def test_migrate_sync_health_idempotent_under_race(tmp_path):
    """`_migrate_sync_health` must not crash when re-run on an already-migrated
    schema — the production cold-start race lands the second worker in this
    exact state (#456)."""
    db_path = tmp_path / "sync.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # Minimal upstream schema needed by the migration.
    cur.execute(
        """
        CREATE TABLE agent_git_config (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # First call adds the columns and creates agent_sync_state.
    _migrations._migrate_sync_health(cur, conn)
    cur.execute("PRAGMA table_info(agent_git_config)")
    cols = {row[1] for row in cur.fetchall()}
    assert "auto_sync_enabled" in cols
    assert "freeze_schedules_if_sync_failing" in cols
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_sync_state'"
    )
    assert cur.fetchone() is not None

    # Second call must be a clean no-op (covers the cold-start re-run path).
    _migrations._migrate_sync_health(cur, conn)

    # Race simulation: corrupt the cursor so PRAGMA reports columns absent
    # *after* they were added — the second ALTER would crash without the
    # duplicate-column swallow.
    lying = _LyingCursor(cur)
    _migrations._migrate_sync_health(lying, conn)

    conn.close()
