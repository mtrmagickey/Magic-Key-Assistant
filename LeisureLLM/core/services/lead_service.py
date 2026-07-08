"""
LeadService — CRUD for the leads and lead_activity tables.

Manages the full pipeline lifecycle: cold → warm → hot → proposal → won/lost.
Every stage transition logs an activity record for audit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService


class LeadService(SqliteService):
    """Operates on the `leads` and `lead_activity` tables."""

    VALID_STAGES = {"cold", "warm", "hot", "proposal", "won", "lost"}

    async def create(
        self,
        name: str,
        *,
        source: Optional[str] = None,
        contact_name: Optional[str] = None,
        contact_info: Optional[str] = None,
        value_estimate: Optional[str] = None,
        notes: Optional[str] = None,
        next_action: Optional[str] = None,
        next_action_date: Optional[str] = None,
        owner_user_id: Optional[int] = None,
        owner_username: Optional[str] = None,
        created_by_username: Optional[str] = None,
    ) -> int:
        """Create a lead and log the creation activity. Returns new row ID."""
        now = datetime.utcnow().isoformat()
        async with self._transaction() as conn:
            lead_id = await self._insert(
                """INSERT INTO leads
                   (name, source, status, contact_name, contact_info, value_estimate,
                    notes, next_action, next_action_date,
                    owner_user_id, owner_username,
                    created_at, updated_at, last_activity)
                   VALUES (?, ?, 'cold', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name.strip()[:500],
                    source,
                    contact_name,
                    contact_info,
                    value_estimate,
                    notes,
                    next_action,
                    next_action_date,
                    owner_user_id,
                    owner_username,
                    now, now, now,
                ),
                conn=conn,
            )
            await self._log_activity(
                lead_id,
                "creation",
                f"Lead created from {source or 'manual'}",
                created_by_username=created_by_username or owner_username,
                conn=conn,
            )
        return lead_id

    async def get(self, lead_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single lead by ID."""
        return await self._fetchone("SELECT * FROM leads WHERE id = ?", (lead_id,))

    async def advance_stage(
        self,
        lead_id: int,
        new_stage: str,
        *,
        by_username: Optional[str] = None,
        note: Optional[str] = None,
    ) -> bool:
        """
        Move a lead to a new pipeline stage and log it.
        Returns True if the update succeeded.
        """
        if new_stage not in self.VALID_STAGES:
            raise ValueError(f"Invalid stage: {new_stage}. Must be one of {self.VALID_STAGES}")

        lead = await self.get(lead_id)
        if not lead:
            return False

        old_stage = lead["status"]
        now = datetime.utcnow().isoformat()

        async with self._transaction() as conn:
            updated = await self._execute(
                """UPDATE leads SET status = ?, updated_at = ?, last_activity = ?
                   WHERE id = ?""",
                (new_stage, now, now, lead_id),
                conn=conn,
            )
            if updated == 0:
                return False

            summary = f"Stage changed: {old_stage} → {new_stage}"
            if note:
                summary += f" — {note}"

            await self._log_activity(
                lead_id,
                "status_change",
                summary,
                created_by_username=by_username,
                conn=conn,
            )
        return True

    async def log_touchpoint(
        self,
        lead_id: int,
        activity_type: str,
        summary: str,
        *,
        by_username: Optional[str] = None,
    ) -> int:
        """Log a follow-up, outreach, or other interaction. Returns activity ID."""
        now = datetime.utcnow().isoformat()
        async with self._transaction() as conn:
            await self._execute(
                "UPDATE leads SET last_activity = ?, updated_at = ? WHERE id = ?",
                (now, now, lead_id),
                conn=conn,
            )
            return await self._log_activity(
                lead_id,
                activity_type,
                summary,
                created_by_username=by_username,
                conn=conn,
            )

    async def get_stale(self, *, days: int = 7, limit: int = 20) -> List[Dict[str, Any]]:
        """Return leads with no activity for N+ days that aren't won/lost."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        return await self._fetchall(
            """SELECT * FROM leads
               WHERE last_activity < ?
                 AND status NOT IN ('won', 'lost')
               ORDER BY last_activity ASC
               LIMIT ?""",
            (cutoff, limit),
        )

    async def list_by_stage(
        self, stage: str, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """List leads at a given pipeline stage."""
        return await self._fetchall(
            "SELECT * FROM leads WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (stage, limit),
        )

    async def get_activities(
        self, lead_id: int, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Return activity log for a lead."""
        return await self._fetchall(
            """SELECT * FROM lead_activity
               WHERE lead_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (lead_id, limit),
        )

    async def pipeline_summary(self) -> Dict[str, int]:
        """Return counts by stage."""
        rows = await self._fetchall("SELECT status, COUNT(*) as c FROM leads GROUP BY status")
        return {row["status"]: row["c"] for row in rows}

    # ── Internal ──────────────────────────────────────────────

    async def _log_activity(
        self,
        lead_id: int,
        activity_type: str,
        summary: str,
        *,
        created_by_username: Optional[str] = None,
        created_by_user_id: Optional[int] = None,
        conn: Any | None = None,
    ) -> int:
        """Insert a lead_activity row. Returns the new row ID."""
        return await self._insert(
            """INSERT INTO lead_activity
               (lead_id, activity_type, summary, created_by_username, created_by_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (lead_id, activity_type, summary, created_by_username, created_by_user_id),
            conn=conn,
        )
