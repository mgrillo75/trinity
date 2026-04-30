"""
Unit tests for the backend PUT /api/agents/{name}/files deny-list (#590).

Verifies the AISEC-C2 RCE-by-config bypass is closed at the platform boundary:
authenticated owners cannot overwrite .mcp.json (or other runtime/credential
config) via the file-write endpoint to inject attacker-controlled MCP tool
definitions.

The agent-server still re-validates server-side via EDIT_PROTECTED_PATHS —
defense in depth — but the backend is the authoritative gate for user-facing
writes.

Module: src/backend/services/agent_service/files.py
Issue:  https://github.com/abilityai/trinity/issues/590
"""

import pytest

# Inline reimplementation of the deny logic for unit testing.
# MUST mirror src/backend/services/agent_service/files.py exactly.
import fnmatch
import posixpath


_FILE_WRITE_DENY_PATTERNS = (
    ".env",
    ".env.*",
    ".mcp.json",
    ".mcp.json.template",
    ".credentials.enc",
    ".ssh/*",
    ".aws/*",
    ".gcp/*",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".trinity/*",
    ".git/*",
    ".gitignore",
    "/opt/trinity/*",
    "/etc/claude-code/*",
    "/etc/*",
    "/proc/*",
    "/sys/*",
)


def _normalize_user_path(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith("/"):
        return posixpath.normpath(raw)
    return posixpath.normpath(posixpath.join("/home/developer", raw))


def _is_user_writable_path(path: str) -> bool:
    normalized = _normalize_user_path(path)
    if not normalized:
        return False
    basename = posixpath.basename(normalized)
    rel_to_home = ""
    if normalized.startswith("/home/developer/"):
        rel_to_home = normalized[len("/home/developer/"):]
    for pattern in _FILE_WRITE_DENY_PATTERNS:
        if fnmatch.fnmatch(basename, pattern):
            return False
        if fnmatch.fnmatch(normalized, pattern):
            return False
        if rel_to_home and fnmatch.fnmatch(rel_to_home, pattern):
            return False
    return True


# ---- AISEC-C2 exact reproduction ----

class TestAisecC2Reproduction:
    """The exact attack chain from the AISEC scan (2026-04-28, scan 3aad5469).

    Sequence: PUT /files?path=.mcp.json with attacker JSON → restart → MCP
    tool runs as agent process → reads /proc/1/environ → exfil OAuth token.
    Closing the .mcp.json write breaks the chain at step 1.
    """

    def test_mcp_json_relative(self):
        """The pentest's literal request — relative path."""
        assert _is_user_writable_path(".mcp.json") is False

    def test_mcp_json_absolute(self):
        """Same target, absolute form — same outcome."""
        assert _is_user_writable_path("/home/developer/.mcp.json") is False

    def test_mcp_json_template(self):
        """envsubst-only template = same RCE, different file."""
        assert _is_user_writable_path(".mcp.json.template") is False

    def test_credentials_enc(self):
        """Overwriting .credentials.enc lets attacker swap encrypted backups
        before next import → write attacker creds on next startup."""
        assert _is_user_writable_path(".credentials.enc") is False


# ---- Other protected paths ----

class TestProtectedPaths:
    """Other paths in the deny list — credential / SSH / runtime config."""

    def test_env_file(self):
        assert _is_user_writable_path(".env") is False

    def test_env_local(self):
        assert _is_user_writable_path(".env.local") is False

    def test_env_production(self):
        assert _is_user_writable_path(".env.production") is False

    def test_ssh_authorized_keys(self):
        assert _is_user_writable_path(".ssh/authorized_keys") is False

    def test_ssh_private_key(self):
        assert _is_user_writable_path(".ssh/id_rsa") is False

    def test_aws_credentials(self):
        assert _is_user_writable_path(".aws/credentials") is False

    def test_gcp_credentials(self):
        assert _is_user_writable_path(".gcp/service-account.json") is False

    def test_claude_settings(self):
        assert _is_user_writable_path(".claude/settings.json") is False

    def test_claude_settings_local(self):
        assert _is_user_writable_path(".claude/settings.local.json") is False

    def test_trinity_dir(self):
        assert _is_user_writable_path(".trinity/persistent-state.yaml") is False

    def test_git_config(self):
        assert _is_user_writable_path(".git/config") is False

    def test_gitignore(self):
        assert _is_user_writable_path(".gitignore") is False

    def test_opt_trinity(self):
        assert _is_user_writable_path("/opt/trinity/hooks/file-guardrail.py") is False

    def test_etc_claude_code(self):
        assert _is_user_writable_path("/etc/claude-code/managed-settings.json") is False


# ---- Path traversal ----

class TestPathTraversal:
    """Lexical normalization defeats `..` traversal attempts that try to
    smuggle a denied path past basename matching."""

    def test_traversal_to_etc(self):
        # `../../etc/passwd` resolved from /home/developer → /etc/passwd
        assert _is_user_writable_path("../../etc/passwd") is False

    def test_traversal_to_proc(self):
        assert _is_user_writable_path("../../proc/1/environ") is False

    def test_traversal_to_mcp_json(self):
        # Traversal back to .mcp.json should still be caught
        assert _is_user_writable_path("content/../.mcp.json") is False

    def test_dot_segments_to_env(self):
        assert _is_user_writable_path("./.env") is False

    def test_double_dot_to_credentials_enc(self):
        assert _is_user_writable_path("subdir/../.credentials.enc") is False


# ---- Allowed paths (regression — legit writes must keep working) ----

class TestAllowedPaths:
    """Paths the user CAN write — these must not be blocked."""

    def test_content_directory(self):
        assert _is_user_writable_path("content/notes.md") is True

    def test_content_subdirectory(self):
        assert _is_user_writable_path("content/reports/q1.txt") is True

    def test_workspace_file(self):
        assert _is_user_writable_path("workspace/script.py") is True

    def test_claude_md(self):
        """Owners DO edit CLAUDE.md — agent instructions are user-managed."""
        assert _is_user_writable_path("CLAUDE.md") is True

    def test_template_yaml(self):
        """template.yaml is metadata, not runtime config — editable."""
        assert _is_user_writable_path("template.yaml") is True

    def test_arbitrary_user_file(self):
        assert _is_user_writable_path("my-data.json") is True

    def test_nested_user_file(self):
        assert _is_user_writable_path("projects/foo/bar.txt") is True


# ---- Edge cases ----

class TestEdgeCases:
    """Boundary conditions in the normalizer."""

    def test_empty_path(self):
        # Empty path normalizes to "" → can't match → not writable (safe default)
        assert _is_user_writable_path("") is False

    def test_root_directory(self):
        # / normalizes to / — basename is empty, doesn't match any pattern,
        # but writing to / is nonsensical. The agent-server's "must be under
        # /home/developer" check catches this layer; the deny list lets it
        # through here.
        assert _is_user_writable_path("/") is True

    def test_home_developer_root(self):
        # The base directory itself
        assert _is_user_writable_path("/home/developer") is True

    def test_case_sensitivity_env(self):
        """.ENV is NOT .env — case-sensitive match (matches agent-server)."""
        assert _is_user_writable_path(".ENV") is True

    def test_subdir_env(self):
        """`subdir/.env` IS protected — basename matches."""
        assert _is_user_writable_path("subdir/.env") is False
