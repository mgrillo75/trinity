"""
Unit tests for public chat context building (fix for #539).

The bug: user message was persisted to the DB *before* build_context_prompt
was called, causing the current message to appear twice in every agent prompt —
once in "Previous conversation:" and once in "Current message:".

Tests exercise PublicChatOperations directly against a temporary SQLite DB
(TRINITY_DB_PATH) so the real get_db_connection() is used but isolated.
"""

from __future__ import annotations

import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable without shadowing tests/utils.
# ---------------------------------------------------------------------------
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# ---------------------------------------------------------------------------
# Minimal schema for the tables PublicChatOperations touches.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS public_chat_sessions (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL,
    session_identifier TEXT NOT NULL,
    identifier_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS public_chat_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL
);
"""


@pytest.fixture()
def ops(tmp_path, monkeypatch):
    """
    Provision a fresh SQLite file DB, point TRINITY_DB_PATH at it, and
    return a PublicChatOperations instance ready to use.
    """
    db_path = tmp_path / "trinity_test.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    # Evict any previously imported db.connection so the env var is re-read.
    for mod in list(sys.modules):
        if mod.startswith("db.") or mod == "db":
            sys.modules.pop(mod, None)

    conn = sqlite3.connect(str(db_path))
    for stmt in _SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    conn.close()

    from db.public_chat import PublicChatOperations
    return PublicChatOperations()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(ops, link_id: str = "link-1") -> str:
    session = ops.get_or_create_session(link_id, secrets.token_urlsafe(8), "anonymous")
    return session.id


def _add_msg(ops, session_id: str, role: str, content: str) -> None:
    ops.add_message(session_id, role, content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildContextPromptNoDuplication:
    """Verify the current message never appears in both context sections."""

    def test_empty_session_message_appears_once(self, ops):
        """First message in a fresh session: should appear only in Current message."""
        sid = _make_session(ops)
        new_msg = "Hello, what can you do?"

        ctx = ops.build_context_prompt(sid, new_msg, max_turns=10)

        assert "Previous conversation:" not in ctx, (
            "Expected no 'Previous conversation:' section in empty session context"
        )
        assert ctx.count(new_msg) == 1, (
            f"New message should appear exactly once; got:\n{ctx}"
        )
        assert "Current message:" in ctx

    def test_prior_history_new_message_appears_once(self, ops):
        """With existing history, new message appears only in Current message."""
        sid = _make_session(ops)
        _add_msg(ops, sid, "user", "First question")
        _add_msg(ops, sid, "assistant", "First answer")

        new_msg = "Follow-up question"
        ctx = ops.build_context_prompt(sid, new_msg, max_turns=10)

        assert "Previous conversation:" in ctx
        assert "First question" in ctx
        assert "First answer" in ctx

        # Parse out just the "Previous conversation:" section
        prev_lines = []
        in_prev = False
        for line in ctx.splitlines():
            if line.strip() == "Previous conversation:":
                in_prev = True
            elif line.strip() == "Current message:":
                in_prev = False
            elif in_prev:
                prev_lines.append(line)

        prev_text = "\n".join(prev_lines)
        assert new_msg not in prev_text, (
            f"New message must NOT appear in 'Previous conversation:':\n{prev_text}"
        )
        assert ctx.count(new_msg) == 1, (
            f"New message should appear exactly once in full context:\n{ctx}"
        )

    def test_old_broken_order_produces_duplicate(self, ops):
        """
        Regression guard: storing user message BEFORE build_context_prompt
        (the old broken order) causes the message to appear twice.

        This test documents the bug so future readers understand why the
        order in routers/public.py matters.
        """
        sid = _make_session(ops)
        new_msg = "Bug-triggering question"

        # OLD (broken) order: store first, then build
        _add_msg(ops, sid, "user", new_msg)
        ctx = ops.build_context_prompt(sid, new_msg, max_turns=10)

        assert ctx.count(new_msg) == 2, (
            "With the old broken order the message appears twice. "
            "If this fails the context builder has been changed to de-dup internally "
            "— update test_correct_order_no_duplication accordingly."
        )

    def test_correct_order_no_duplication(self, ops):
        """
        Fix validation: build context THEN store user message → appears once.

        This mirrors the corrected call order in routers/public.py after #539.
        """
        sid = _make_session(ops)
        _add_msg(ops, sid, "user", "Previous Q")
        _add_msg(ops, sid, "assistant", "Previous A")

        new_msg = "New question after fix"

        # CORRECT ORDER: build context first, then store
        ctx = ops.build_context_prompt(sid, new_msg, max_turns=10)
        _add_msg(ops, sid, "user", new_msg)

        assert ctx.count(new_msg) == 1
        assert "Previous conversation:" in ctx
        assert "Previous Q" in ctx

    def test_public_link_mode_header_always_present(self, ops):
        """The public-link sentinel header must be the first line."""
        sid = _make_session(ops)
        ctx = ops.build_context_prompt(sid, "anything", max_turns=10)
        assert ctx.splitlines()[0] == "### Trinity: Public Link Access Mode"

    def test_max_turns_respected(self, ops):
        """Only the most recent max_turns exchanges appear in history."""
        sid = _make_session(ops)
        # Add 6 full turns (12 messages)
        for i in range(6):
            _add_msg(ops, sid, "user", f"Q{i}")
            _add_msg(ops, sid, "assistant", f"A{i}")

        # max_turns=2 → only last 4 messages (Q4, A4, Q5, A5)
        ctx = ops.build_context_prompt(sid, "new", max_turns=2)

        assert "Q5" in ctx and "A5" in ctx, "Most recent turn should be present"
        assert "Q0" not in ctx, "Oldest turn should be pruned by max_turns"
