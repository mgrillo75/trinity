"""
Unit tests for check_public_folder_mount_matches (amazing-file-outbound Step 2).

The helper decides whether an agent container's current /home/developer/public
mount matches its file_sharing_enabled flag. This drives container recreation
on start. Four-case truth table:

| flag    | mount present | result |
|---------|---------------|--------|
| True    | Yes           | True   |  (aligned, no recreation)
| True    | No            | False  |  (needs recreation → attach)
| False   | Yes           | False  |  (needs recreation → detach)
| False   | No            | True   |  (aligned)

Plus some adversarial cases: similar-looking paths, empty mount list, etc.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


@pytest.fixture
def helpers_module(monkeypatch):
    """Load file_sharing.py with `database` and adjacent imports stubbed.

    services/agent_service/file_sharing.py imports:
      - database.db  → we stub with MagicMock
      - models.User  → we stub with object
      - fastapi.HTTPException  → passthrough real fastapi if available,
        else stub (the function under test doesn't raise it)
      - services.docker_service.get_agent_container  → unused by the
        function under test; stub with MagicMock
    """
    stub_db = MagicMock()
    stub_db.get_public_mount_path.return_value = "/home/developer/public"

    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(db=stub_db))
    monkeypatch.setitem(sys.modules, "models", SimpleNamespace(User=object))
    monkeypatch.setitem(
        sys.modules,
        "services.docker_service",
        SimpleNamespace(get_agent_container=MagicMock()),
    )
    # Stub fastapi if not installed on host (harmless if already there)
    if "fastapi" not in sys.modules:
        monkeypatch.setitem(
            sys.modules,
            "fastapi",
            SimpleNamespace(HTTPException=type("HTTPException", (Exception,), {})),
        )

    spec = importlib.util.spec_from_file_location(
        "_ams_file_sharing_svc",
        _BACKEND / "services" / "agent_service" / "file_sharing.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ams_file_sharing_svc"] = mod
    spec.loader.exec_module(mod)

    return mod, stub_db


def _container_with(destinations: list[str]):
    """Build a minimal fake container whose attrs expose the given mounts."""
    return SimpleNamespace(
        attrs={"Mounts": [{"Destination": d} for d in destinations]}
    )


# ---------------------------------------------------------------------------
# Four-case truth table
# ---------------------------------------------------------------------------


def test_enabled_and_mounted_returns_true(helpers_module):
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = True
    container = _container_with(["/home/developer/public"])
    assert mod.check_public_folder_mount_matches(container, "a1") is True


def test_enabled_and_not_mounted_returns_false(helpers_module):
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = True
    container = _container_with(["/home/developer/workspace"])
    assert mod.check_public_folder_mount_matches(container, "a1") is False


def test_disabled_and_mounted_returns_false(helpers_module):
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = False
    container = _container_with(["/home/developer/public"])
    assert mod.check_public_folder_mount_matches(container, "a1") is False


def test_disabled_and_not_mounted_returns_true(helpers_module):
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = False
    container = _container_with([])
    assert mod.check_public_folder_mount_matches(container, "a1") is True


# ---------------------------------------------------------------------------
# Adversarial / edge cases
# ---------------------------------------------------------------------------


def test_similar_path_does_not_count_as_public_mount(helpers_module):
    """A mount at /home/developer/public-backup must NOT satisfy the check."""
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = True
    container = _container_with(["/home/developer/public-backup"])
    assert mod.check_public_folder_mount_matches(container, "a1") is False


def test_nested_path_does_not_count_as_public_mount(helpers_module):
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = True
    container = _container_with(["/home/developer/public/inner"])
    assert mod.check_public_folder_mount_matches(container, "a1") is False


def test_container_without_mounts_key(helpers_module):
    """attrs may legitimately miss 'Mounts' on a stopped/brand-new container."""
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = False
    container = SimpleNamespace(attrs={})
    assert mod.check_public_folder_mount_matches(container, "a1") is True


def test_reads_flag_per_call(helpers_module):
    """DB value is re-read every call — no hidden caching."""
    mod, stub_db = helpers_module
    container = _container_with(["/home/developer/public"])
    stub_db.get_file_sharing_enabled.return_value = True
    assert mod.check_public_folder_mount_matches(container, "a1") is True
    stub_db.get_file_sharing_enabled.return_value = False
    assert mod.check_public_folder_mount_matches(container, "a1") is False
    assert stub_db.get_file_sharing_enabled.call_count == 2


def test_other_mounts_do_not_interfere(helpers_module):
    """An agent may have shared-out, shared-in/*, workspace mounts alongside."""
    mod, stub_db = helpers_module
    stub_db.get_file_sharing_enabled.return_value = True
    container = _container_with([
        "/home/developer/shared-out",
        "/home/developer/shared-in/peer",
        "/home/developer/workspace",
        "/home/developer/public",
    ])
    assert mod.check_public_folder_mount_matches(container, "a1") is True
