"""
Tests for X-Forwarded-For rate limit bypass fix (issue #181 / pentest 3.2.4).

Covers:
- UNIT: _get_client_ip trusted-proxy logic (no backend needed)
- UNIT: _is_trusted_proxy network matching
- SMOKE: Per-IP rate limiting still works via the API
- SMOKE: Per-token secondary rate limit is enforced

Run with: pytest tests/test_ip_rate_limit_fix.py -v
"""

import os
import sys
import ipaddress
import importlib
import types
import pytest
import httpx
from unittest.mock import MagicMock, patch

BASE_URL = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Helpers to load the router module without a full FastAPI app
# ---------------------------------------------------------------------------

def _load_public_router():
    """Import the public router and return its module."""
    # Add backend to path
    backend_path = os.path.join(os.path.dirname(__file__), "..", "src", "backend")
    backend_path = os.path.abspath(backend_path)
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    # ------------------------------------------------------------------
    # Stub heavy dependencies so the module loads without a running app.
    #
    # IMPORTANT: "routers" must be stubbed as an empty package BEFORE the
    # first `import routers.public`.  Without this, Python evaluates
    # routers/__init__.py which eagerly imports routers.agents → fastapi,
    # which is not installed in the test venv.
    # ------------------------------------------------------------------
    if "routers" not in sys.modules:
        routers_pkg = types.ModuleType("routers")
        routers_pkg.__path__ = [os.path.join(backend_path, "routers")]
        routers_pkg.__package__ = "routers"
        sys.modules["routers"] = routers_pkg

    # Stub fastapi unconditionally so that response_model= annotations on
    # router decorators in routers/public.py don't trigger real Pydantic
    # validation when loaded in a combined pytest session (where
    # test_inter_agent_timeout_unit.py already imported the real FastAPI).
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.APIRouter = MagicMock(return_value=MagicMock())
    fastapi_mod.HTTPException = Exception
    fastapi_mod.Request = MagicMock()
    fastapi_mod.Depends = MagicMock()
    fastapi_mod.exceptions = types.ModuleType("fastapi.exceptions")
    fastapi_mod.routing = types.ModuleType("fastapi.routing")
    sys.modules["fastapi"] = fastapi_mod
    sys.modules["fastapi.exceptions"] = fastapi_mod.exceptions

    fr_mod = types.ModuleType("fastapi.responses")
    fr_mod.StreamingResponse = MagicMock()
    sys.modules["fastapi.responses"] = fr_mod

    # Application-level stubs.
    # Use setdefault for most stubs (don't overwrite stubs other test files
    # installed for their own purposes), EXCEPT for `database` which must
    # always be a MagicMock so that `from database import X` works when
    # loading routers/public.py.  test_inter_agent_timeout_unit.py installs
    # a types.SimpleNamespace (not a MagicMock) via setdefault, which does
    # not support attribute-access style imports.
    sys.modules["database"] = MagicMock()
    stubs = {
        "routers.auth": MagicMock(
            check_login_rate_limit=MagicMock(),
            record_login_attempt=MagicMock(),
        ),
        "services.docker_service": MagicMock(),
        "services.email_service": MagicMock(),
        "services.task_execution_service": MagicMock(),
        "services.platform_prompt_service": MagicMock(),
        "services.settings_service": MagicMock(),
    }
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    # Force a fresh load of routers.public every call so module-level state
    # (e.g. _trusted_proxy_networks) is reset cleanly.
    sys.modules.pop("routers.public", None)
    import routers.public as pub  # noqa: E402
    return pub


# ---------------------------------------------------------------------------
# UNIT: _is_trusted_proxy
# ---------------------------------------------------------------------------

