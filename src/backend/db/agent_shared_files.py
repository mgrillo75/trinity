"""
agent_shared_files database operations (amazing-file-outbound, FILES-001).

Thin CRUD over the table. No business logic here — that belongs in
services/agent_shared_files_service.py.

MVP scope (Step 3):
- create        — insert a new shared-file row
- get_by_token  — token-based lookup (Step 4 download endpoint will use this)
- total_bytes_for_agent — quota enforcement helper

Methods needed by later steps (list_by_agent, revoke, mark_downloaded,
delete_expired) are added later.

One-time link semantics are deferred (see amazing-file-outbound.md §9).
The `one_time` / `consumed_at` columns exist in the schema but are not
written or read by this layer — kept so the feature can be re-enabled
without another migration.
"""

from typing import Optional, Dict, Any

from .connection import get_db_connection


class AgentSharedFilesOperations:
    """CRUD for agent_shared_files."""

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    def create(
        self,
        *,
        file_id: str,
        agent_name: str,
        filename: str,
        stored_filename: str,
        size_bytes: int,
        mime_type: Optional[str],
        download_token: str,
        created_by: str,
        created_at: str,
        expires_at: str,
    ) -> str:
        """Insert a new shared-file row. Returns the file_id."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO agent_shared_files (
                    id, agent_name, filename, stored_filename, size_bytes,
                    mime_type, download_token, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    agent_name,
                    filename,
                    stored_filename,
                    size_bytes,
                    mime_type,
                    download_token,
                    created_by,
                    created_at,
                    expires_at,
                ),
            )
            conn.commit()
            return file_id

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    def get_by_id(self, file_id: str) -> Optional[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM agent_shared_files WHERE id = ?", (file_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_by_token(self, download_token: str) -> Optional[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM agent_shared_files WHERE download_token = ?",
                (download_token,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def mark_downloaded(self, file_id: str) -> None:
        """Increment download_count + bump last_downloaded_at."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_shared_files
                   SET download_count = download_count + 1,
                       last_downloaded_at = datetime('now')
                 WHERE id = ?
                """,
                (file_id,),
            )
            conn.commit()

    def revoke(self, file_id: str) -> bool:
        """
        Mark a share as revoked. Idempotent — a second revoke is a no-op.
        Returns True if a row transitioned from active to revoked.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_shared_files
                   SET revoked_at = datetime('now')
                 WHERE id = ? AND revoked_at IS NULL
                """,
                (file_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_expired_and_revoked(self, revoke_grace_hours: int = 24) -> list:
        """
        Delete rows where the share is:
          - expired (expires_at < now) — any state, OR
          - revoked more than `revoke_grace_hours` ago

        Returns a list of `stored_filename` values whose rows were
        deleted, so the caller can unlink the on-disk files under
        /data/agent-files/. Called by the cleanup service (C4 / Step 7).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, stored_filename
                  FROM agent_shared_files
                 WHERE datetime(expires_at) < datetime('now')
                    OR (revoked_at IS NOT NULL
                        AND datetime(revoked_at) < datetime('now', ?))
                """,
                (f"-{revoke_grace_hours} hours",),
            )
            rows = cursor.fetchall()
            if not rows:
                return []
            stored = [row["stored_filename"] for row in rows]
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" * len(ids))
            cursor.execute(
                f"DELETE FROM agent_shared_files WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
            return stored

    def delete_for_agent(self, agent_name: str) -> list:
        """
        Delete every shared-file row for an agent and return the
        `stored_filename` values so the caller can unlink the files
        on disk.

        Used by the agent-delete handler. We can't rely on the FK
        `ON DELETE CASCADE` because the platform's SQLite connections
        don't `PRAGMA foreign_keys = ON`; deletion of child rows is
        done explicitly everywhere else in the codebase too
        (`rename_agent` in `db/agent_settings/metadata.py` follows the
        same manual-cascade pattern).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT stored_filename FROM agent_shared_files WHERE agent_name = ?",
                (agent_name,),
            )
            stored = [row["stored_filename"] for row in cursor.fetchall()]
            cursor.execute(
                "DELETE FROM agent_shared_files WHERE agent_name = ?",
                (agent_name,),
            )
            conn.commit()
            return stored

    def list_active_for_agent(self, agent_name: str) -> list:
        """
        Active (non-revoked, non-expired) shares for an agent,
        newest first. Used by the Sharing panel.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, agent_name, filename, stored_filename, size_bytes,
                       mime_type, download_token, created_by, created_at,
                       expires_at, revoked_at, download_count, last_downloaded_at
                  FROM agent_shared_files
                 WHERE agent_name = ?
                   AND revoked_at IS NULL
                   AND datetime(expires_at) > datetime('now')
                 ORDER BY created_at DESC
                """,
                (agent_name,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def total_bytes_for_agent(self, agent_name: str) -> int:
        """
        Sum of size_bytes for rows that still count toward the quota:
        not revoked and not expired.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) AS total
                FROM agent_shared_files
                WHERE agent_name = ?
                  AND revoked_at IS NULL
                  AND datetime(expires_at) > datetime('now')
                """,
                (agent_name,),
            )
            row = cursor.fetchone()
            return int(row["total"]) if row else 0
