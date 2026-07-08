"""Minimum viable work packet service.

This layer owns workflow-state rows only. Business facts remain authoritative
in existing tables such as inbox_threads, obligations, tasks, and leads.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.services.audit_service import AuditService

WORK_PACKET_STATUSES = {
    "proposed",
    "active",
    "blocked",
    "awaiting_human",
    "completed",
    "cancelled",
    "failed",
}
WORK_PACKET_LANES = {"deterministic", "assistive", "reasoning", "maintenance"}
APPROVAL_STATUSES = {"not_required", "pending", "approved", "rejected"}


class WorkPacketService:
    """CRUD and lifecycle helpers for the minimum viable work packet kernel."""

    def __init__(self, db: Any):
        self.db = db
        self.audit = AuditService(db)

    def _audit_snapshot(self, packet: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not packet:
            return None
        return {
            "packet_key": packet.get("packet_key"),
            "packet_type": packet.get("packet_type"),
            "status": packet.get("status"),
            "lane": packet.get("lane"),
            "owner_kind": packet.get("owner_kind"),
            "owner_ref": packet.get("owner_ref"),
            "next_step": packet.get("next_step"),
            "blocked_reason": packet.get("blocked_reason"),
            "approval_required": bool(packet.get("approval_required")),
            "approval_status": packet.get("approval_status"),
            "current_summary": packet.get("current_summary"),
            "completion_summary": packet.get("completion_summary"),
            "terminal_reason": packet.get("terminal_reason"),
            "created_from_type": packet.get("created_from_type"),
            "created_from_id": packet.get("created_from_id"),
            "updated_at": packet.get("updated_at"),
        }

    async def _record_audit(
        self,
        *,
        packet_id: int,
        action: str,
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
        actor_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.audit.record_mutation(
            entity_type="work_packet",
            entity_id=packet_id,
            action=action,
            before=before,
            after=after,
            actor_id=actor_id,
            metadata=metadata,
        )

    async def create_packet(
        self,
        *,
        packet_key: str,
        packet_type: str,
        title: str,
        objective: str,
        status: str,
        lane: str,
        owner_kind: str,
        owner_ref: Optional[str] = None,
        next_step: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        approval_required: bool = False,
        approval_status: Optional[str] = None,
        current_summary: Optional[str] = None,
        completion_summary: Optional[str] = None,
        created_from_type: str = "manual",
        created_from_id: Optional[str] = None,
        terminal_reason: Optional[str] = None,
        actor_kind: str = "system",
        actor_ref: Optional[str] = None,
        actor_id: Optional[int] = None,
        summary: Optional[str] = None,
        related_job_run_id: Optional[int] = None,
        related_tool_execution_id: Optional[int] = None,
        related_chat_interaction_id: Optional[int] = None,
        related_inbox_thread_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._validate_status(status)
        self._validate_lane(lane)
        approval_status = self._normalize_approval_status(approval_required, approval_status)

        existing = await self.get_by_key(packet_key)
        if existing:
            return existing

        now = datetime.utcnow().isoformat()
        async with self.db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO work_packets
                   (packet_key, packet_type, title, objective, status, lane,
                    owner_kind, owner_ref, next_step, blocked_reason,
                    approval_required, approval_status, current_summary,
                    completion_summary, created_from_type, created_from_id,
                    created_at, updated_at, completed_at, terminal_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    packet_key,
                    packet_type,
                    title,
                    objective,
                    status,
                    lane,
                    owner_kind,
                    owner_ref,
                    next_step,
                    blocked_reason,
                    1 if approval_required else 0,
                    approval_status,
                    current_summary,
                    completion_summary,
                    created_from_type,
                    created_from_id,
                    now,
                    now,
                    now if status == "completed" else None,
                    terminal_reason,
                ),
            ) as cur:
                packet_id = cur.lastrowid
            await conn.commit()

        packet = await self.get(packet_id)
        await self._record_audit(
            packet_id=int(packet_id),
            action="packet_created",
            before=None,
            after=self._audit_snapshot(packet),
            actor_id=actor_id,
            metadata={"packet_type": packet_type},
        )
        await self.record_event(
            packet_id,
            event_type="packet_created",
            from_status=None,
            to_status=status,
            lane=lane,
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            summary=summary or f"Created {packet_type} packet.",
            related_job_run_id=related_job_run_id,
            related_tool_execution_id=related_tool_execution_id,
            related_chat_interaction_id=related_chat_interaction_id,
            related_inbox_thread_id=related_inbox_thread_id,
            requires_confirmation=approval_required,
            confirmation_status=approval_status,
            snapshot_json=self._snapshot_json(packet),
        )
        return packet

    async def get(self, packet_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM work_packets WHERE id = ?",
(packet_id,),)
        return dict(row) if row else None

    async def get_by_key(self, packet_key: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM work_packets WHERE packet_key = ?",
(packet_key,),)
        return dict(row) if row else None

    async def get_latest_by_source(self, created_from_type: str, created_from_id: Any) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("""SELECT * FROM work_packets
