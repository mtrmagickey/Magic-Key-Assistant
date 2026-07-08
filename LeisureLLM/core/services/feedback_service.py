"""
FeedbackService — CRUD for structured product feedback.

Captures bugs, feature requests, UX issues with environment
snapshots for reproducibility.
"""

from __future__ import annotations

import json
import platform
import sys
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


def _build_env_snapshot() -> Dict[str, str]:
    """Capture a lightweight environment fingerprint."""
    return {
        "os": platform.platform(),
        "python": sys.version,
        "arch": platform.machine(),
    }


class FeedbackService(SqliteService):
    """Operates on the `feedback` table."""

    # ── Create ────────────────────────────────────────────────
    async def create(
        self,
        summary: str,
        *,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        context: Optional[str] = None,
        submitted_by: Optional[str] = None,
        auto_snapshot: bool = True,
    ) -> int:
        """Create a feedback entry. Returns the new row ID."""
        snapshot = json.dumps(_build_env_snapshot()) if auto_snapshot else None
        return await self._insert(
            """INSERT INTO feedback
               (summary, category, severity, context,
                environment_snapshot, submitted_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                summary.strip()[:1000],
                category,
                severity,
                context,
                snapshot,
                submitted_by,
            ),
        )

    # ── Read ──────────────────────────────────────────────────
    async def get(self, feedback_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single feedback entry by ID."""
        return await self._fetchone("SELECT * FROM feedback WHERE id = ?", (feedback_id,))

    async def list_all(
        self,
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List feedback with optional filters."""
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if category:
            conditions.append("category = ?")
            params.append(category)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        return await self._fetchall(
            f"SELECT * FROM feedback {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )

    # ── Update ────────────────────────────────────────────────
    async def update(self, feedback_id: int, **fields) -> bool:
        """Update arbitrary fields. Returns True if row was updated."""
        allowed = {"summary", "category", "severity", "context", "status", "resolution"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [feedback_id]
        return await self._execute(
            f"UPDATE feedback SET {set_clause} WHERE id = ?",
            tuple(params),
        ) > 0

    async def resolve(self, feedback_id: int, resolution: str) -> bool:
        """Mark feedback as resolved with a resolution note."""
        return await self._execute(
            """UPDATE feedback SET status = 'resolved', resolution = ?
               WHERE id = ? AND status != 'resolved'""",
            (resolution, feedback_id),
        ) > 0

    # ── Stats ─────────────────────────────────────────────────
    async def stats(self) -> Dict[str, Any]:
        """Return counts by status and category."""
        result: Dict[str, Any] = {}
        by_status = await self._fetchall("SELECT status, COUNT(*) as c FROM feedback GROUP BY status")
        result["by_status"] = {row["status"]: row["c"] for row in by_status}
        by_category = await self._fetchall(
            "SELECT category, COUNT(*) as c FROM feedback WHERE category IS NOT NULL GROUP BY category"
        )
        result["by_category"] = {row["category"]: row["c"] for row in by_category}
        return result
