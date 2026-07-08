"""
MeetingService — CRUD for the meeting_notes table.

Handles creation, linking to actions/decisions, and retrieval.
Depends on migration 004_meeting_notes_and_source_links.sqlite.sql.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


class MeetingService(SqliteService):
    """Operates on the `meeting_notes` table and cross-links."""

    async def create(
        self,
        title: str,
        *,
        channel_id: Optional[int] = None,
        message_id: Optional[int] = None,
        summary: Optional[str] = None,
        raw_text: Optional[str] = None,
        attendees: Optional[str] = None,
        meeting_date: Optional[str] = None,
        recorded_by_username: Optional[str] = None,
        recorded_by_user_id: Optional[int] = None,
    ) -> int:
        """Create a meeting note. Returns new row ID."""
        now = datetime.utcnow().isoformat()
        return await self._insert(
            """INSERT INTO meeting_notes
               (summary, meeting_date, attendees, raw_text,
                created_at, created_by_user_id, created_by_username,
                discord_message_id, discord_thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (summary or title).strip()[:500],
                meeting_date,
                attendees,
                raw_text,
                now,
                recorded_by_user_id,
                recorded_by_username,
                message_id,
                channel_id,
            ),
        )

    async def get(self, meeting_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single meeting note by ID."""
        return await self._fetchone("SELECT * FROM meeting_notes WHERE id = ?", (meeting_id,))

    async def list_recent(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent meetings."""
        return await self._fetchall(
            "SELECT * FROM meeting_notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        """Search meetings by summary or transcript text."""
        pattern = f"%{query}%"
        return await self._fetchall(
            """SELECT * FROM meeting_notes
               WHERE summary LIKE ? OR raw_text LIKE ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (pattern, pattern, limit),
        )

    # ── Cross-linking ─────────────────────────────────────────

    async def link_action(self, meeting_id: int, task_id: int) -> bool:
        """Link a task to a meeting."""
        return await self._execute(
            "UPDATE tasks SET source_meeting_id = ? WHERE id = ?",
            (meeting_id, task_id),
        ) > 0

    async def link_decision(self, meeting_id: int, decision_id: int) -> bool:
        """Link a decision to a meeting."""
        return await self._execute(
            "UPDATE decisions SET source_meeting_id = ? WHERE id = ?",
            (meeting_id, decision_id),
        ) > 0

    async def get_linked_actions(self, meeting_id: int) -> List[Dict[str, Any]]:
        """Return all tasks linked to a meeting."""
        return await self._fetchall(
            "SELECT * FROM tasks WHERE source_meeting_id = ? ORDER BY created_at",
            (meeting_id,),
        )

    async def get_linked_decisions(self, meeting_id: int) -> List[Dict[str, Any]]:
        """Return all decisions linked to a meeting."""
        return await self._fetchall(
            "SELECT * FROM decisions WHERE source_meeting_id = ? ORDER BY decided_at DESC, id DESC",
            (meeting_id,),
        )

    async def add_source_link(
        self,
        meeting_id: int,
        *,
        url: Optional[str] = None,
        title: Optional[str] = None,
        source_type: str = "reference",
        added_by_username: Optional[str] = None,
    ) -> int:
        """Attach a source/reference link to a meeting. Returns new row ID."""
        now = datetime.utcnow().isoformat()
        source_id = (url or title or "").strip()
        if not source_id:
            raise ValueError("Source link requires a URL or title")
        metadata = json.dumps(
            {
                "title": title,
                "added_by_username": added_by_username,
            }
        )
        return await self._insert(
            """INSERT INTO source_links
               (record_type, record_id, source_type, source_id, created_at, metadata)
               VALUES ('meeting_note', ?, ?, ?, ?, ?)""",
            (meeting_id, source_type, source_id, now, metadata),
        )

    async def get_source_links(self, meeting_id: int) -> List[Dict[str, Any]]:
        """Return all source links for a meeting."""
        return await self._fetchall(
            """SELECT * FROM source_links
               WHERE record_type = 'meeting_note' AND record_id = ?
               ORDER BY created_at""",
            (meeting_id,),
        )
