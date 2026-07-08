"""Service layer for canonical operational records.

This service owns the additive continuity schema introduced by migration 018.
It deliberately does not rewrite the legacy authority tables yet.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from core.operational_records import (
    OperationalRecordInput,
    OperationalRecordValidationError,
    is_archived_state,
    is_resolved_state,
    normalize_record_type,
    validate_operational_record_input,
    validate_transition,
)
from core.services.audit_service import AuditService


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(dict(value or {}), sort_keys=True)


_UNSET = object()


class OperationalRecordService:
    """CRUD and transition helpers for canonical operational records."""

    def __init__(self, db: Any):
        self.db = db
        self.audit = AuditService(db)

    def _record_audit_snapshot(self, record: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if not record:
            return None
        payload = record.get("canonical_payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = None
        return {
            "record_type": record.get("record_type"),
            "title": record.get("title"),
            "summary": record.get("summary"),
            "state": record.get("state"),
            "owner_id": record.get("owner_id"),
            "due_at": record.get("due_at"),
            "stale_after_at": record.get("stale_after_at"),
            "review_at": record.get("review_at"),
            "rationale": record.get("rationale"),
            "notes": record.get("notes"),
            "source_context_id": record.get("source_context_id"),
            "resolved_at": record.get("resolved_at"),
            "archived_at": record.get("archived_at"),
            "canonical_payload": payload,
        }

    def _update_action_from_change_set(self, changed_fields: set[str]) -> str:
        if changed_fields == {"owner_id"}:
            return "ownership_changed"
        if changed_fields == {"due_at"}:
            return "due_date_changed"
        return "record_updated"

    async def ensure_actor(
        self,
        *,
        actor_kind: str,
        external_ref: str,
        display_name: Optional[str] = None,
        stable_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        actor_kind = (actor_kind or "").strip()
        external_ref = (external_ref or "").strip()
        if not actor_kind or not external_ref:
            raise OperationalRecordValidationError("actor_kind and external_ref are required")

        async with self.db.acquire() as conn:
            async with conn.execute(
                "SELECT * FROM operational_actors WHERE actor_kind = ? AND external_ref = ?",
                (actor_kind, external_ref),
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                if display_name and display_name != existing["display_name"]:
                    await conn.execute(
                        "UPDATE operational_actors SET display_name = ?, updated_at = ? WHERE id = ?",
                        (display_name, _utc_now_iso(), existing["id"]),
                    )
                    await conn.commit()
                    async with conn.execute("SELECT * FROM operational_actors WHERE id = ?", (existing["id"],)) as cur:
                        existing = await cur.fetchone()
                return dict(existing)

            actor_stable_id = stable_id or f"actor_{uuid.uuid4().hex}"
            now = _utc_now_iso()
            async with conn.execute(
                """INSERT INTO operational_actors
                   (stable_id, actor_kind, external_ref, display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (actor_stable_id, actor_kind, external_ref, display_name, now, now),
            ) as cur:
                actor_id = cur.lastrowid
            await conn.commit()
            async with conn.execute("SELECT * FROM operational_actors WHERE id = ?", (actor_id,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else {}

    async def get_actor(self, actor_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM operational_actors WHERE id = ?", (actor_id,))
        return dict(row) if row else None

    async def create_record(
        self,
        *,
        record_type: str,
        title: str,
        summary: Optional[str] = None,
        state: Optional[str] = None,
        owner_id: Optional[int] = None,
        created_by_actor_id: int,
        updated_by_actor_id: Optional[int] = None,
        source_context_id: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        project_scope: Optional[str] = None,
        due_at: Optional[str] = None,
        stale_after_at: Optional[str] = None,
        review_at: Optional[str] = None,
        rationale: Optional[str] = None,
        notes: Optional[str] = None,
        deliverables: Optional[str] = None,
        canonical_payload: Optional[Mapping[str, Any]] = None,
        stable_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        record_input = validate_operational_record_input(
            OperationalRecordInput(
                record_type=record_type,
                title=title,
                summary=summary,
                state=state,
                owner_id=owner_id,
                created_by_actor_id=created_by_actor_id,
                updated_by_actor_id=updated_by_actor_id,
                source_context_id=source_context_id,
                workspace_scope=workspace_scope,
                project_scope=project_scope,
                due_at=due_at,
                stale_after_at=stale_after_at,
                review_at=review_at,
                rationale=rationale,
                notes=notes,
                deliverables=deliverables,
                canonical_payload=canonical_payload,
            )
        )

        record_stable_id = stable_id or f"oprec_{uuid.uuid4().hex}"
        now = _utc_now_iso()
        resolved_at = now if is_resolved_state(record_input.record_type, record_input.state) else None
        archived_at = now if is_archived_state(record_input.record_type, record_input.state) else None

        async with self.db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO operational_records
                   (stable_id, record_type, title, summary, state, owner_id,
                    created_by_actor_id, updated_by_actor_id, source_context_id,
                    workspace_scope, project_scope, due_at, stale_after_at,
                    review_at, rationale, notes, deliverables, canonical_payload_json,
                    created_at, updated_at, resolved_at, archived_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_stable_id,
                    record_input.record_type,
                    record_input.title,
                    record_input.summary,
                    record_input.state,
                    record_input.owner_id,
                    record_input.created_by_actor_id,
                    record_input.updated_by_actor_id,
                    record_input.source_context_id,
                    record_input.workspace_scope,
                    record_input.project_scope,
                    record_input.due_at,
                    record_input.stale_after_at,
                    record_input.review_at,
                    record_input.rationale,
                    record_input.notes,
                    record_input.deliverables,
                    _json_dumps(record_input.canonical_payload),
                    now,
                    now,
                    resolved_at,
                    archived_at,
                ),
            ) as cur:
                record_id = cur.lastrowid
            await conn.commit()

        created = await self.get_record(int(record_id))
        await self.audit.record_mutation(
            entity_type="operational_record",
            entity_id=record_id,
            action="record_created",
            before=None,
            after=self._record_audit_snapshot(created),
            actor_id=record_input.updated_by_actor_id or record_input.created_by_actor_id,
            source_context_id=record_input.source_context_id,
            metadata={"record_type": record_input.record_type},
        )
        await self._append_event(
            record=created,
            event_type="created",
            actor_id=record_input.updated_by_actor_id or record_input.created_by_actor_id,
            previous_state=None,
            new_state=record_input.state,
            source_context_id=record_input.source_context_id,
            summary=f"Created {record_input.record_type} record.",
            payload=record_input.canonical_payload,
        )
        return await self.get_record(int(record_id)) or {}

    async def get_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM operational_records WHERE id = ?", (record_id,))
        return dict(row) if row else None

    async def list_records(
        self,
        *,
        record_type: Optional[str] = None,
        state: Optional[str] = None,
        owner_id: Optional[int] = None,
        source_context_id: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 25,
    ) -> list[Dict[str, Any]]:
        """List operational records with optional filters."""
        where, params = [], []
        if record_type:
            where.append("record_type = ?")
            params.append(record_type)
        if state:
            where.append("state = ?")
            params.append(state)
        if owner_id is not None:
            where.append("owner_id = ?")
            params.append(owner_id)
        if source_context_id:
            where.append("source_context_id = ?")
            params.append(source_context_id)
        if not include_archived:
            where.append("archived_at IS NULL")
        w = f"WHERE {' AND '.join(where)}" if where else ""
        limit = min(limit, 100)
        params.append(limit)
        async with self.db.acquire() as conn, conn.execute(
            f"""SELECT id, stable_id, record_type, title, summary, state,
                           owner_id, source_context_id, due_at, created_at,
                           updated_at, resolved_at, deliverables
                    FROM operational_records {w}
                    ORDER BY updated_at DESC LIMIT ?""",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_events(self, record_id: int) -> list[Dict[str, Any]]:
        async with self.db.acquire() as conn, conn.execute(
            "SELECT * FROM operational_record_events WHERE record_id = ? ORDER BY id ASC",
            (record_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def link_legacy_record(self, *, record_id: int, legacy_table: str, legacy_id: int) -> None:
        record = await self.get_record(record_id)
        await self.db.execute(
            """
            INSERT OR IGNORE INTO operational_record_legacy_links
            (record_id, legacy_table, legacy_id, linked_at)
            VALUES (?, ?, ?, ?)
            """,
            (record_id, legacy_table.strip(), int(legacy_id), _utc_now_iso()),
            )
        await self.audit.record_mutation(
            entity_type="operational_record",
            entity_id=record_id,
            action="traceability_link_added",
            before=None,
            after={"legacy_table": legacy_table.strip(), "legacy_id": int(legacy_id)},
            actor_id=record.get("updated_by_actor_id") if record else None,
            source_context_id=record.get("source_context_id") if record else None,
            metadata={"record_type": record.get("record_type") if record else None},
        )

    async def get_record_by_legacy_link(self, *, legacy_table: str, legacy_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("""
SELECT r.*
FROM operational_record_legacy_links l
JOIN operational_records r ON r.id = l.record_id
WHERE l.legacy_table = ? AND l.legacy_id = ?
""",
(legacy_table.strip(), int(legacy_id)),)
        return dict(row) if row else None

    async def update_record(
        self,
        *,
        record_id: int,
        actor_id: int,
        title: Any = _UNSET,
        summary: Any = _UNSET,
        owner_id: Any = _UNSET,
        due_at: Any = _UNSET,
        stale_after_at: Any = _UNSET,
        review_at: Any = _UNSET,
        rationale: Any = _UNSET,
        notes: Any = _UNSET,
        deliverables: Any = _UNSET,
        canonical_payload: Any = _UNSET,
        source_context_id: Optional[str] = None,
        event_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        record = await self.get_record(record_id)
        if not record:
            raise OperationalRecordValidationError(f"Unknown operational record: {record_id}")

        updates = []
        params = []
        for field_name, field_value in (
            ("title", title),
            ("summary", summary),
            ("owner_id", owner_id),
            ("due_at", due_at),
            ("stale_after_at", stale_after_at),
            ("review_at", review_at),
            ("rationale", rationale),
            ("notes", notes),
            ("deliverables", deliverables),
        ):
            if field_value is not _UNSET:
                updates.append(f"{field_name} = ?")
                params.append(field_value)

        if canonical_payload is not _UNSET:
            updates.append("canonical_payload_json = ?")
            params.append(_json_dumps(canonical_payload))

        if source_context_id is not None:
            updates.append("source_context_id = ?")
            params.append(source_context_id)

        if not updates:
            return record

        before_snapshot = self._record_audit_snapshot(record)

        now = _utc_now_iso()
        updates.extend(["updated_by_actor_id = ?", "updated_at = ?"])
        params.extend([actor_id, now, record_id])

        await self.db.execute(
            f"UPDATE operational_records SET {', '.join(updates)} WHERE id = ?",
            params,
            )
        updated = await self.get_record(record_id)
        after_snapshot = self._record_audit_snapshot(updated)
        changed_fields = {
            field_name
            for field_name in set((before_snapshot or {}).keys()) | set((after_snapshot or {}).keys())
            if (before_snapshot or {}).get(field_name) != (after_snapshot or {}).get(field_name)
        }
        await self.audit.record_mutation(
            entity_type="operational_record",
            entity_id=record_id,
            action=self._update_action_from_change_set(changed_fields),
            before=before_snapshot,
            after=after_snapshot,
            actor_id=actor_id,
            source_context_id=source_context_id or (updated or record).get("source_context_id"),
            metadata={"record_type": (updated or record).get("record_type")},
        )
        await self._append_event(
            record=updated,
            event_type="updated",
            actor_id=actor_id,
            previous_state=record.get("state"),
            new_state=updated.get("state") if updated else record.get("state"),
            source_context_id=source_context_id,
            summary=event_summary or "Updated operational record metadata.",
            payload=canonical_payload if canonical_payload is not _UNSET else None,
        )
        return updated or record

    async def transition_record(
        self,
        *,
        record_id: int,
        new_state: str,
        actor_id: int,
        source_context_id: Optional[str] = None,
        summary: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        record = await self.get_record(record_id)
        if not record:
            raise OperationalRecordValidationError(f"Unknown operational record: {record_id}")

        record_type = normalize_record_type(record["record_type"])
        current_state = str(record["state"])
        target_state = new_state
        validate_transition(record_type, current_state, target_state)
        target_state = str(target_state).strip().lower()

        before_snapshot = self._record_audit_snapshot(record)
        now = _utc_now_iso()
        resolved_at = record.get("resolved_at")
        archived_at = record.get("archived_at")
        if is_resolved_state(record_type, target_state):
            resolved_at = resolved_at or now
        elif is_resolved_state(record_type, current_state):
            resolved_at = None

        if is_archived_state(record_type, target_state):
            archived_at = archived_at or now
        elif is_archived_state(record_type, current_state):
            archived_at = None

        await self.db.execute(
            """UPDATE operational_records
            SET state = ?, updated_by_actor_id = ?, updated_at = ?,
            resolved_at = ?, archived_at = ?,
            source_context_id = COALESCE(?, source_context_id)
            WHERE id = ?""",
            (target_state, actor_id, now, resolved_at, archived_at, source_context_id, record_id),
            )
        updated = await self.get_record(record_id)
        await self.audit.record_mutation(
            entity_type="operational_record",
            entity_id=record_id,
            action="state_transitioned",
            before=before_snapshot,
            after=self._record_audit_snapshot(updated),
            actor_id=actor_id,
            source_context_id=source_context_id or (updated or record).get("source_context_id"),
            metadata={
                "record_type": record_type.value,
                "previous_state": current_state,
                "new_state": target_state,
            },
        )
        await self._append_event(
            record=updated,
            event_type="transitioned",
            actor_id=actor_id,
            previous_state=current_state,
            new_state=target_state,
            source_context_id=source_context_id,
            summary=summary or f"Transitioned {record_type.value} from {current_state} to {target_state}.",
            payload=payload,
        )
        return updated or record

    async def archive_record(
        self,
        *,
        record_id: int,
        actor_id: int,
        source_context_id: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        record = await self.get_record(record_id)
        if not record:
            raise OperationalRecordValidationError(f"Unknown operational record: {record_id}")

        now = _utc_now_iso()
        before_snapshot = self._record_audit_snapshot(record)
        await self.db.execute(
            """UPDATE operational_records
            SET archived_at = ?, updated_by_actor_id = ?, updated_at = ?,
            source_context_id = COALESCE(?, source_context_id)
            WHERE id = ?""",
            (now, actor_id, now, source_context_id, record_id),
            )
        updated = await self.get_record(record_id)
        await self.audit.record_mutation(
            entity_type="operational_record",
            entity_id=record_id,
            action="record_archived",
            before=before_snapshot,
            after=self._record_audit_snapshot(updated),
            actor_id=actor_id,
            source_context_id=source_context_id or (updated or record).get("source_context_id"),
            metadata={"record_type": record.get("record_type")},
        )
        await self._append_event(
            record=updated,
            event_type="archived",
            actor_id=actor_id,
            previous_state=record.get("state"),
            new_state=record.get("state"),
            source_context_id=source_context_id,
            summary=summary or "Archived operational record.",
            payload=None,
        )
        return updated or record

    async def _append_event(
        self,
        *,
        record: Optional[Dict[str, Any]],
        event_type: str,
        actor_id: int,
        previous_state: Optional[str],
        new_state: Optional[str],
        source_context_id: Optional[str],
        summary: Optional[str],
        payload: Optional[Mapping[str, Any]],
    ) -> None:
        if not record:
            return

        await self.db.execute(
            """INSERT INTO operational_record_events
            (record_id, stable_id, event_type, previous_state, new_state,
            actor_id, source_context_id, summary, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
            record["id"],
            record["stable_id"],
            event_type,
            previous_state,
            new_state,
            actor_id,
            source_context_id,
            summary,
            _json_dumps(payload),
            ),
            )