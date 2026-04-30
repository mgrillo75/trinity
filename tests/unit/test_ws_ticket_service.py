"""
Unit tests for WebSocket ticket auth (#550).

Covers ``services.ws_ticket_service``:

- ``mint_ticket`` writes a ticket-keyed entry to Redis with the
  configured TTL and returns the opaque token.
- ``consume_ticket`` returns the principal exactly once (single-use
  via ``GETDEL``).
- Missing / malformed / expired tickets return ``None``.
- Redis-down behavior fails closed (mint raises, consume returns
  ``None``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


class _FakeRedis:
    """In-memory stand-in for the live Redis client.

    Implements the subset our service actually calls: ``setex``,
    ``getdel``. TTLs are tracked but treated as a one-shot expire flag
    (the ``expire_now`` helper).
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value
        self.ttl[key] = ttl

    def getdel(self, key: str):
        return self.store.pop(key, None)

    def expire_now(self, key: str) -> None:
        self.store.pop(key, None)
        self.ttl.pop(key, None)


@pytest.fixture
def ticket_service(monkeypatch):
    """Load ``services.ws_ticket_service`` with a stub ``routers.auth``.

    Loading the module via the regular package path triggers FastAPI
    + jose imports we don't want in a unit test. We stub the ``routers``
    package and the only function the service needs from it
    (``get_redis_client``).
    """
    fake = _FakeRedis()
    routers_pkg = types.ModuleType("routers")
    routers_pkg.__path__ = []
    auth_stub = types.ModuleType("routers.auth")
    auth_stub.get_redis_client = lambda: fake
    monkeypatch.setitem(sys.modules, "routers", routers_pkg)
    monkeypatch.setitem(sys.modules, "routers.auth", auth_stub)

    spec = importlib.util.spec_from_file_location(
        "ws_ticket_service_under_test",
        str(_BACKEND / "services" / "ws_ticket_service.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, fake, auth_stub


def test_mint_writes_ticket_with_ttl(ticket_service):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("alice", scope="user")

    assert isinstance(ticket, str) and len(ticket) >= 32
    key = f"ws_ticket:{ticket}"
    assert fake.store[key]
    assert fake.ttl[key] == 30
    payload = json.loads(fake.store[key])
    assert payload == {"sub": "alice", "scope": "user"}


def test_consume_returns_principal_then_invalidates(ticket_service):
    module, _fake, _ = ticket_service
    ticket = module.mint_ticket("bob")

    first = module.consume_ticket(ticket)
    second = module.consume_ticket(ticket)

    assert first == {"sub": "bob", "scope": "user"}
    assert second is None  # single-use: second exchange fails


def test_consume_unknown_ticket_returns_none(ticket_service):
    module, _fake, _ = ticket_service
    assert module.consume_ticket("never-issued") is None
    assert module.consume_ticket("") is None
    assert module.consume_ticket(None) is None


def test_consume_expired_ticket_returns_none(ticket_service):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("carol")
    fake.expire_now(f"ws_ticket:{ticket}")

    assert module.consume_ticket(ticket) is None


def test_mint_raises_when_redis_unavailable(ticket_service, monkeypatch):
    # The service binds ``get_redis_client`` at import time via
    # ``from routers.auth import get_redis_client``, so we patch the
    # name as it lives in the service module — not on the auth stub.
    module, _fake, _auth_stub = ticket_service
    monkeypatch.setattr(module, "get_redis_client", lambda: None)

    with pytest.raises(RuntimeError, match="Redis unavailable"):
        module.mint_ticket("alice")


def test_consume_returns_none_when_redis_unavailable(ticket_service, monkeypatch):
    module, _fake, _auth_stub = ticket_service
    monkeypatch.setattr(module, "get_redis_client", lambda: None)

    assert module.consume_ticket("anything") is None


def test_consume_handles_malformed_payload(ticket_service):
    module, fake, _ = ticket_service
    fake.store["ws_ticket:badjson"] = "not-json"

    assert module.consume_ticket("badjson") is None
    # Even malformed, the GETDEL still removed the entry — defensive.
    assert "ws_ticket:badjson" not in fake.store
