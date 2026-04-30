"""Regression tests for Issue #520: clean (return_code == 0) exits whose
final ``result`` JSON line was lost — typically because a child subprocess
inherited stdout and the reader thread leaked — must not be reported as
"completed successfully" on a 200.

When ``metadata.cost_usd`` and ``metadata.duration_ms`` are both ``None``,
the headless task path raises HTTP 502 so backend records the execution
as FAILED with a useful diagnostic, instead of returning an empty 200
that the watchdog later orphan-reaps with a misleading message.

Sibling of #516 / ``_classify_signal_exit`` — that one handles
``return_code != 0`` external kills; this one handles the ``return_code
== 0`` lost-result-line shape.

Module under test:
    docker/base-image/agent_server/services/claude_code.py::_classify_empty_result
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

if "agent_server" not in sys.modules:
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [str(_AGENT_SERVER_DIR)]
    sys.modules["agent_server"] = _stub

from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.claude_code import _classify_empty_result  # noqa: E402


# ---------------------------------------------------------------------------
# Empty-result exits — must classify as 502 with diagnostic context.
# ---------------------------------------------------------------------------

def test_both_cost_and_duration_none_returns_502():
    """The defining condition: cost_usd and duration_ms both ``None`` means
    the terminal ``result`` JSON line never arrived. Must surface as 502 so
    backend records FAILED with the diagnostic detail rather than dispatching
    a misleading empty 200."""
    metadata = ExecutionMetadata(tool_count=7, num_turns=12)
    # cost_usd and duration_ms intentionally left as None (default)

    result = _classify_empty_result(metadata, raw_message_count=22)

    assert result is not None
    status_code, detail = result
    assert status_code == 502
    # Diagnostic context surfaces what was captured before the result line
    # was lost — operators can correlate with agent-server logs.
    assert "7 tool calls" in detail
    assert "12 turns" in detail
    assert "raw_messages=22" in detail
    # Hint at the typical cause (subprocess inherited stdout) so operators
    # know what to look for in the logs.
    assert "stdout" in detail.lower() or "reader thread" in detail.lower()


def test_metadata_with_no_turns_renders_zero():
    """num_turns is Optional[int]; a None value must render as 0 — not crash,
    and not omit the field — so the diagnostic stays well-formed."""
    metadata = ExecutionMetadata(tool_count=3)  # num_turns left as None

    result = _classify_empty_result(metadata, raw_message_count=10)

    assert result is not None
    _, detail = result
    assert "3 tool calls" in detail
    assert "0 turns" in detail


def test_zero_tool_count_renders_explicitly():
    """A clean exit with zero tools and an empty result is the most extreme
    case — must still classify rather than slipping through to the success
    path."""
    metadata = ExecutionMetadata()  # all fields default

    result = _classify_empty_result(metadata, raw_message_count=0)

    assert result is not None
    status_code, detail = result
    assert status_code == 502
    assert "0 tool calls" in detail
    assert "raw_messages=0" in detail


# ---------------------------------------------------------------------------
# Populated metadata — must NOT classify, so the caller proceeds to build
# the response and return 200 normally.
# ---------------------------------------------------------------------------

def test_populated_metadata_returns_none():
    """The happy path: result message arrived, cost_usd and duration_ms are
    both populated. Classifier must return None so the success path runs."""
    metadata = ExecutionMetadata(
        cost_usd=0.0123,
        duration_ms=15000,
        tool_count=4,
        num_turns=8,
    )

    assert _classify_empty_result(metadata, raw_message_count=15) is None


def test_only_cost_populated_returns_none():
    """Single-field nullability is treated as a Claude format quirk, not a
    lost result message. The classifier is conservative — only the BOTH-None
    case triggers, so callers don't get false positives on edge cases where
    one field is missing for unrelated reasons."""
    metadata = ExecutionMetadata(cost_usd=0.005, duration_ms=None, tool_count=2)

    assert _classify_empty_result(metadata, raw_message_count=8) is None


def test_only_duration_populated_returns_none():
    """Mirror of the cost-only case — single populated field is enough to
    consider the result message present."""
    metadata = ExecutionMetadata(cost_usd=None, duration_ms=8200, tool_count=2)

    assert _classify_empty_result(metadata, raw_message_count=8) is None


def test_zero_cost_is_not_treated_as_missing():
    """``cost_usd == 0.0`` is a valid populated value (e.g. a subscription
    user's $0 cost), distinct from ``None``. Must NOT be classified as
    empty — guards against ``if not metadata.cost_usd`` false positives."""
    metadata = ExecutionMetadata(cost_usd=0.0, duration_ms=5000)

    assert _classify_empty_result(metadata, raw_message_count=4) is None


def test_zero_duration_is_not_treated_as_missing():
    """Mirror: ``duration_ms == 0`` (degenerate but valid) must not trigger
    the classifier — the test is ``is None``, not ``if not value``."""
    metadata = ExecutionMetadata(cost_usd=0.001, duration_ms=0)

    assert _classify_empty_result(metadata, raw_message_count=2) is None


# ---------------------------------------------------------------------------
# raw_messages fallback: derive tool_count / num_turns when result line lost.
# Issue #531: metadata.tool_count and num_turns are also None when the result
# line is lost (they're only populated by that line). Pass raw_messages so
# the 502 detail shows honest counts.
# ---------------------------------------------------------------------------

def _make_assistant_msg(tool_use: bool) -> dict:
    """Build a minimal raw 'assistant' message, optionally with tool_use."""
    content = (
        [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]
        if tool_use
        else [{"type": "text", "text": "hello"}]
    )
    return {"type": "assistant", "message": {"content": content}}


def test_raw_messages_fallback_derives_num_turns():
    """When the result line is lost, metadata.num_turns is None (it's only
    populated from the result line). raw_messages are used to derive an
    honest turn count for the 502 detail.

    metadata.tool_count is accumulated per-message during parsing, so it
    remains accurate even without the result line and is used directly.
    """
    raw = [
        {"type": "user", "message": {"content": "go"}},
        _make_assistant_msg(tool_use=True),   # turn 1
        {"type": "user", "message": {"content": "result"}},
        _make_assistant_msg(tool_use=False),  # turn 2
        _make_assistant_msg(tool_use=True),   # turn 3
    ]
    # tool_count=5 (accumulated during parsing); num_turns=None (result line lost)
    metadata = ExecutionMetadata(tool_count=5)

    result = _classify_empty_result(metadata, raw_message_count=len(raw), raw_messages=raw)

    assert result is not None
    _, detail = result
    assert "5 tool calls" in detail  # from metadata (accumulated during parsing)
    assert "3 turns" in detail       # 3 assistant messages counted from raw_messages


def test_raw_messages_fallback_not_used_when_num_turns_present():
    """When metadata.num_turns is populated (result line arrived normally),
    raw_messages counting is skipped — metadata is authoritative."""
    raw = [
        _make_assistant_msg(tool_use=True),
        _make_assistant_msg(tool_use=True),
        _make_assistant_msg(tool_use=True),  # 3 from raw
    ]
    metadata = ExecutionMetadata(tool_count=7, num_turns=10)  # explicit counts

    result = _classify_empty_result(metadata, raw_message_count=len(raw), raw_messages=raw)

    assert result is not None
    _, detail = result
    assert "7 tool calls" in detail   # uses metadata tool_count
    assert "10 turns" in detail       # uses metadata num_turns (not raw count 3)


def test_raw_messages_empty_falls_back_to_zero():
    """No raw_messages and no metadata counts → zeros in detail, no crash."""
    metadata = ExecutionMetadata()  # all None

    result = _classify_empty_result(metadata, raw_message_count=0, raw_messages=[])

    assert result is not None
    _, detail = result
    assert "0 tool calls" in detail
    assert "0 turns" in detail


def test_raw_messages_none_falls_back_to_zero():
    """raw_messages=None (old callers) → zeros in detail, backward compat."""
    metadata = ExecutionMetadata()

    result = _classify_empty_result(metadata, raw_message_count=0, raw_messages=None)

    assert result is not None
    _, detail = result
    assert "0 tool calls" in detail


# ---------------------------------------------------------------------------
# Defensive: missing metadata.
# ---------------------------------------------------------------------------

def test_none_metadata_returns_none():
    """If metadata itself is missing, we have no signal to act on — return
    None and let the caller handle it via the existing empty-response 500
    path. The classifier should never crash on bad input."""
    assert _classify_empty_result(None, raw_message_count=0) is None
