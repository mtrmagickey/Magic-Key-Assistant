"""Durable append-only audit trail for operational continuity mutations."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from services.audit_context import get_audit_context

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _json_loads(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _normalize_snapshot(snapshot: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if snapshot is None:
        return None
    return dict(snapshot)


def _diff_snapshots(
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, list[str]]]]:
    before_snapshot = _normalize_snapshot(before)
    after_snapshot = _normalize_snapshot(after)
    if before_snapshot is None and after_snapshot is None:
        return None, None, None
    if before_snapshot is None:
        return None, after_snapshot, {"added": sorted(after_snapshot.keys()), "removed": [], "changed": []}
    if after_snapshot is None:
        return before_snapshot, None, {"added": [], "removed": sorted(before_snapshot.keys()), "changed": []}

    keys = set(before_snapshot) | set(after_snapshot)
    before_delta: Dict[str, Any] = {}
    after_delta: Dict[str, Any] = {}
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    for key in sorted(keys):
        before_present = key in before_snapshot
        after_present = key in after_snapshot
        before_value = before_snapshot.get(key)
        after_value = after_snapshot.get(key)
        if before_present and after_present and before_value == after_value:
            continue
        if not before_present:
            added.append(key)
            after_delta[key] = after_value
            continue
        if not after_present:
            removed.append(key)
            before_delta[key] = before_value
            continue
        changed.append(key)
        before_delta[key] = before_value
        after_delta[key] = after_value

    if not added and not removed and not changed:
        return None, None, None
    return before_delta or None, after_delta or None, {"added": added, "removed": removed, "changed": changed}


class AuditService:
    """Persists append-only audit events for continuity mutations."""

    def __init__(self, db: Any):
        self.db = db

    async def record_mutation(
        self,
        *,
        entity_type: str,
        entity_id: Any,
        action: str,
        before: Optional[Mapping[str, Any]] = None,
        after: Optional[Mapping[str, Any]] = None,
        actor_id: Optional[int] = None,
        surface: Optional[str] = None,
        correlation_id: Optional[str] = None,
        source_context_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        context = get_audit_context()
        before_delta, after_delta, changed_fields = _diff_snapshots(before, after)
        resolved_actor_id = actor_id if actor_id is not None else context.get("actor_id")
        resolved_surface = surface or context.get("surface") or "system"
        resolved_correlation_id = correlation_id or context.get("correlation_id")
        event_id = f"audit_{uuid.uuid4().hex}"
        timestamp = created_at or _utc_now_iso()

        async with self.db.acquire() as conn:
            try:
                async with conn.execute(
                    """INSERT INTO operational_audit_events
                       (event_id, entity_type, entity_id, action, before_json, after_json,
                        changed_fields_json, actor_id, surface, correlation_id,
                        source_context_id, metadata_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        entity_type.strip(),
                        str(entity_id),
                        action.strip(),
                        _json_dumps(before_delta),
                        _json_dumps(after_delta),
                        _json_dumps(changed_fields),
                        resolved_actor_id,
                        resolved_surface,
                        resolved_correlation_id,
                        source_context_id,
                        _json_dumps(dict(metadata)) if metadata is not None else None,
                        timestamp,
                    ),
                ) as cur:
                    row_id = cur.lastrowid
                await conn.commit()
            except Exception as exc:
                if "no such table: operational_audit_events" not in str(exc).lower():
                    raise
                logger.debug("Operational audit table unavailable; skipping audit write")
                row_id = 0

        return {
            "id": int(row_id or 0),
            "event_id": event_id,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "action": action,
            "before": before_delta,
            "after": after_delta,
            "changed_fields": changed_fields,
            "actor_id": resolved_actor_id,
            "surface": resolved_surface,
            "correlation_id": resolved_correlation_id,
            "source_context_id": source_context_id,
            "metadata": dict(metadata) if metadata is not None else None,
            "created_at": timestamp,
        }

    async def list_events(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[Any] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(str(entity_id))
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([min(max(limit, 1), 500), max(offset, 0)])
        async with self.db.acquire() as conn:
            try:
                async with conn.execute(
                    f"""SELECT * FROM operational_audit_events
                        {where_sql}
                        ORDER BY created_at DESC, id DESC
                        LIMIT ? OFFSET ?""",
                    tuple(params),
                ) as cur:
                    rows = await cur.fetchall()
            except Exception as exc:
                if "no such table: operational_audit_events" not in str(exc).lower():
                    raise
                logger.debug("Operational audit table unavailable; returning empty history")
                rows = []
        return [self._row_to_dict(dict(row)) for row in rows]

    async def get_entity_history(self, *, entity_type: str, entity_id: Any, limit: int = 200) -> list[Dict[str, Any]]:
        return await self.list_events(entity_type=entity_type, entity_id=entity_id, limit=limit, offset=0)

    def _row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["before"] = _json_loads(row.pop("before_json", None))
        row["after"] = _json_loads(row.pop("after_json", None))
        row["changed_fields"] = _json_loads(row.pop("changed_fields_json", None))
        row["metadata"] = _json_loads(row.pop("metadata_json", None))
        return row