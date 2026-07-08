"""
SOPService — CRUD for Standard Operating Procedures.

Versioned runbooks that document how recurring processes are
executed, with exercise tracking and drift detection.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


class SOPService(SqliteService):
    """Operates on the `sops` table."""

    # ── Create ────────────────────────────────────────────────
    async def create(
        self,
        title: str,
        *,
        body: Optional[str] = None,
        owner_username: Optional[str] = None,
        checklist: Optional[List[str]] = None,
        linked_decisions: Optional[List[int]] = None,
        linked_incidents: Optional[List[str]] = None,
        category: Optional[str] = None,
        status: str = "active",
    ) -> int:
        """Create an SOP. Returns the new row ID."""
        return await self._insert(
            """INSERT INTO sops
               (title, version, owner_username, body, checklist,
                linked_decisions, linked_incidents, category, status)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title.strip()[:500],
                owner_username,
                body,
                json.dumps(checklist) if checklist else None,
                json.dumps(linked_decisions) if linked_decisions else None,
                json.dumps(linked_incidents) if linked_incidents else None,
                category,
                status,
            ),
        )

    # ── Read ──────────────────────────────────────────────────
    async def get(self, sop_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single SOP by ID."""
        return await self._fetchone("SELECT * FROM sops WHERE id = ?", (sop_id,))

    async def list_all(
        self,
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List SOPs with optional filters."""
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
            f"SELECT * FROM sops {where} ORDER BY title ASC LIMIT ?",
            tuple(params),
        )

    async def get_stale(self, *, days: int = 90, limit: int = 20) -> List[Dict[str, Any]]:
        """SOPs not exercised or reviewed in N days."""
        return await self._fetchall(
            """SELECT * FROM sops
               WHERE status = 'active'
                 AND (
                     last_exercised IS NULL
                     OR last_exercised < datetime('now', ? || ' days')
                 )
                 AND (
                     last_reviewed IS NULL
                     OR last_reviewed < datetime('now', ? || ' days')
                 )
               ORDER BY COALESCE(last_exercised, last_reviewed, created_at) ASC
               LIMIT ?""",
            (f"-{days}", f"-{days}", limit),
        )

    # ── Update ────────────────────────────────────────────────
    async def update(self, sop_id: int, **fields) -> bool:
        """Update arbitrary fields. Returns True if row was updated."""
        allowed = {
            "title", "body", "owner_username", "checklist", "linked_decisions",
            "linked_incidents", "category", "status", "last_exercised",
            "last_reviewed",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False

        # Bump version if body changes
        bump_version = "body" in updates

        # JSON-encode list fields
        for key in ("checklist", "linked_decisions", "linked_incidents"):
            if key in updates and isinstance(updates[key], list):
                updates[key] = json.dumps(updates[key])

        updates["updated_at"] = "datetime('now')"  # will use raw SQL
        set_parts = []
        params = []
        for k, v in updates.items():
            if v == "datetime('now')":
                set_parts.append(f"{k} = datetime('now')")
            else:
                set_parts.append(f"{k} = ?")
                params.append(v)

        if bump_version:
            set_parts.append("version = version + 1")

        set_clause = ", ".join(set_parts)
        params.append(sop_id)
        return await self._execute(
            f"UPDATE sops SET {set_clause} WHERE id = ?",
            tuple(params),
        ) > 0

    async def mark_exercised(self, sop_id: int) -> bool:
        """Record that the SOP was exercised (run through)."""
        return await self._execute(
            """UPDATE sops SET last_exercised = datetime('now'),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (sop_id,),
        ) > 0

    async def mark_reviewed(self, sop_id: int) -> bool:
        """Record that the SOP was reviewed for accuracy."""
        return await self._execute(
            """UPDATE sops SET last_reviewed = datetime('now'),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (sop_id,),
        ) > 0

    # ── Stats ─────────────────────────────────────────────────
    async def stats(self) -> Dict[str, int]:
        """Return counts by status."""
        rows = await self._fetchall("SELECT status, COUNT(*) as c FROM sops GROUP BY status")
        return {row["status"]: row["c"] for row in rows}
