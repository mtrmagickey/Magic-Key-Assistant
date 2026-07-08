"""
ObligationService — CRUD for recurring obligations.

Renewals, filings, inspections, payroll, maintenance — anything
the org must do on a schedule to stay compliant and operational.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


class ObligationService(SqliteService):
    """Operates on the `obligations` table."""

    # ── Create ────────────────────────────────────────────────
    async def create(
        self,
        title: str,
        *,
        description: Optional[str] = None,
        frequency: str = "monthly",
        owner_username: Optional[str] = None,
        next_due: Optional[str] = None,
        checklist: Optional[List[str]] = None,
        evidence_links: Optional[List[str]] = None,
        category: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Create an obligation. Returns the new row ID."""
        return await self._insert(
            """INSERT INTO obligations
               (title, description, frequency, owner_username, next_due,
                checklist, evidence_links, category, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title.strip()[:500],
                description,
                frequency,
                owner_username,
                next_due,
                json.dumps(checklist) if checklist else None,
                json.dumps(evidence_links) if evidence_links else None,
                category,
                notes,
            ),
        )

    # ── Read ──────────────────────────────────────────────────
    async def get(self, obligation_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single obligation by ID."""
        return await self._fetchone("SELECT * FROM obligations WHERE id = ?", (obligation_id,))

    async def list_all(
        self,
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List obligations with optional filters."""
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
            f"SELECT * FROM obligations {where} ORDER BY next_due ASC LIMIT ?",
            tuple(params),
        )

    async def get_upcoming(self, *, days: int = 14, limit: int = 20) -> List[Dict[str, Any]]:
        """Return obligations due within N days."""
        return await self._fetchall(
            """SELECT * FROM obligations
               WHERE next_due <= date('now', ? || ' days')
                 AND status IN ('active', 'upcoming')
               ORDER BY next_due ASC
               LIMIT ?""",
            (f"+{days}", limit),
        )

    async def get_overdue(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Return obligations past their due date."""
        return await self._fetchall(
            """SELECT * FROM obligations
               WHERE next_due < date('now')
                 AND status IN ('active', 'upcoming')
               ORDER BY next_due ASC
               LIMIT ?""",
            (limit,),
        )

    # ── Update ────────────────────────────────────────────────
    async def update(self, obligation_id: int, **fields) -> bool:
        """Update arbitrary fields. Returns True if row was updated."""
        allowed = {
            "title", "description", "frequency", "owner_username", "next_due",
            "status", "checklist", "evidence_links", "category", "notes",
            "last_completed",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        # JSON-encode list fields
        for key in ("checklist", "evidence_links"):
            if key in updates and isinstance(updates[key], list):
                updates[key] = json.dumps(updates[key])

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [obligation_id]
        return await self._execute(
            f"UPDATE obligations SET {set_clause} WHERE id = ?",
            tuple(params),
        ) > 0

    async def mark_completed(self, obligation_id: int) -> bool:
        """Mark an obligation as completed and record the timestamp."""
        return await self._execute(
            """UPDATE obligations
               SET status = 'completed', last_completed = datetime('now')
               WHERE id = ? AND status != 'completed'""",
            (obligation_id,),
        ) > 0

    async def mark_overdue(self, obligation_id: int) -> bool:
        """Mark an obligation as overdue."""
        return await self._execute(
            """UPDATE obligations SET status = 'overdue'
               WHERE id = ? AND status IN ('active', 'upcoming')""",
            (obligation_id,),
        ) > 0

    # ── Stats ─────────────────────────────────────────────────
    async def stats(self) -> Dict[str, int]:
        """Return counts by status."""
        rows = await self._fetchall("SELECT status, COUNT(*) as c FROM obligations GROUP BY status")
        return {row["status"]: row["c"] for row in rows}
