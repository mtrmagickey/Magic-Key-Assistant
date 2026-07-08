"""
ActionService — CRUD for action items / tasks.

All database operations for the tasks table, extracted from the cogs
so they can be tested and reused without Discord.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


class ActionService(SqliteService):
    """
    Operates on the `tasks` table.

    Expects an `db` object with async `execute`, `fetchone`, `fetchall` methods
    (the LeisureLLM Database wrapper).
    """

    async def create(
        self,
        title: str,
        *,
        assigned_to_username: Optional[str] = None,
        assigned_to_user_id: Optional[int] = None,
        created_by_username: Optional[str] = None,
        created_by_user_id: Optional[int] = None,
        due_date: Optional[str] = None,
        priority: str = "medium",
        project_id: Optional[int] = None,
        tags: Optional[List[str]] = None,
        notes: Optional[str] = None,
        source_meeting_id: Optional[int] = None,
        source_decision_id: Optional[int] = None,
    ) -> int:
        """
        Create an action item. Returns the new row ID.
        """
        tags_str = json.dumps(tags) if tags else None
        return await self._insert(
            """INSERT INTO tasks
               (title, assigned_to_username, assigned_to_user_id,
                created_by_username, created_by_user_id,
                due_date, priority, project_id, tags, notes, status,
                source_meeting_id, source_decision_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'todo', ?, ?)""",
            (
                title.strip()[:500],
                assigned_to_username,
                assigned_to_user_id,
                created_by_username,
                created_by_user_id,
                due_date,
                priority,
                project_id,
                tags_str,
                notes,
                source_meeting_id,
                source_decision_id,
            ),
        )

    async def get(self, action_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single action by ID."""
        return await self._fetchone("SELECT * FROM tasks WHERE id = ?", (action_id,))

    async def list_by_status(
        self,
        status: str = "todo",
        *,
        owner_username: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List actions filtered by status and optionally by owner."""
        if owner_username:
            sql = """SELECT * FROM tasks
                     WHERE status = ? AND assigned_to_username = ?
                     ORDER BY priority DESC, due_date ASC
                     LIMIT ?"""
            params: tuple = (status, owner_username, limit)
        else:
            sql = """SELECT * FROM tasks
                     WHERE status = ?
                     ORDER BY priority DESC, due_date ASC
                     LIMIT ?"""
            params = (status, limit)

        return await self._fetchall(sql, params)

    async def mark_done(self, action_id: int) -> bool:
        """Mark an action as completed. Returns True if updated."""
        return await self._execute(
            """UPDATE tasks SET status = 'done', completed_at = datetime('now'),
               updated_at = datetime('now')
               WHERE id = ? AND status != 'done'""",
            (action_id,),
        ) > 0

    async def mark_cancelled(self, action_id: int) -> bool:
        """Cancel an action. Returns True if updated."""
        return await self._execute(
            """UPDATE tasks SET status = 'cancelled', updated_at = datetime('now')
               WHERE id = ? AND status NOT IN ('done', 'cancelled')""",
            (action_id,),
        ) > 0

    async def get_overdue(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Return actions past their due date that aren't done/cancelled."""
        return await self._fetchall(
            """SELECT * FROM tasks
               WHERE due_date < date('now')
                 AND status NOT IN ('done', 'cancelled')
               ORDER BY due_date ASC
               LIMIT ?""",
            (limit,),
        )

    async def get_stale(self, *, days: int = 14, limit: int = 20) -> List[Dict[str, Any]]:
        """Return actions untouched for N days that are still open."""
        return await self._fetchall(
            """SELECT * FROM tasks
               WHERE status IN ('todo', 'in_progress')
                 AND updated_at < datetime('now', ? || ' days')
               ORDER BY updated_at ASC
               LIMIT ?""",
            (f"-{days}", limit),
        )

    async def stats(self) -> Dict[str, int]:
        """Return counts by status."""
        rows = await self._fetchall("SELECT status, COUNT(*) as c FROM tasks GROUP BY status")
        return {row["status"]: row["c"] for row in rows}
