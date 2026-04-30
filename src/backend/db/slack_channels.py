"""
Database operations for Slack multi-agent channel routing.

Handles:
- Workspace connections (one bot token per workspace, encrypted)
- Channel-agent bindings (which agent responds in which channel)
- Active thread tracking (reply-without-mention)
"""

import logging
import secrets
from datetime import datetime
from typing import Optional, List

from db.connection import get_db_connection
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class SlackChannelOperations:
    """Operations for Slack workspace connections and channel-agent bindings."""

    # =========================================================================
    # Encryption helpers (same pattern as subscription_credentials)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service."""
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_token(self, token: str) -> str:
        """Encrypt a bot token for storage."""
        svc = self._get_encryption_service()
        return svc.encrypt({"bot_token": token})

    def _decrypt_token(self, encrypted: str) -> Optional[str]:
        """Decrypt a bot token from storage. Returns None if decryption fails."""
        try:
            svc = self._get_encryption_service()
            decrypted = svc.decrypt(encrypted)
            return decrypted.get("bot_token")
        except Exception as e:
            logger.error(f"Failed to decrypt bot token: {e}")
            # Fallback: might be plaintext from before encryption was added
            if encrypted.startswith("xoxb-"):
                logger.warning("Bot token stored in plaintext — will re-encrypt on next update")
                return encrypted
            return None

    # =========================================================================
    # Workspace Operations
    # =========================================================================

    def create_workspace(
        self,
        team_id: str,
        team_name: Optional[str],
        bot_token: str,
        connected_by: Optional[str] = None
    ) -> dict:
        """Create or update a workspace connection. Bot token is encrypted at rest."""
        workspace_id = secrets.token_urlsafe(16)
        now = utc_now_iso()
        encrypted_token = self._encrypt_token(bot_token)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO slack_workspaces (id, team_id, team_name, bot_token, connected_by, connected_at, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(team_id) DO UPDATE SET
                    bot_token = excluded.bot_token,
                    team_name = excluded.team_name,
                    connected_at = excluded.connected_at
            """, (workspace_id, team_id, team_name, encrypted_token, connected_by, now))
            conn.commit()

        return self.get_workspace_by_team(team_id)

    def get_workspace_by_team(self, team_id: str) -> Optional[dict]:
        """Get workspace connection by Slack team ID. Bot token is decrypted."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, team_id, team_name, bot_token, connected_by, connected_at, enabled
                FROM slack_workspaces
                WHERE team_id = ? AND enabled = 1
            """, (team_id,))
            row = cursor.fetchone()

        if not row:
            return None

        result = self._row_to_workspace(row)
        # Decrypt the bot token
        result["bot_token"] = self._decrypt_token(result["bot_token"]) or ""
        return result

    def get_workspace_bot_token(self, team_id: str) -> Optional[str]:
        """Get decrypted bot token for a workspace."""
        ws = self.get_workspace_by_team(team_id)
        return ws["bot_token"] if ws else None

    def get_all_workspaces(self) -> List[dict]:
        """Get all enabled workspaces with decrypted bot tokens."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, team_id, team_name, bot_token, connected_by, connected_at, enabled
                FROM slack_workspaces
                WHERE enabled = 1
            """)
            rows = cursor.fetchall()

        results = []
        for row in rows:
            ws = self._row_to_workspace(row)
            ws["bot_token"] = self._decrypt_token(ws["bot_token"]) or ""
            results.append(ws)
        return results

    def delete_workspace(self, team_id: str) -> bool:
        """Delete a workspace and all its channel bindings."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM slack_channel_agents WHERE team_id = ?", (team_id,))
            cursor.execute("DELETE FROM slack_workspaces WHERE team_id = ?", (team_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    # =========================================================================
    # Channel-Agent Binding Operations
    # =========================================================================

    def bind_channel_to_agent(
        self,
        team_id: str,
        slack_channel_id: str,
        slack_channel_name: Optional[str],
        agent_name: str,
        created_by: Optional[str] = None,
        is_dm_default: bool = False,
    ) -> dict:
        """Bind a Slack channel to an agent."""
        binding_id = secrets.token_urlsafe(16)
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO slack_channel_agents
                (id, team_id, slack_channel_id, slack_channel_name, agent_name, is_dm_default, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, slack_channel_id) DO UPDATE SET
                    agent_name = excluded.agent_name,
                    slack_channel_name = excluded.slack_channel_name,
                    is_dm_default = excluded.is_dm_default
            """, (binding_id, team_id, slack_channel_id, slack_channel_name, agent_name,
                  1 if is_dm_default else 0, created_by, now))
            conn.commit()

        return self.get_channel_agent(team_id, slack_channel_id)

    def get_channel_agent(self, team_id: str, slack_channel_id: str) -> Optional[dict]:
        """Get which agent is bound to a channel."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, team_id, slack_channel_id, slack_channel_name, agent_name,
                       is_dm_default, created_by, created_at
                FROM slack_channel_agents
                WHERE team_id = ? AND slack_channel_id = ?
            """, (team_id, slack_channel_id))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_channel_agent(row)

    def get_agent_name_for_channel(self, team_id: str, slack_channel_id: str) -> Optional[str]:
        """Get agent name for a channel (fast lookup)."""
        binding = self.get_channel_agent(team_id, slack_channel_id)
        return binding["agent_name"] if binding else None

    def get_dm_default_agent(self, team_id: str) -> Optional[str]:
        """Get the default agent for DMs in a workspace."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM slack_channel_agents
                WHERE team_id = ? AND is_dm_default = 1
                LIMIT 1
            """, (team_id,))
            row = cursor.fetchone()
        return row[0] if row else None

    def set_dm_default(self, team_id: str, agent_name: str) -> bool:
        """Make ``agent_name`` the DM-default for the workspace.

        Single transaction: clear all existing flags, then set on the target.
        Avoids any window where two agents would both look like the default
        (the schema has no exclusivity constraint, so the read-side falls
        back to ``LIMIT 1`` and would pick non-deterministically).

        Returns True if the target row was updated, False if the agent is
        not bound in this workspace (caller should 404).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                cursor.execute(
                    "UPDATE slack_channel_agents SET is_dm_default = 0 WHERE team_id = ?",
                    (team_id,),
                )
                cursor.execute(
                    """UPDATE slack_channel_agents SET is_dm_default = 1
                         WHERE team_id = ? AND agent_name = ?""",
                    (team_id, agent_name),
                )
                changed = cursor.rowcount > 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return changed

    def get_agents_for_workspace(self, team_id: str) -> List[dict]:
        """Get all agent-channel bindings for a workspace."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, team_id, slack_channel_id, slack_channel_name, agent_name,
                       is_dm_default, created_by, created_at
                FROM slack_channel_agents
                WHERE team_id = ?
                ORDER BY created_at ASC
            """, (team_id,))
            rows = cursor.fetchall()

        return [self._row_to_channel_agent(row) for row in rows]

    def get_channel_for_agent(self, team_id: str, agent_name: str) -> Optional[dict]:
        """Get channel binding for a specific agent in a workspace."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, team_id, slack_channel_id, slack_channel_name, agent_name,
                       is_dm_default, created_by, created_at
                FROM slack_channel_agents
                WHERE team_id = ? AND agent_name = ?
            """, (team_id, agent_name))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_channel_agent(row)

    def unbind_agent(self, team_id: str, agent_name: str) -> bool:
        """Remove an agent's channel binding.

        Pure delete — does not auto-promote a new DM default. The router
        layer is responsible for refusing to unbind the current DM default
        while other agents are still bound (#584). When the unbind target
        is the only agent in the workspace, the binding is removed cleanly
        and the workspace ends up with no Slack agents at all.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM slack_channel_agents
                WHERE team_id = ? AND agent_name = ?
            """, (team_id, agent_name))
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    def unbind_channel(self, team_id: str, slack_channel_id: str) -> bool:
        """Remove a channel's agent binding."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM slack_channel_agents
                WHERE team_id = ? AND slack_channel_id = ?
            """, (team_id, slack_channel_id))
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_workspace(self, row) -> dict:
        return {
            "id": row[0],
            "team_id": row[1],
            "team_name": row[2],
            "bot_token": row[3],
            "connected_by": row[4],
            "connected_at": row[5],
            "enabled": bool(row[6]),
        }

    # =========================================================================
    # Active Thread Operations (for reply-without-mention)
    # =========================================================================

    def register_active_thread(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        agent_name: str,
    ) -> None:
        """Record that the bot responded in a thread (enables reply-without-mention)."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO slack_active_threads
                (team_id, channel_id, thread_ts, agent_name, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (team_id, channel_id, thread_ts, agent_name, now))
            conn.commit()

    def is_active_thread(self, team_id: str, channel_id: str, thread_ts: str) -> Optional[str]:
        """Check if a thread is active (bot participated). Returns agent_name or None."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM slack_active_threads
                WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
            """, (team_id, channel_id, thread_ts))
            row = cursor.fetchone()
        return row[0] if row else None

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_channel_agent(self, row) -> dict:
        return {
            "id": row[0],
            "team_id": row[1],
            "slack_channel_id": row[2],
            "slack_channel_name": row[3],
            "agent_name": row[4],
            "is_dm_default": bool(row[5]),
            "created_by": row[6],
            "created_at": row[7],
        }