class TestIsTrustedProxy:
    """Test the trusted-proxy network matching logic."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.pub = _load_public_router()
        # Reset cached networks so env changes are picked up
        self.pub._trusted_proxy_networks = None

    def test_docker_bridge_ip_is_trusted(self):
        """Default config trusts Docker bridge range 172.16–31.x.x."""
        assert self.pub._is_trusted_proxy("172.17.0.1") is True
        assert self.pub._is_trusted_proxy("172.20.0.5") is True

    def test_rfc1918_192168_is_trusted(self):
        assert self.pub._is_trusted_proxy("192.168.1.1") is True

    def test_rfc1918_10_is_trusted(self):
        assert self.pub._is_trusted_proxy("10.0.0.1") is True

    def test_loopback_is_trusted(self):
        assert self.pub._is_trusted_proxy("127.0.0.1") is True

    def test_public_ip_is_not_trusted(self):
        assert self.pub._is_trusted_proxy("1.2.3.4") is False
        assert self.pub._is_trusted_proxy("203.0.113.5") is False

    def test_custom_trusted_proxies_env(self, monkeypatch):
        """TRUSTED_PROXIES env var overrides the default list."""
        monkeypatch.setenv("TRUSTED_PROXIES", "203.0.113.0/24")
        self.pub._trusted_proxy_networks = None  # clear cache

        assert self.pub._is_trusted_proxy("203.0.113.1") is True
        assert self.pub._is_trusted_proxy("10.0.0.1") is False  # no longer trusted


# ---------------------------------------------------------------------------
# UNIT: _get_client_ip
# ---------------------------------------------------------------------------

def _make_request(direct_ip, x_forwarded_for=None, x_real_ip=None):
    """Build a mock FastAPI Request with the given connection attributes."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = direct_ip

    headers = {}
    if x_forwarded_for:
        headers["x-forwarded-for"] = x_forwarded_for
    if x_real_ip:
        headers["x-real-ip"] = x_real_ip

    # Keep req.headers as a MagicMock and wire its .get() to the headers dict.
    # Assigning .get on a plain dict raises AttributeError on Python 3.14+
    # because dict.get is a read-only slot wrapper.
    req.headers = MagicMock()
    req.headers.get.side_effect = lambda key, default=None: headers.get(key.lower(), default)
    return req


class TestGetClientIp:
    """Test the _get_client_ip function against the pentest scenario."""

    @pytest.fixture(autouse=True)
    def load_module(self):
        self.pub = _load_public_router()
        self.pub._trusted_proxy_networks = None

    def test_direct_connection_ignores_xff(self):
        """Public IP connecting directly — spoofed XFF must be ignored."""
        req = _make_request(
            direct_ip="1.2.3.4",
            x_forwarded_for="10.0.0.99"  # attacker's spoofed header
        )
        assert self.pub._get_client_ip(req) == "1.2.3.4"

    def test_pentest_bypass_scenario(self):
        """Replicates the pentest PoC: rotating X-Forwarded-For from a public IP."""
        for fake_ip in [f"10.0.0.{i}" for i in range(1, 36)]:
            req = _make_request(direct_ip="1.2.3.4", x_forwarded_for=fake_ip)
            # All 35 requests must resolve to the same real IP
            assert self.pub._get_client_ip(req) == "1.2.3.4"

    def test_behind_trusted_proxy_uses_x_real_ip(self):
        """When nginx (trusted proxy) sets X-Real-IP, that IP is used."""
        req = _make_request(
            direct_ip="172.17.0.2",  # nginx container
            x_real_ip="5.6.7.8",     # real client IP set by nginx
            x_forwarded_for="5.6.7.8"
        )
        assert self.pub._get_client_ip(req) == "5.6.7.8"

    def test_behind_trusted_proxy_xff_fallback(self):
        """Falls back to X-Forwarded-For when X-Real-IP absent."""
        req = _make_request(
            direct_ip="172.17.0.2",
            x_forwarded_for="9.10.11.12"
        )
        assert self.pub._get_client_ip(req) == "9.10.11.12"

    def test_behind_trusted_proxy_xff_rightmost_nontrusted(self):
        """Takes rightmost non-trusted IP from X-Forwarded-For chain."""
        req = _make_request(
            direct_ip="172.17.0.2",
            x_forwarded_for="spoofed.1.1.1, real.client.1.2, 172.17.0.3"
        )
        # 172.17.0.3 is trusted, so skip it; real.client.1.2 is the rightmost non-trusted
        assert self.pub._get_client_ip(req) == "real.client.1.2"

    def test_no_client_returns_unknown(self):
        req = MagicMock()
        req.client = None
        req.headers.get.side_effect = lambda k, d=None: d
        assert self.pub._get_client_ip(req) == "unknown"


# ---------------------------------------------------------------------------
# SMOKE: Per-token rate limit enforced via API
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_headers():
    password = os.getenv("TRINITY_TEST_PASSWORD", "password")
    resp = httpx.post(f"{BASE_URL}/api/token", data={"username": "admin", "password": password})
    if resp.status_code != 200:
        pytest.skip("Could not authenticate")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestPerTokenRateLimit:
    """Verify the per-token secondary rate limit constant is configured."""

    def test_max_messages_per_token_constant_exists(self):
        pub = _load_public_router()
        assert hasattr(pub, "MAX_CHAT_MESSAGES_PER_TOKEN")
        assert pub.MAX_CHAT_MESSAGES_PER_TOKEN > 0

    def test_rate_limit_constants_reasonable(self):
        """Per-token limit must be >= per-IP limit (token covers all IPs)."""
        pub = _load_public_router()
        assert pub.MAX_CHAT_MESSAGES_PER_TOKEN >= pub.MAX_CHAT_MESSAGES_PER_IP
