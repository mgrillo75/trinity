"""
Schema + migration tests for agent_shared_files (amazing-file-outbound, FILES-001).

Covers the Step 1 MVP invariants:
- Migration creates the agent_shared_files table with the expected columns
- All 3 indexes land, including the partial `WHERE revoked_at IS NULL`
- file_sharing_enabled column is added to agent_ownership
- FK carries BOTH ON DELETE CASCADE and ON UPDATE CASCADE
- Migration is idempotent (re-run on the same DB is a no-op, no errors)
- Works on a legacy DB that predates the column (simulates real upgrades)
- ON UPDATE CASCADE actually propagates a parent rename to child rows
- ON DELETE CASCADE actually removes child rows

No live backend, no Docker — just sqlite3 + the real migration function.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# Load db/migrations.py and db/schema.py directly to avoid pulling in
# db/__init__.py (which imports pydantic via db_models). This keeps the
# test suite runnable on a bare Python without the full backend runtime
# installed — schema + migrations are pure-stdlib anyway.
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


schema = _load_module("_ams_schema", _BACKEND / "db" / "schema.py")
migrations = _load_module("_ams_migrations", _BACKEND / "db" / "migrations.py")

TABLES = schema.TABLES
_migrate_agent_shared_files = migrations._migrate_agent_shared_files


LEGACY_AGENT_OWNERSHIP = """
    CREATE TABLE agent_ownership (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_name TEXT UNIQUE NOT NULL,
        owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
"""

LEGACY_USERS = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""


@pytest.fixture
def legacy_db(tmp_path):
    """A DB that has agent_ownership but NOT file_sharing_enabled or the new table.

    Simulates an existing install upgrading over this migration.
    """
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute(LEGACY_USERS)
    cur.execute(LEGACY_AGENT_OWNERSHIP)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def fresh_db(tmp_path):
    """A DB with the CURRENT schema.py DDL (including file_sharing_enabled).

    Simulates a fresh install where schema.py was applied, then the named
    migration runs. The migration must be a no-op for this path too.
    """
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute(TABLES["users"])
    cur.execute(TABLES["agent_ownership"])
    conn.commit()
    yield conn
    conn.close()


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fk_for(conn, table, from_col):
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return [r for r in rows if r[3] == from_col]


def _index_names(conn, table):
    return {
        r[0] for r in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='{table}'"
        ).fetchall()
    }


def _run_migration(conn):
    cur = conn.cursor()
    _migrate_agent_shared_files(cur, conn)


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def test_creates_table_on_legacy_db(legacy_db):
    _run_migration(legacy_db)
    row = legacy_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_shared_files'"
    ).fetchone()
    assert row is not None, "agent_shared_files table was not created"


def test_expected_columns_present(legacy_db):
    _run_migration(legacy_db)
    cols = _column_names(legacy_db, "agent_shared_files")
    expected = {
        "id", "agent_name", "filename", "stored_filename", "size_bytes",
        "mime_type", "download_token", "created_by", "created_at",
        "expires_at", "revoked_at", "one_time", "consumed_at",
        "download_count", "last_downloaded_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_file_sharing_enabled_column_added(legacy_db):
    assert "file_sharing_enabled" not in _column_names(legacy_db, "agent_ownership")
    _run_migration(legacy_db)
    assert "file_sharing_enabled" in _column_names(legacy_db, "agent_ownership")


def test_file_sharing_enabled_defaults_to_zero(legacy_db):
    _run_migration(legacy_db)
    legacy_db.execute(
        "INSERT INTO users (id, username, password_hash, role, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 'user', 'now', 'now')"
    )
    legacy_db.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
        "VALUES ('a1', 1, 'now')"
    )
    legacy_db.commit()
    val = legacy_db.execute(
        "SELECT file_sharing_enabled FROM agent_ownership WHERE agent_name='a1'"
    ).fetchone()[0]
    assert val == 0


# ---------------------------------------------------------------------------
# FK contract — both DELETE and UPDATE cascade
# ---------------------------------------------------------------------------


def test_fk_cascade_flags(legacy_db):
    _run_migration(legacy_db)
    fks = _fk_for(legacy_db, "agent_shared_files", "agent_name")
    assert len(fks) == 1, f"expected 1 FK on agent_name, got {fks}"
    # PRAGMA foreign_key_list tuple:
    # (id, seq, table, from, to, on_update, on_delete, match)
    _, _, parent_table, _, parent_col, on_update, on_delete, _ = fks[0]
    assert parent_table == "agent_ownership"
    assert parent_col == "agent_name"
    assert on_update == "CASCADE", f"ON UPDATE should CASCADE, got {on_update!r}"
    assert on_delete == "CASCADE", f"ON DELETE should CASCADE, got {on_delete!r}"


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_all_three_indexes_created(legacy_db):
    _run_migration(legacy_db)
    idx = _index_names(legacy_db, "agent_shared_files")
    for expected in ("idx_agent_files_agent", "idx_agent_files_token", "idx_agent_files_expires"):
        assert expected in idx, f"missing {expected}; have {idx}"


def test_expires_index_is_partial(legacy_db):
    _run_migration(legacy_db)
    row = legacy_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_agent_files_expires'"
    ).fetchone()
    assert row is not None
    ddl = row[0].lower()
    assert "where" in ddl, f"expires index is not partial: {row[0]}"
    assert "revoked_at is null" in ddl, f"partial WHERE is wrong: {row[0]}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_migration_is_idempotent_on_legacy_db(legacy_db):
    _run_migration(legacy_db)
    _run_migration(legacy_db)  # must not raise
    row = legacy_db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='agent_shared_files'"
    ).fetchone()
    assert row[0] == 1


def test_migration_is_noop_on_fresh_db(fresh_db):
    """Fresh install applied schema.py already; migration should be safe to run."""
    assert "file_sharing_enabled" in _column_names(fresh_db, "agent_ownership")
    _run_migration(fresh_db)
    assert "file_sharing_enabled" in _column_names(fresh_db, "agent_ownership")


# ---------------------------------------------------------------------------
# CASCADE behavior — prove the FK actually does what it advertises
# ---------------------------------------------------------------------------


def _seed_parent_and_child(conn, agent="orig"):
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 'user', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at) VALUES (?, 1, 'now')",
        (agent,),
    )
    conn.execute(
        """
        INSERT INTO agent_shared_files
            (id, agent_name, filename, stored_filename, size_bytes,
             download_token, created_by, created_at, expires_at)
        VALUES ('file1', ?, 'report.csv', 'stored-uuid', 100, 'tok', ?, 'now', 'later')
        """,
        (agent, agent),
    )
    conn.commit()


def test_on_update_cascade_propagates_rename(legacy_db):
    _run_migration(legacy_db)
    legacy_db.execute("PRAGMA foreign_keys = ON")
    _seed_parent_and_child(legacy_db, agent="orig")

    legacy_db.execute(
        "UPDATE agent_ownership SET agent_name='renamed' WHERE agent_name='orig'"
    )
    legacy_db.commit()

    child_name = legacy_db.execute(
        "SELECT agent_name FROM agent_shared_files WHERE id='file1'"
    ).fetchone()[0]
    assert child_name == "renamed", "ON UPDATE CASCADE did not propagate"


def test_on_delete_cascade_removes_children(legacy_db):
    _run_migration(legacy_db)
    legacy_db.execute("PRAGMA foreign_keys = ON")
    _seed_parent_and_child(legacy_db, agent="doomed")

    legacy_db.execute("DELETE FROM agent_ownership WHERE agent_name='doomed'")
    legacy_db.commit()

    count = legacy_db.execute(
        "SELECT COUNT(*) FROM agent_shared_files WHERE id='file1'"
    ).fetchone()[0]
    assert count == 0, "ON DELETE CASCADE did not remove child row"


# ---------------------------------------------------------------------------
# download_token uniqueness — enforced by UNIQUE constraint
# ---------------------------------------------------------------------------


def test_download_token_unique(legacy_db):
    _run_migration(legacy_db)
    _seed_parent_and_child(legacy_db, agent="agent-a")

    with pytest.raises(sqlite3.IntegrityError):
        legacy_db.execute(
            """
            INSERT INTO agent_shared_files
                (id, agent_name, filename, stored_filename, size_bytes,
                 download_token, created_by, created_at, expires_at)
            VALUES ('file2', 'agent-a', 'r2.csv', 'stored-2', 50, 'tok', 'agent-a', 'now', 'later')
            """
        )
