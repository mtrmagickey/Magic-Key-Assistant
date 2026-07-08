"""
RailsService — Venture lifecycle state machine.

Rails represent lifecycle tracks (Validate, Launch, Operate).
Each rail contains ordered stages with required & actual outputs.
Advancement is gated: you must produce the required artifacts
before the state machine lets you move forward.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ._sqlite_service import SqliteService

logger = logging.getLogger(__name__)

# ── Default stage templates per rail type ─────────────────────
_DEFAULT_STAGES: Dict[str, List[Dict[str, Any]]] = {
    "validate": [
        {"name": "Problem Definition", "required_outputs": ["Problem statement doc", "Customer segment definition"], "escalation_days": 7},
        {"name": "Customer Discovery", "required_outputs": ["5+ customer interviews", "Pain point ranking"], "escalation_days": 14},
        {"name": "Solution Hypothesis", "required_outputs": ["Solution sketch", "Key assumptions list"], "escalation_days": 7},
        {"name": "MVP Scoping", "required_outputs": ["MVP feature list", "Build-vs-buy decision"], "escalation_days": 7},
        {"name": "Validation Gate", "required_outputs": ["Go/No-go decision [Decision#]", "Evidence summary"], "escalation_days": 3},
    ],
    "launch": [
        {"name": "Build Sprint", "required_outputs": ["Working MVP", "Test results"], "escalation_days": 21},
        {"name": "Beta Test", "required_outputs": ["Beta user feedback", "Bug triage"], "escalation_days": 14},
        {"name": "Go-to-Market Prep", "required_outputs": ["Pricing decision", "Channel plan"], "escalation_days": 7},
        {"name": "Launch Gate", "required_outputs": ["Launch checklist complete", "Day-1 SOP created"], "escalation_days": 3},
    ],
    "operate": [
        {"name": "Steady State Setup", "required_outputs": ["KPI dashboard", "Obligation schedule"], "escalation_days": 14},
        {"name": "First Review Cycle", "required_outputs": ["30-day review", "Adjustment decisions"], "escalation_days": 30},
        {"name": "Scaling Decision", "required_outputs": ["Scale/Pivot/Kill decision", "Resource plan"], "escalation_days": 14},
    ],
}


class RailsService(SqliteService):
    """Operates on the `rails` and `rail_stages` tables."""

    @staticmethod
    def _json_or_none(value: Optional[List[str]]) -> Optional[str]:
        """Serialize list-like stage payloads exactly once at the service edge."""
        return json.dumps(value) if value else None

    async def _get_rail_row(self, rail_id: int, *, conn: Any | None = None) -> Optional[Dict[str, Any]]:
        return await self._fetchone("SELECT * FROM rails WHERE id = ?", (rail_id,), conn=conn)

    async def _set_first_stage_as_current(self, rail_id: int, *, conn: Any) -> Optional[int]:
        """Point the rail at its first stage after the stage rows exist."""
        first_stage = await self._fetchone(
            "SELECT id FROM rail_stages WHERE rail_id = ? ORDER BY position ASC LIMIT 1",
            (rail_id,),
            conn=conn,
        )
        if not first_stage:
            return None

        await self._execute(
            "UPDATE rails SET current_stage_id = ?, updated_at = datetime('now') WHERE id = ?",
            (first_stage["id"], rail_id),
            conn=conn,
        )
        return int(first_stage["id"])

    # ── Rail map helpers ──────────────────────────────────────
    @staticmethod
    def _load_rail_map(map_name: str) -> Optional[Dict[str, Any]]:
        """Load a rail map definition from config/rail_maps.yaml."""
        from pathlib import Path

        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — rail maps unavailable")
            return None

        config_dir = Path(__file__).resolve().parent.parent.parent / "config"
        maps_path = config_dir / "rail_maps.yaml"
        if not maps_path.exists():
            return None

        with open(maps_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return (data.get("maps") or {}).get(map_name)

    async def create_from_map(
        self,
        map_name: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> int:
        """Create a rail populated with stages from a rail_maps.yaml map.

        Returns the rail ID.
        """
        map_def = self._load_rail_map(map_name)
        if not map_def:
            raise ValueError(f"Rail map {map_name!r} not found in rail_maps.yaml")

        rail_name = name or map_def.get("label", map_name.title())
        rail_desc = description or map_def.get("description", "")

        # Map "launch"/"stabilize" → a rail_type the DB accepts.
        # We treat the map name as the rail_type since it's descriptive.
        # But DB currently constrains to validate/launch/operate. We'll use
        # the closest match, or add the map name as a new type.
        type_map = {"launch": "launch", "stabilize": "operate"}
        rail_type = type_map.get(map_name, "operate")

        # Keep rail creation atomic so we never leave behind a half-built state machine.
        stages = map_def.get("stages", [])
        async with self._transaction() as conn:
            rail_id = await self._insert(
                """INSERT INTO rails (name, rail_type, description)
                   VALUES (?, ?, ?)""",
                (rail_name.strip()[:200], rail_type, rail_desc),
                conn=conn,
            )

            # Stage rows are created in order because downstream logic relies on that ordering.
            for pos, stage_def in enumerate(stages, start=1):
                await self.add_stage(
                    rail_id,
                    name=stage_def["name"],
                    position=pos,
                    description=stage_def.get("description", ""),
                    required_outputs=stage_def.get("required_outputs", []),
                    escalation_days=stage_def.get("escalation_days", 7),
                    conn=conn,
                )

            if stages:
                await self._set_first_stage_as_current(rail_id, conn=conn)

        logger.info("Created rail #%d from map %r with %d stages", rail_id, map_name, len(stages))
        return rail_id

    # ── Rail CRUD ─────────────────────────────────────────────
    async def create_rail(
        self,
        name: str,
        rail_type: str,
        *,
        description: Optional[str] = None,
        use_default_stages: bool = True,
    ) -> int:
        """
        Create a new rail (and optionally populate with default stages).
        Returns the rail ID.
        """
        if rail_type not in ("validate", "launch", "operate"):
            raise ValueError(f"Invalid rail_type: {rail_type!r}")

        default_stages = _DEFAULT_STAGES.get(rail_type, []) if use_default_stages else []
        async with self._transaction() as conn:
            rail_id = await self._insert(
                """INSERT INTO rails (name, rail_type, description)
                   VALUES (?, ?, ?)""",
                (name.strip()[:200], rail_type, description),
                conn=conn,
            )

            for pos, stage_def in enumerate(default_stages, start=1):
                await self.add_stage(
                    rail_id,
                    name=stage_def["name"],
                    position=pos,
                    required_outputs=stage_def["required_outputs"],
                    escalation_days=stage_def.get("escalation_days", 7),
                    conn=conn,
                )

            if default_stages:
                await self._set_first_stage_as_current(rail_id, conn=conn)

        return rail_id

    async def get_rail(self, rail_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a rail with its stages."""
        rail = await self._get_rail_row(rail_id)
        if not rail:
            return None
        rail["stages"] = await self.list_stages(rail_id)
        return rail

    async def list_rails(
        self,
        *,
        rail_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List rails with optional filters."""
        conditions = []
        params: list = []
        if rail_type:
            conditions.append("rail_type = ?")
            params.append(rail_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        return await self._fetchall(
            f"SELECT * FROM rails {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )

    async def update_rail(self, rail_id: int, *, conn: Any | None = None, **fields) -> bool:
        """Update rail metadata."""
        allowed = {"name", "description", "status", "current_stage_id"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False

        updates["updated_at"] = None  # sentinel for datetime('now')
        set_parts = []
        params = []
        for k, v in updates.items():
            if k == "updated_at":
                set_parts.append("updated_at = datetime('now')")
            else:
                set_parts.append(f"{k} = ?")
                params.append(v)
        params.append(rail_id)
        return await self._execute(
            f"UPDATE rails SET {', '.join(set_parts)} WHERE id = ?",
            tuple(params),
            conn=conn,
        ) > 0

    # ── Stage CRUD ────────────────────────────────────────────
    async def add_stage(
        self,
        rail_id: int,
        *,
        name: str,
        position: int,
        description: Optional[str] = None,
        required_outputs: Optional[List[str]] = None,
        escalation_days: int = 7,
        conn: Any | None = None,
    ) -> int:
        """Add a stage to a rail. Returns the stage ID."""
        return await self._insert(
            """INSERT INTO rail_stages
               (rail_id, name, position, description, required_outputs, escalation_days)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                rail_id,
                name.strip()[:200],
                position,
                description,
                self._json_or_none(required_outputs),
                escalation_days,
            ),
            conn=conn,
        )

    async def list_stages(self, rail_id: int) -> List[Dict[str, Any]]:
        """List all stages for a rail, ordered by position."""
        return await self._fetchall(
            "SELECT * FROM rail_stages WHERE rail_id = ? ORDER BY position ASC",
            (rail_id,),
        )

    async def update_stage(self, stage_id: int, *, conn: Any | None = None, **fields) -> bool:
        """Update stage fields."""
        allowed = {
            "name", "description", "required_outputs", "actual_outputs",
            "status", "notes", "escalation_days",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False

        for key in ("required_outputs", "actual_outputs"):
            if key in updates and isinstance(updates[key], list):
                updates[key] = self._json_or_none(updates[key])

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [stage_id]
        return await self._execute(
            f"UPDATE rail_stages SET {set_clause} WHERE id = ?",
            tuple(params),
            conn=conn,
        ) > 0

    # ── State machine ─────────────────────────────────────────
    async def advance_stage(self, rail_id: int) -> Dict[str, Any]:
        """
        Attempt to advance to the next stage.
        Returns a dict with 'success', 'message', and optionally 'new_stage'.

        Rules:
        - Current stage must have status 'complete' or 'skipped'
        - If there is a next stage, set it as current and mark it 'in_progress'
        - If no next stage, mark the rail as 'completed'
        """
        rail = await self.get_rail(rail_id)
        if not rail:
            return {"success": False, "message": "Rail not found"}

        current_stage_id = rail.get("current_stage_id")
        stages = rail.get("stages", [])
        if not stages:
            return {"success": False, "message": "Rail has no stages"}

        # Advancement is position-based, so we resolve the current stage against the ordered stage list.
        current_idx = next((i for i, stage in enumerate(stages) if stage["id"] == current_stage_id), None)
        if current_idx is None:
            return {"success": False, "message": "Current stage not found"}

        current_stage = stages[current_idx]
        if current_stage["status"] not in ("complete", "skipped"):
            return {
                "success": False,
                "message": (
                    f"Current stage '{current_stage['name']}' is '{current_stage['status']}' "
                    "— must be complete or skipped to advance"
                ),
            }

        next_idx = current_idx + 1
        if next_idx >= len(stages):
            await self.update_rail(rail_id, status="completed")
            return {"success": True, "message": "Rail completed — all stages done"}

        next_stage = stages[next_idx]
        # The current-stage pointer and the next-stage status change together or not at all.
        async with self._transaction() as conn:
            await self.update_stage(next_stage["id"], status="in_progress", conn=conn)
            await self._execute(
                "UPDATE rail_stages SET entered_at = datetime('now') WHERE id = ?",
                (next_stage["id"],),
                conn=conn,
            )
            await self.update_rail(rail_id, current_stage_id=next_stage["id"], conn=conn)

        return {
            "success": True,
            "message": f"Advanced to stage: {next_stage['name']}",
            "new_stage": next_stage["name"],
        }

    async def complete_stage(
        self,
        stage_id: int,
        *,
        actual_outputs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Mark a stage as complete and record actual outputs."""
        updates: Dict[str, Any] = {"status": "complete"}
        if actual_outputs:
            updates["actual_outputs"] = actual_outputs

        # Completion timestamp and output capture belong to the same state transition.
        async with self._transaction() as conn:
            await self.update_stage(stage_id, conn=conn, **updates)
            await self._execute(
                "UPDATE rail_stages SET completed_at = datetime('now') WHERE id = ?",
                (stage_id,),
                conn=conn,
            )
        return {"success": True, "message": "Stage completed"}

    async def get_escalation_candidates(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Find stages that are overdue (in_progress longer than escalation_days)."""
        return await self._fetchall(
            """SELECT rs.*, r.name as rail_name, r.rail_type
               FROM rail_stages rs
               JOIN rails r ON rs.rail_id = r.id
               WHERE rs.status = 'in_progress'
                 AND rs.entered_at IS NOT NULL
                 AND julianday('now') - julianday(rs.entered_at) > rs.escalation_days
               ORDER BY (julianday('now') - julianday(rs.entered_at)) DESC
               LIMIT ?""",
            (limit,),
        )

    # ── Stats ─────────────────────────────────────────────────
    async def stats(self) -> Dict[str, Any]:
        """Rail and stage counts."""
        result: Dict[str, Any] = {}
        rails_by_type = await self._fetchall("SELECT rail_type, COUNT(*) as c FROM rails GROUP BY rail_type")
        result["rails_by_type"] = {row["rail_type"]: row["c"] for row in rails_by_type}
        stages_by_status = await self._fetchall("SELECT status, COUNT(*) as c FROM rail_stages GROUP BY status")
        result["stages_by_status"] = {row["status"]: row["c"] for row in stages_by_status}
        return result
