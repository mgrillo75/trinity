"""
Agent ownership and access control database operations.

Core ownership management stays here. All other concerns are delegated
to focused mixin classes in db/agent_settings/:
- SharingMixin: Agent sharing operations
- ResourcesMixin: Memory, CPU, timeout, parallel capacity
- SecurityMixin: Full capabilities, read-only mode
- AutonomyMixin: Autonomy mode, API key settings
- AvatarMixin: Avatar identity management
- MetadataMixin: Batch queries, rename operations
- GitPATMixin: Per-agent GitHub PAT management (#347)
"""

import sqlite3
from datetime import datetime
from typing import Optional, List, Dict

from .connection import get_db_connection
from .agent_settings import (
    SharingMixin,
    ResourcesMixin,
    SecurityMixin,
    AutonomyMixin,
    AvatarMixin,
    MetadataMixin,
    AccessPolicyMixin,
    GitPATMixin,
    FileSharingMixin,
)
from utils.helpers import utc_now_iso

# System agent name constant
SYSTEM_AGENT_NAME = "trinity-system"


class AgentOperations(
    SharingMixin,
    ResourcesMixin,
    SecurityMixin,
    AutonomyMixin,
    AvatarMixin,
    MetadataMixin,
    AccessPolicyMixin,
    GitPATMixin,
    FileSharingMixin,
):
    """Agent ownership, access control, and settings database operations.

    Core ownership methods are defined directly on this class.
    All other concerns are provided by mixin classes from db/agent_settings/.
    """

    def __init__(self, user_ops):
        """Initialize with reference to user operations for lookups."""
        self._user_ops = user_ops

    # =========================================================================
    # Agent Ownership Management
    # =========================================================================

    def register_agent_owner(self, agent_name: str, owner_username: str, is_system: bool = False) -> bool:
        """Register the owner of an agent.

        Args:
            agent_name: Name of the agent
            owner_username: Username of the owner
            is_system: True for system agents (deletion-protected)
        """
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO agent_ownership (agent_name, owner_id, created_at, is_system)
                    VALUES (?, ?, ?, ?)
                """, (agent_name, user["id"], utc_now_iso(), 1 if is_system else 0))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Agent already registered - update is_system flag if needed
                if is_system:
                    cursor.execute("""
                        UPDATE agent_ownership SET is_system = 1 WHERE agent_name = ?
                    """, (agent_name,))
                    conn.commit()
                return False

    def get_agent_owner(self, agent_name: str) -> Optional[Dict]:
        """Get the owner of an agent, including is_system flag."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ao.id, ao.agent_name, ao.owner_id, u.username as owner_username,
                       ao.created_at, COALESCE(ao.is_system, 0) as is_system
                FROM agent_ownership ao
                JOIN users u ON ao.owner_id = u.id
                WHERE ao.agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                result = dict(row)
                result["is_system"] = bool(result.get("is_system", 0))
                return result
            return None

    def get_agents_by_owner(self, owner_username: str) -> List[str]:
        """Get all agent names owned by a user."""
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return []

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM agent_ownership WHERE owner_id = ?
            """, (user["id"],))
            return [row["agent_name"] for row in cursor.fetchall()]

    def delete_agent_ownership(self, agent_name: str) -> bool:
        """Remove agent ownership record, all sharing records, and pending access requests."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Delete sharing records first (cascade)
            cursor.execute("DELETE FROM agent_sharing WHERE agent_name = ?", (agent_name,))
            # Delete access requests (issue #311)
            cursor.execute("DELETE FROM access_requests WHERE agent_name = ?", (agent_name,))
            # Delete ownership record
            cursor.execute("DELETE FROM agent_ownership WHERE agent_name = ?", (agent_name,))
            conn.commit()
            return cursor.rowcount > 0

    def can_user_access_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can access an agent (owner, shared, or admin)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        # Admins can access all agents
        if user["role"] == "admin":
            return True

        # Check if user is the owner
        owner = self.get_agent_owner(agent_name)
        if owner and owner["owner_username"] == username:
            return True

        # Check if agent is shared with user
        if self.is_agent_shared_with_user(agent_name, username):
            return True

        return False

    def can_user_delete_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can delete an agent (owner or admin, but NOT system agents)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        # Check if this is a system agent - NO ONE can delete system agents
        owner = self.get_agent_owner(agent_name)
        if owner and owner.get("is_system", False):
            return False

        # Admins can delete any non-system agent
        if user["role"] == "admin":
            return True

        # Owners can delete their own non-system agents
        if owner and owner["owner_username"] == username:
            return True

        return False

    def is_system_agent(self, agent_name: str) -> bool:
        """Check if an agent is a system agent (deletion-protected)."""
        # Quick check by name
        if agent_name == SYSTEM_AGENT_NAME:
            return True
        # Check database flag
        owner = self.get_agent_owner(agent_name)
        return owner.get("is_system", False) if owner else False

    # =========================================================================
    # Voice System Prompt (VOICE-005)
    # =========================================================================

    def get_voice_system_prompt(self, agent_name: str) -> Optional[str]:
        """Get the voice system prompt for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT voice_system_prompt FROM agent_ownership WHERE agent_name = ?",
                (agent_name,),
            )
            row = cursor.fetchone()
            return row["voice_system_prompt"] if row and row["voice_system_prompt"] else None

    def set_voice_system_prompt(self, agent_name: str, prompt: Optional[str]) -> bool:
        """Set the voice system prompt for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_ownership SET voice_system_prompt = ? WHERE agent_name = ?",
                (prompt or None, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0
