"""Regression for #523: the terminate endpoint must dispatch the
synchronous registry.terminate() call to a thread-pool executor so the
asyncio event loop stays responsive to concurrent /health probes.

registry.terminate() does up to 7s of process.wait() (5s SIGINT grace +
2s SIGKILL grace). Calling it directly from an async route handler
blocks the event loop for that entire window — concurrent /health
requests time out, the backend circuit breaker opens, and UI fan-out
hangs. The fix wraps the call in loop.run_in_executor; this test pins
that contract so it cannot regress.

Module under test:
    docker/base-image/agent_server/routers/chat.py::terminate_execution
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import the router without booting the full agent_server package.
# Same shim pattern as test_signal_exit_classification.py.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

if "agent_server" not in sys.modules:
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [str(_AGENT_SERVER_DIR)]
    sys.modules["agent_server"] = _stub

# Bypass routers/__init__.py — it imports every router module (snapshot,
# git, etc.) and pulls in optional deps like python-multipart that this
# test doesn't need. A namespace-package shim lets chat.py be imported
# in isolation.
if "agent_server.routers" not in sys.modules:
    _routers_stub = types.ModuleType("agent_server.routers")
    _routers_stub.__path__ = [str(_AGENT_SERVER_DIR / "routers")]
    sys.modules["agent_server.routers"] = _routers_stub

from agent_server.routers.chat import terminate_execution  # noqa: E402


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminate_dispatches_to_executor():
    """registry.terminate must run on a thread different from the event
    loop thread. If someone reverts the run_in_executor wrap and calls
    registry.terminate synchronously, this test fails."""
    main_thread_id = threading.get_ident()
    terminate_thread_ids: list[int] = []

    def fake_terminate(execution_id):
        terminate_thread_ids.append(threading.get_ident())
        # Simulate process.wait() so we can also observe the event loop
        # is responsive while the call is in flight.
        time.sleep(0.3)
        return {"success": True, "returncode": 0}

    fake_registry = MagicMock()
    fake_registry.terminate = fake_terminate

    with patch(
        "agent_server.routers.chat.get_process_registry",
        return_value=fake_registry,
    ):
        async def quick_yield_after_dispatch():
            # Let terminate dispatch to the executor first.
            await asyncio.sleep(0.05)
            # Now measure how long a single yield takes. If the event
            # loop is blocked, this will be roughly the remaining sleep
            # duration (~0.25s). If properly dispatched, sub-millisecond.
            t = time.monotonic()
            await asyncio.sleep(0)
            return time.monotonic() - t

        terminate_task = asyncio.create_task(terminate_execution("test-exec"))
        yield_elapsed = await quick_yield_after_dispatch()
        result = await terminate_task

    assert terminate_thread_ids, "fake_terminate was never invoked"
    assert terminate_thread_ids[0] != main_thread_id, (
        "registry.terminate ran on the event loop thread — synchronous "
        "process.wait() will block /health (#523 regression)"
    )
    assert yield_elapsed < 0.05, (
        f"event loop blocked for {yield_elapsed:.3f}s during terminate; "
        f"expected sub-millisecond yield (#523 regression)"
    )
    assert result == {"status": "terminated", "execution_id": "test-exec"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminate_passes_through_not_found():
    """registry.terminate -> {success: False, reason: not_found} must
    surface as HTTP 404 even after the executor wrap."""
    from fastapi import HTTPException

    fake_registry = MagicMock()
    fake_registry.terminate = lambda execution_id: {
        "success": False,
        "reason": "not_found",
    }

    with patch(
        "agent_server.routers.chat.get_process_registry",
        return_value=fake_registry,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await terminate_execution("missing-exec")

    assert exc_info.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminate_passes_through_already_finished():
    """A process that already exited must surface as 200 with status
    'already_finished' — same shape as before the executor wrap."""
    fake_registry = MagicMock()
    fake_registry.terminate = lambda execution_id: {
        "success": False,
        "reason": "already_finished",
        "returncode": 0,
    }

    with patch(
        "agent_server.routers.chat.get_process_registry",
        return_value=fake_registry,
    ):
        result = await terminate_execution("already-done")

    assert result == {
        "status": "already_finished",
        "execution_id": "already-done",
        "returncode": 0,
    }
