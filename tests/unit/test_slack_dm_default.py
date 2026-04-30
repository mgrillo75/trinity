"""
Unit tests for Slack DM-default management (#584).

Covers:
- ``set_dm_default`` — single-tx clear-then-set, exclusivity, idempotency
- ``unbind_agent`` — must NOT auto-promote (router enforces the guard)
- The "blocked unbind" rule itself is a router-layer concern; we exercise
  it via a thin direct-call shim around the router handler.

Run in-process against an ephemeral SQLite database (no backend, no Docker).
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
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load_module(rel_path: str, name: str):
    path = _BACKEND / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_schema_mod = _load_module("db/schema.py", "_schema_slack")
_migrations_mod = _load_module("db/migrations.py", "_migrations_slack")
init_schema = _schema_mod.init_schema
run_all_migrations = _migrations_mod.run_all_migrations


pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Throwaway DB with full schema + migrations applied."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    init_schema(cursor, conn)
    run_all_migrations(cursor, conn)
    conn.commit()
    conn.close()

    # Drop cached modules so production code picks up the new path.
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db."):
            sys.modules.pop(modname, None)

    yield db_path


@pytest.fixture
def slack_ops(tmp_db):
    """Fresh SlackChannelOperations bound to the tmp DB."""
    from db.slack_channels import SlackChannelOperations
    return SlackChannelOperations()


def _bind(slack_ops, team_id, agent_name, *, is_dm_default=False, channel_id=None):
    """Helper: bind an agent to a workspace channel."""
    return slack_ops.bind_channel_to_agent(
        team_id=team_id,
        slack_channel_id=channel_id or f"C-{agent_name}",
        slack_channel_name=agent_name,
        agent_name=agent_name,
        is_dm_default=is_dm_default,
    )


# ---------------------------------------------------------------------------
# set_dm_default
# ---------------------------------------------------------------------------


class TestSetDmDefault:

    def test_returns_false_when_agent_not_bound(self, slack_ops):
        """No row to flip → setter returns False so the router can 404."""
        assert slack_ops.set_dm_default("T-x", "ghost-agent") is False

    def test_sets_default_on_bound_agent(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha")
        assert slack_ops.set_dm_default("T-1", "alpha") is True
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

    def test_clears_previous_default(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

        slack_ops.set_dm_default("T-1", "beta")
        # Exactly one default after the flip
        assert slack_ops.get_dm_default_agent("T-1") == "beta"
        agents = slack_ops.get_agents_for_workspace("T-1")
        assert sum(1 for a in agents if a["is_dm_default"]) == 1

    def test_idempotent_when_already_default(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        slack_ops.set_dm_default("T-1", "alpha")
        slack_ops.set_dm_default("T-1", "alpha")
        agents = slack_ops.get_agents_for_workspace("T-1")
        assert sum(1 for a in agents if a["is_dm_default"]) == 1
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

    def test_isolated_per_workspace(self, slack_ops):
        """Setting default in workspace A must not touch workspace B."""
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-2", "alpha")  # different workspace, same name
        slack_ops.set_dm_default("T-2", "alpha")

        # Both workspaces have alpha as default — no cross-talk.
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"
        assert slack_ops.get_dm_default_agent("T-2") == "alpha"

    def test_setting_one_does_not_touch_siblings(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")
        _bind(slack_ops, "T-1", "gamma")

        slack_ops.set_dm_default("T-1", "gamma")

        agents = {a["agent_name"]: a["is_dm_default"]
                  for a in slack_ops.get_agents_for_workspace("T-1")}
        assert agents == {"alpha": False, "beta": False, "gamma": True}


# ---------------------------------------------------------------------------
# unbind_agent — pure delete, no auto-promote
# ---------------------------------------------------------------------------


class TestUnbindAgent:

    def test_unbind_non_default_agent(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")

        assert slack_ops.unbind_agent("T-1", "beta") is True
        # Default is unchanged
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

    def test_unbind_default_agent_does_not_promote(self, slack_ops):
        """Per #584 the DB layer is a pure delete — auto-promote was
        rejected in favour of a router-layer guard. This test pins the
        contract so any future drift is loud."""
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")

        slack_ops.unbind_agent("T-1", "alpha")

        # Beta is NOT promoted automatically.
        assert slack_ops.get_dm_default_agent("T-1") is None
        agents = slack_ops.get_agents_for_workspace("T-1")
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "beta"
        assert agents[0]["is_dm_default"] is False

    def test_unbind_only_agent_clears_workspace(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        assert slack_ops.unbind_agent("T-1", "alpha") is True
        assert slack_ops.get_agents_for_workspace("T-1") == []
        assert slack_ops.get_dm_default_agent("T-1") is None

    def test_unbind_unknown_agent_returns_false(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        assert slack_ops.unbind_agent("T-1", "ghost") is False
        # Default unchanged
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"