WHERE created_from_type = ? AND created_from_id = ?
ORDER BY created_at DESC LIMIT 1""",
(created_from_type, str(created_from_id)),)
        return dict(row) if row else None

    async def list_packets(self, *, limit: int = 50, include_completed: bool = True) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = ""
        if not include_completed:
            where = "WHERE status NOT IN ('completed', 'cancelled', 'failed')"
        params.append(min(max(limit, 1), 200))
        async with self.db.acquire() as conn, conn.execute(
            f"SELECT * FROM work_packets {where} ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_events(self, packet_id: int, *, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.acquire() as conn, conn.execute(
            """SELECT * FROM packet_events
                   WHERE packet_id = ?
                   ORDER BY created_at ASC, id ASC
                   LIMIT ?""",
            (packet_id, min(max(limit, 1), 500)),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def ensure_link(
        self,
        packet_id: int,
        *,
        link_role: str,
        target_type: str,
        target_id: Optional[Any] = None,
        target_key: Optional[str] = None,
        is_primary: bool = False,
        note: Optional[str] = None,
        actor_kind: str = "system",
        actor_ref: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        target_id_text = None if target_id is None else str(target_id)
        async with self.db.acquire() as conn:
            async with conn.execute(
                """SELECT * FROM packet_links
                   WHERE packet_id = ? AND link_role = ? AND target_type = ?
                     AND COALESCE(target_id, '') = COALESCE(?, '')
                     AND COALESCE(target_key, '') = COALESCE(?, '')""",
                (packet_id, link_role, target_type, target_id_text, target_key),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                return dict(existing)

            if is_primary:
                await conn.execute(
                    "UPDATE packet_links SET is_primary = 0 WHERE packet_id = ?",
                    (packet_id,),
                )

            async with conn.execute(
                """INSERT INTO packet_links
                   (packet_id, link_role, target_type, target_id, target_key, is_primary, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (packet_id, link_role, target_type, target_id_text, target_key, 1 if is_primary else 0, note),
            ) as cur:
                link_id = cur.lastrowid
            await conn.commit()

        packet = await self.get(packet_id)
        await self._record_audit(
            packet_id=packet_id,
            action="traceability_link_added",
            before=None,
            after={
                "link_role": link_role,
                "target_type": target_type,
                "target_id": target_id_text,
                "target_key": target_key,
                "is_primary": bool(is_primary),
                "note": note,
            },
            actor_id=actor_id,
            metadata={"packet_type": packet.get("packet_type") if packet else None},
        )
        await self.record_event(
            packet_id,
            event_type="packet_linked",
            from_status=packet.get("status") if packet else None,
            to_status=packet.get("status") if packet else None,
            lane=packet.get("lane") if packet else None,
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            summary=note or f"Linked {target_type} record to packet.",
            requires_confirmation=bool(packet and packet.get("approval_required")),
            confirmation_status=packet.get("approval_status") if packet else "not_required",
            snapshot_json=self._snapshot_json(packet),
        )
        row = await self.db.fetchone("SELECT * FROM packet_links WHERE id = ?", (link_id,))
        return dict(row)

    async def transition(
        self,
        packet_id: int,
        *,
        status: Optional[str] = None,
        lane: Optional[str] = None,
        owner_kind: Optional[str] = None,
        owner_ref: Optional[str] = None,
        next_step: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        approval_required: Optional[bool] = None,
        approval_status: Optional[str] = None,
        current_summary: Optional[str] = None,
        completion_summary: Optional[str] = None,
        terminal_reason: Optional[str] = None,
        event_type: str = "packet_status_changed",
        actor_kind: str = "system",
        actor_ref: Optional[str] = None,
        summary: Optional[str] = None,
        related_job_run_id: Optional[int] = None,
        related_tool_execution_id: Optional[int] = None,
        related_chat_interaction_id: Optional[int] = None,
        related_inbox_thread_id: Optional[int] = None,
        requires_confirmation: Optional[bool] = None,
        confirmation_status: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        packet = await self.get(packet_id)
        if not packet:
            raise ValueError(f"Unknown work packet: {packet_id}")

        before_snapshot = self._audit_snapshot(packet)
        target_status = status or packet["status"]
        target_lane = lane or packet["lane"]
        self._validate_status(target_status)
        self._validate_lane(target_lane)

        approval_required_value = packet["approval_required"] if approval_required is None else (1 if approval_required else 0)
        target_approval_status = self._normalize_approval_status(bool(approval_required_value), approval_status or packet["approval_status"])

        updates: Dict[str, Any] = {
            "status": target_status,
            "lane": target_lane,
            "owner_kind": owner_kind or packet["owner_kind"],
            "owner_ref": owner_ref if owner_ref is not None else packet.get("owner_ref"),
            "approval_required": approval_required_value,
            "approval_status": target_approval_status,
            "updated_at": datetime.utcnow().isoformat(),
        }
        if next_step is not None:
            updates["next_step"] = next_step
        if blocked_reason is not None:
            updates["blocked_reason"] = blocked_reason
        if current_summary is not None:
            updates["current_summary"] = current_summary
        if completion_summary is not None:
            updates["completion_summary"] = completion_summary
        if terminal_reason is not None:
            updates["terminal_reason"] = terminal_reason
        if target_status == "completed":
            updates["completed_at"] = datetime.utcnow().isoformat()

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [packet_id]
        await self.db.execute(
            f"UPDATE work_packets SET {set_clause} WHERE id = ?",
            tuple(params),
            )
        updated = await self.get(packet_id)
        await self._record_audit(
            packet_id=packet_id,
            action="state_transitioned" if packet.get("status") != (updated or {}).get("status") else "packet_updated",
            before=before_snapshot,
            after=self._audit_snapshot(updated),
            actor_id=actor_id,
            metadata={
                "event_type": event_type,
                "from_status": packet.get("status"),
                "to_status": updated.get("status") if updated else target_status,
            },
        )
        await self.record_event(
            packet_id,
            event_type=event_type,
            from_status=packet.get("status"),
            to_status=updated.get("status") if updated else target_status,
            lane=updated.get("lane") if updated else target_lane,
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            summary=summary,
            related_job_run_id=related_job_run_id,
            related_tool_execution_id=related_tool_execution_id,
            related_chat_interaction_id=related_chat_interaction_id,
            related_inbox_thread_id=related_inbox_thread_id,
            requires_confirmation=approval_required_value if requires_confirmation is None else requires_confirmation,
            confirmation_status=target_approval_status if confirmation_status is None else confirmation_status,
            snapshot_json=self._snapshot_json(updated),
        )
        return updated or packet

    async def record_event(
        self,
        packet_id: int,
        *,
        event_type: str,
        from_status: Optional[str],
        to_status: Optional[str],
        lane: Optional[str],
        actor_kind: str,
        actor_ref: Optional[str] = None,
        summary: Optional[str] = None,
        snapshot_json: Optional[str] = None,
        related_job_run_id: Optional[int] = None,
        related_tool_execution_id: Optional[int] = None,
        related_chat_interaction_id: Optional[int] = None,
        related_inbox_thread_id: Optional[int] = None,
        requires_confirmation: bool = False,
        confirmation_status: str = "not_required",
        actor_id: Optional[int] = None,
    ) -> int:
        confirmation_status = self._normalize_approval_status(bool(requires_confirmation), confirmation_status)
        should_audit_review = event_type in {"approval_requested", "approval_received", "approval_rejected"}
        packet = await self.get(packet_id) if should_audit_review else None
        async with self.db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO packet_events
                   (packet_id, event_type, from_status, to_status, lane,
                    actor_kind, actor_ref, summary, snapshot_json,
                    related_job_run_id, related_tool_execution_id,
                    related_chat_interaction_id, related_inbox_thread_id,
                    requires_confirmation, confirmation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    packet_id,
                    event_type,
                    from_status,
                    to_status,
                    lane,
                    actor_kind,
                    actor_ref,
                    summary,
                    snapshot_json,
                    related_job_run_id,
                    related_tool_execution_id,
                    related_chat_interaction_id,
                    related_inbox_thread_id,
                    1 if requires_confirmation else 0,
                    confirmation_status,
                ),
            ) as cur:
                event_id = cur.lastrowid
            await conn.commit()
        if should_audit_review:
            await self._record_audit(
                packet_id=packet_id,
                action={
                    "approval_requested": "review_requested",
                    "approval_received": "review_approved",
                    "approval_rejected": "review_rejected",
                }[event_type],
                before=None,
                after={
                    "status": packet.get("status") if packet else to_status,
                    "lane": lane,
                    "confirmation_status": confirmation_status,
                    "requires_confirmation": bool(requires_confirmation),
                    "summary": summary,
                },
                actor_id=actor_id,
                metadata={"event_type": event_type},
            )
        return int(event_id or 0)

    def _snapshot_json(self, packet: Optional[Dict[str, Any]]) -> Optional[str]:
        if not packet:
            return None
        snapshot = {
            "id": packet.get("id"),
            "packet_key": packet.get("packet_key"),
            "packet_type": packet.get("packet_type"),
            "status": packet.get("status"),
            "lane": packet.get("lane"),
            "owner_kind": packet.get("owner_kind"),
            "owner_ref": packet.get("owner_ref"),
            "next_step": packet.get("next_step"),
            "blocked_reason": packet.get("blocked_reason"),
            "approval_required": bool(packet.get("approval_required")),
            "approval_status": packet.get("approval_status"),
            "created_from_type": packet.get("created_from_type"),
            "created_from_id": packet.get("created_from_id"),
            "updated_at": packet.get("updated_at"),
        }
        return json.dumps(snapshot)

    def _normalize_approval_status(self, approval_required: bool, approval_status: Optional[str]) -> str:
        if not approval_required:
            return "not_required"
        candidate = approval_status or "pending"
        if candidate not in APPROVAL_STATUSES:
            raise ValueError(f"Invalid approval status: {candidate}")
        if candidate == "not_required":
            return "pending"
        return candidate

    def _validate_status(self, status: str) -> None:
        if status not in WORK_PACKET_STATUSES:
            raise ValueError(f"Invalid work packet status: {status}")

    def _validate_lane(self, lane: str) -> None:
        if lane not in WORK_PACKET_LANES:
            raise ValueError(f"Invalid work packet lane: {lane}")