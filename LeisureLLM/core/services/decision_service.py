"""
DecisionService — CRUD for the decisions table.

Decisions are the core recall artifact. Every decision stores
who, what, why, when, and linked evidence so it can be surfaced
by RAG or keyword search later.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


class DecisionService(SqliteService):
    """Operates on the `decisions` table."""

    async def create(
        self,
        title: str,
        decision: str,
        *,
        rationale: Optional[str] = None,
        decided_by: Optional[List[str]] = None,
        category: Optional[str] = None,
        impact: Optional[str] = None,
        related_project_id: Optional[int] = None,
        source_meeting_id: Optional[int] = None,
        tags: Optional[List[str]] = None,
    ) -> int:
        """Create a decision record. Returns the new row ID."""
        return await self._insert(
            """INSERT INTO decisions
               (title, decision, rationale, decided_by, category, impact,
                related_project_id, source_meeting_id, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title.strip()[:500],
                decision.strip(),
                rationale,
                json.dumps(decided_by) if decided_by else None,
                category,
                impact,
                related_project_id,
                source_meeting_id,
                json.dumps(tags) if tags else None,
            ),
        )

    async def get(self, decision_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single decision by ID."""
        return await self._fetchone("SELECT * FROM decisions WHERE id = ?", (decision_id,))

    async def search(
        self,
        keyword: str,
        *,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Keyword search across decision text, title, and rationale.
        Simulates what RAG retrieval does at the embedding level.
        """
        like = f"%{keyword}%"
        if category:
            sql = """SELECT * FROM decisions
                     WHERE (title LIKE ? OR decision LIKE ? OR rationale LIKE ?)
                       AND category = ?
                     ORDER BY decided_at DESC
                     LIMIT ?"""
            params: tuple = (like, like, like, category, limit)
        else:
            sql = """SELECT * FROM decisions
                     WHERE title LIKE ? OR decision LIKE ? OR rationale LIKE ?
                     ORDER BY decided_at DESC
                     LIMIT ?"""
            params = (like, like, like, limit)

        return await self._fetchall(sql, params)

    async def list_recent(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the N most recent decisions."""
        return await self._fetchall(
            "SELECT * FROM decisions ORDER BY decided_at DESC LIMIT ?",
            (limit,),
        )

    async def for_meeting(self, meeting_id: int) -> List[Dict[str, Any]]:
        """Return all decisions linked to a meeting."""
        return await self._fetchall(
            "SELECT * FROM decisions WHERE source_meeting_id = ? ORDER BY id",
            (meeting_id,),
        )

    async def for_project(self, project_id: int) -> List[Dict[str, Any]]:
        """Return all decisions linked to a project."""
        return await self._fetchall(
            "SELECT * FROM decisions WHERE related_project_id = ? ORDER BY decided_at DESC",
            (project_id,),
        )
