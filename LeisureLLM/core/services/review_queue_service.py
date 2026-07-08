"""Unified operational review queue derived from canonical proposals and records."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from core.operational_records import ActionState
from core.services.audit_service import AuditService
from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_continuity_service import OperationalContinuityService
from core.services.operational_record_service import OperationalRecordService
from core.services.provenance_service import ProvenanceService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _json_dumps(value: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(dict(value or {}), sort_keys=True)


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_dt(raw_value: Optional[str]) -> Optional[datetime]:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _severity_rank(value: str) -> int:
    return {
        "low": 1,
        "medium": 2,
        "high": 3,
        "critical": 4,
    }.get(str(value or "").strip().lower(), 0)


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


class ReviewItemType(str, Enum):
    EXTRACTION_PROPOSAL_LOW_CONFIDENCE = "extraction_proposal_low_confidence"
    EXTRACTION_PROPOSAL_PENDING_HUMAN_REVIEW = "extraction_proposal_pending_human_review"
    ACTION_OVERDUE = "action_overdue"
    ACTION_UNOWNED = "action_unowned"
    DECISION_UNRESOLVED = "decision_unresolved"
    BLOCKER_ESCALATED_OR_STALE = "blocker_escalated_or_stale"


class ReviewSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True)
class ReviewQueueItem:
    id: str
    item_type: str
    severity: str
    created_at: Optional[str]
    detected_at: Optional[str]
    last_seen_at: Optional[str]
    owner_id: Optional[int] = None
    owner_display_name: Optional[str] = None
    project_scope: Optional[str] = None
    workspace_scope: Optional[str] = None
    operational_record_id: Optional[int] = None
    proposal_id: Optional[int] = None
    underlying_entity_type: Optional[str] = None
    underlying_entity_id: Optional[str] = None
    created_by_actor_id: Optional[int] = None
    reason: Optional[str] = None
    recommended_next_actions: list[str] = field(default_factory=list)
    source_references: list[dict[str, Any]] = field(default_factory=list)
    proposal: Optional[dict[str, Any]] = None
    record: Optional[dict[str, Any]] = None
    continuity_state_ids: list[int] = field(default_factory=list)
    continuity_states: list[str] = field(default_factory=list)
    defer_count: int = 0
    deferred_until: Optional[str] = None
    deferral_rationale: Optional[str] = None
    escalation_destination: dict[str, Any] = field(default_factory=dict)
    escalated_at: Optional[str] = None
    last_action_at: Optional[str] = None
    last_action_type: Optional[str] = None
    is_deferred: bool = False
    must_surface_in_weekly_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewQueuePolicy:
    low_confidence_threshold: float = 0.65
    weekly_unresolved_min_severity: str = ReviewSeverity.HIGH.value
    repeated_deferral_bump_after: int = 2
    repeated_deferral_escalate_after: int = 3
    max_defer_days_by_item_type: dict[str, int] = field(
        default_factory=lambda: {
            ReviewItemType.EXTRACTION_PROPOSAL_LOW_CONFIDENCE.value: 14,
            ReviewItemType.EXTRACTION_PROPOSAL_PENDING_HUMAN_REVIEW.value: 30,
            ReviewItemType.ACTION_OVERDUE.value: 7,
            ReviewItemType.ACTION_UNOWNED.value: 14,
            ReviewItemType.DECISION_UNRESOLVED.value: 21,
            ReviewItemType.BLOCKER_ESCALATED_OR_STALE.value: 7,
        }
    )
    max_defer_days_by_severity: dict[str, int] = field(
        default_factory=lambda: {
            ReviewSeverity.LOW.value: 30,
            ReviewSeverity.MEDIUM.value: 21,
            ReviewSeverity.HIGH.value: 7,
            ReviewSeverity.CRITICAL.value: 3,
        }
    )


class ReviewQueueService:
    """Aggregates reviewable work while mutating canonical underlying objects."""

    def __init__(self, db: Any, *, policy: Optional[ReviewQueuePolicy] = None):
        self.db = db
        self.policy = policy or ReviewQueuePolicy()
        self.audit = AuditService(db)
        self.records = OperationalRecordService(db)
        self.proposals = ExtractionProposalService(db)
        self.continuity = OperationalContinuityService(db)
        self.provenance = ProvenanceService(db)

    async def list_items(
        self,
        *,
        owner_id: Optional[int] = None,
        project_scope: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        severity: Optional[str] = None,
        item_type: Optional[str] = None,
        scope: str = "all",
        current_actor_id: Optional[int] = None,
        include_deferred: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        proposal_items = await self._build_proposal_items()
        continuity_items = await self._build_continuity_items()
        items_by_id: dict[str, ReviewQueueItem] = {}
        for item in [*proposal_items, *continuity_items]:
            items_by_id[item.id] = item

        await self._apply_overlay_state(items_by_id)

        filtered: list[ReviewQueueItem] = []
        normalized_scope = str(scope or "all").strip().lower()
        normalized_severity = str(severity or "").strip().lower() or None
        normalized_item_type = str(item_type or "").strip().lower() or None
        normalized_project_scope = str(project_scope or "").strip() or None
        normalized_workspace_scope = str(workspace_scope or "").strip() or None

        for item_obj in items_by_id.values():
            if normalized_item_type and item_obj.item_type != normalized_item_type:
                continue
            if normalized_severity and item_obj.severity != normalized_severity:
                continue
            if owner_id is not None and item_obj.owner_id != int(owner_id):
                continue
            if normalized_project_scope and item_obj.project_scope != normalized_project_scope:
                continue
            if normalized_workspace_scope and item_obj.workspace_scope != normalized_workspace_scope:
                continue
            if item_obj.is_deferred and not include_deferred:
                continue
            if not self._scope_match(item_obj, scope=normalized_scope, current_actor_id=current_actor_id):
                continue
            filtered.append(item_obj)

        filtered.sort(key=self._sort_key)
        return [item.to_dict() for item in filtered[: min(max(int(limit), 1), 500)]]

    async def get_item(
        self,
        item_id: str,
        *,
        current_actor_id: Optional[int] = None,
        include_deferred: bool = True,
    ) -> Optional[dict[str, Any]]:
        for item in await self.list_items(
            current_actor_id=current_actor_id,
            include_deferred=include_deferred,
            limit=500,
        ):
            if item["id"] == item_id:
                return item
        return None

    async def apply_action(
        self,
        *,
        item_id: str,
        action: str,
        actor_id: int,
        rationale: Optional[str] = None,
        final_fields: Optional[Mapping[str, Any]] = None,
        merge_record_id: Optional[int] = None,
        owner_id: Optional[int] = None,
        defer_until: Optional[str] = None,
        new_state: Optional[str] = None,
        severity: Optional[str] = None,
        escalation_destination: Optional[Mapping[str, Any]] = None,
        review_notes: Optional[str] = None,
    ) -> dict[str, Any]:
        queue_item = await self.get_item(item_id, include_deferred=True)
        if not queue_item:
            raise ValueError(f"Unknown review queue item: {item_id}")

        normalized_action = str(action or "").strip().lower()
        if normalized_action in {"accept_proposal", "accept", "edit_accept", "accept_with_edits", "merge_proposal", "merge"}:
            return await self._handle_proposal_accept(
                queue_item=queue_item,
                action=normalized_action,
                actor_id=actor_id,
                final_fields=final_fields,
                merge_record_id=merge_record_id,
                review_notes=review_notes or rationale,
            )
        if normalized_action in {"reject_proposal", "reject"}:
            return await self._handle_proposal_reject(
                queue_item=queue_item,
                actor_id=actor_id,
                rationale=rationale,
                review_notes=review_notes,
            )
        if normalized_action == "assign_owner":
            return await self._handle_assign_owner(queue_item=queue_item, actor_id=actor_id, owner_id=owner_id, rationale=rationale)
        if normalized_action == "defer":
            return await self.defer_item(queue_item=queue_item, actor_id=actor_id, defer_until=defer_until, rationale=rationale)
        if normalized_action == "escalate":
            return await self.escalate_item(
                queue_item=queue_item,
                actor_id=actor_id,
                rationale=rationale,
                severity=severity,
                escalation_destination=escalation_destination,
            )
        if normalized_action == "resolve":
            return await self.resolve_item(queue_item=queue_item, actor_id=actor_id, new_state=new_state, rationale=rationale)
        raise ValueError("Unknown review queue action")

    async def bulk_apply_action(
        self,
        *,
        item_ids: Iterable[str],
        action: str,
        actor_id: int,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        results = []
        errors = []
        action_payload = dict(payload or {})
        for item_id in item_ids:
            try:
                results.append(
                    await self.apply_action(
                        item_id=str(item_id),
                        action=action,
                        actor_id=actor_id,
                        rationale=action_payload.get("rationale"),
                        final_fields=action_payload.get("final_fields"),
                        merge_record_id=_coerce_optional_int(action_payload.get("merge_record_id")),
                        owner_id=_coerce_optional_int(action_payload.get("owner_id")),
                        defer_until=action_payload.get("defer_until"),
                        new_state=action_payload.get("new_state"),
                        severity=action_payload.get("severity"),
                        escalation_destination=action_payload.get("escalation_destination"),
                        review_notes=action_payload.get("review_notes"),
                    )
                )
            except Exception as exc:
                errors.append({"item_id": str(item_id), "error": str(exc)})
        return {
            "action": action,
            "results": results,
            "errors": errors,
            "success_count": len(results),
            "error_count": len(errors),
        }

    async def defer_item(
        self,
        *,
        queue_item: Mapping[str, Any],
        actor_id: int,
        defer_until: Optional[str],
        rationale: Optional[str],
    ) -> dict[str, Any]:
        if not defer_until:
            raise ValueError("Deferred review items require a defer_until timestamp")
        defer_until_dt = _normalize_dt(defer_until)
        if defer_until_dt is None:
            raise ValueError("Invalid defer_until timestamp")
        now = _utc_now()
        if defer_until_dt <= now:
            raise ValueError("defer_until must be in the future")
        rationale_value = str(rationale or "").strip()
        if not rationale_value:
            raise ValueError("Deferred review items require rationale")

        item_type = str(queue_item["item_type"])
        current_severity = str(queue_item["severity"])
        max_defer_days = min(
            self.policy.max_defer_days_by_item_type.get(item_type, 14),
            self.policy.max_defer_days_by_severity.get(current_severity, 14),
        )
        if defer_until_dt > now + timedelta(days=max_defer_days):
            raise ValueError(f"{item_type} can only be deferred up to {max_defer_days} days")

        state_row = await self._get_overlay_state(str(queue_item["id"]))
        defer_count = int((state_row or {}).get("defer_count") or 0) + 1
        severity_override = None
        escalation_destination_payload = _json_loads((state_row or {}).get("escalation_destination_json"))
        if defer_count >= self.policy.repeated_deferral_bump_after:
            severity_override = self._bump_severity(current_severity)
        if defer_count >= self.policy.repeated_deferral_escalate_after and not escalation_destination_payload:
            escalation_destination_payload = {
                "route": "weekly_review",
                "reason": "Repeated deferrals exceeded threshold.",
            }

        before_snapshot = self._overlay_snapshot(state_row)
        updated_row = await self._upsert_overlay_state(
            queue_item=queue_item,
            values={
                "deferred_until": defer_until_dt.isoformat(),
                "deferral_rationale": rationale_value,
                "defer_count": defer_count,
                "severity_override": severity_override,
                "escalation_destination_json": _json_dumps(escalation_destination_payload),
                "updated_at": _utc_now_iso(),
                "last_action_at": _utc_now_iso(),
                "last_action_type": "defer",
                "last_action_by_actor_id": actor_id,
                "escalated_at": _utc_now_iso() if escalation_destination_payload and not (state_row or {}).get("escalated_at") else (state_row or {}).get("escalated_at"),
                "escalated_by_actor_id": actor_id if escalation_destination_payload else (state_row or {}).get("escalated_by_actor_id"),
            },
        )
        await self._record_queue_action(
            queue_item=queue_item,
            action_type="review_deferred",
            actor_id=actor_id,
            rationale=rationale_value,
            payload={
                "defer_until": defer_until_dt.isoformat(),
                "defer_count": defer_count,
                "severity_override": severity_override,
                "escalation_destination": escalation_destination_payload,
            },
            before=before_snapshot,
            after=self._overlay_snapshot(updated_row),
        )
        refreshed = await self.get_item(str(queue_item["id"]), include_deferred=True)
        return {"success": True, "item": refreshed}

    async def escalate_item(
        self,
        *,
        queue_item: Mapping[str, Any],
        actor_id: int,
        rationale: Optional[str],
        severity: Optional[str],
        escalation_destination: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        rationale_value = str(rationale or "").strip()
        if not rationale_value:
            raise ValueError("Escalation requires rationale")
        current_severity = str(queue_item["severity"])
        target_severity = str(severity or self._bump_severity(current_severity)).strip().lower()
        if target_severity not in {level.value for level in ReviewSeverity}:
            raise ValueError("Unknown escalation severity")

        state_row = await self._get_overlay_state(str(queue_item["id"]))
        before_snapshot = self._overlay_snapshot(state_row)
        updated_row = await self._upsert_overlay_state(
            queue_item=queue_item,
            values={
                "severity_override": target_severity,
                "escalation_destination_json": _json_dumps(escalation_destination),
                "escalated_at": _utc_now_iso(),
                "escalated_by_actor_id": actor_id,
                "updated_at": _utc_now_iso(),
                "last_action_at": _utc_now_iso(),
                "last_action_type": "escalate",
                "last_action_by_actor_id": actor_id,
            },
        )
        await self._record_queue_action(
            queue_item=queue_item,
            action_type="review_escalated",
            actor_id=actor_id,
            rationale=rationale_value,
            payload={
                "severity": target_severity,
                "escalation_destination": dict(escalation_destination or {}),
            },
            before=before_snapshot,
            after=self._overlay_snapshot(updated_row),
        )
        refreshed = await self.get_item(str(queue_item["id"]), include_deferred=True)
        return {"success": True, "item": refreshed}

    async def resolve_item(
        self,
        *,
        queue_item: Mapping[str, Any],
        actor_id: int,
        new_state: Optional[str],
        rationale: Optional[str],
    ) -> dict[str, Any]:
        record_id = _coerce_optional_int(queue_item.get("operational_record_id"))
        if not record_id:
            raise ValueError("Only record-backed review items can be resolved")
        target_state = str(new_state or "").strip().lower()
        if not target_state:
            raise ValueError("Resolving a review item requires new_state")
        summary = str(rationale or "").strip() or f"Resolved from unified review queue as {target_state}."
        await self.records.transition_record(
            record_id=record_id,
            new_state=target_state,
            actor_id=actor_id,
            source_context_id=f"review-queue:resolve:{queue_item['id']}",
            summary=summary,
            payload={"review_item_id": queue_item["id"], "item_type": queue_item["item_type"]},
        )
        await self.continuity.run_sweep(actor_id=actor_id, source_context_id=f"review-queue:sweep:{queue_item['id']}")
        state_row = await self._get_overlay_state(str(queue_item["id"]))
        before_snapshot = self._overlay_snapshot(state_row)
        updated_row = await self._upsert_overlay_state(
            queue_item=queue_item,
            values={
                "resolved_at": _utc_now_iso(),
                "resolved_by_actor_id": actor_id,
                "resolution_rationale": summary,
                "updated_at": _utc_now_iso(),
                "last_action_at": _utc_now_iso(),
                "last_action_type": "resolve",
                "last_action_by_actor_id": actor_id,
                "deferred_until": None,
            },
        )
        await self._record_queue_action(
            queue_item=queue_item,
            action_type="review_resolved",
            actor_id=actor_id,
            rationale=summary,
            payload={"new_state": target_state, "operational_record_id": record_id},
            before=before_snapshot,
            after=self._overlay_snapshot(updated_row),
        )
        refreshed = await self.get_item(str(queue_item["id"]), include_deferred=True)
        return {"success": True, "resolved": refreshed is None, "item": refreshed}

    async def generate_review_session(
        self,
        *,
        cadence: str,
        actor_id: int,
        scope: str = "all",
        owner_id: Optional[int] = None,
        workspace_scope: Optional[str] = None,
        project_scope: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_cadence = str(cadence or "").strip().lower()
        if normalized_cadence not in {"daily", "weekly"}:
            raise ValueError("cadence must be daily or weekly")

        items = await self.list_items(
            owner_id=owner_id,
            workspace_scope=workspace_scope,
            project_scope=project_scope,
            scope=scope,
            current_actor_id=actor_id,
            include_deferred=True,
            limit=500,
        )
        session_items = []
        for item in items:
            if normalized_cadence == "daily":
                if item.get("is_deferred"):
                    continue
                session_items.append(item)
                continue
            if not item.get("is_deferred") or item.get("must_surface_in_weekly_review"):
                session_items.append(item)

        session_stable_id = f"review_session_{uuid.uuid4().hex}"
        now = _utc_now_iso()
        snapshot = {
            "cadence": normalized_cadence,
            "scope": scope,
            "item_count": len(session_items),
            "generated_at": now,
        }
        async with self.db.acquire() as conn:
            async with conn.execute(
                """
                INSERT INTO operational_review_sessions
                    (session_id, cadence, scope, owner_id, workspace_scope, project_scope,
                     snapshot_json, created_by_actor_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_stable_id,
                    normalized_cadence,
                    str(scope or "all").strip().lower(),
                    owner_id,
                    workspace_scope,
                    project_scope,
                    _json_dumps(snapshot),
                    actor_id,
                    now,
                ),
            ) as cur:
                session_row_id = cur.lastrowid
            for item in session_items:
                await conn.execute(
                    """
                    INSERT INTO operational_review_session_items
                        (review_session_id, review_item_id, item_type, severity, snapshot_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_row_id,
                        item["id"],
                        item["item_type"],
                        item["severity"],
                        _json_dumps(item),
                        now,
                    ),
                )
            await conn.commit()

        await self.audit.record_mutation(
            entity_type="review_queue_session",
            entity_id=session_stable_id,
            action="review_session_generated",
            before=None,
            after=snapshot,
            actor_id=actor_id,
            source_context_id=f"review-queue:session:{session_stable_id}",
            metadata={"cadence": normalized_cadence, "item_count": len(session_items)},
        )
        return await self.get_review_session(session_stable_id) or {}

    async def complete_review_session(self, *, session_id: str, actor_id: int, completion_notes: Optional[str] = None) -> dict[str, Any]:
        session = await self.get_review_session(session_id)
        if not session:
            raise ValueError(f"Unknown review session: {session_id}")
        now = _utc_now_iso()
        await self.db.execute(
            """
            UPDATE operational_review_sessions
            SET completed_at = ?, completed_by_actor_id = ?, completion_notes = ?
            WHERE session_id = ?
            """,
            (now, actor_id, completion_notes, session_id),
            )
        await self.audit.record_mutation(
            entity_type="review_queue_session",
            entity_id=session_id,
            action="review_session_completed",
            before={"completed_at": session.get("completed_at")},
            after={"completed_at": now, "completion_notes": completion_notes},
            actor_id=actor_id,
            source_context_id=f"review-queue:session:{session_id}",
            metadata={"cadence": session.get("cadence")},
        )
        return await self.get_review_session(session_id) or {}

    async def get_review_session(self, session_id: str) -> Optional[dict[str, Any]]:
        async with self.db.acquire() as conn:
            async with conn.execute("SELECT * FROM operational_review_sessions WHERE session_id = ?", (session_id,)) as cur:
                session_row = await cur.fetchone()
            if not session_row:
                return None
            async with conn.execute(
                "SELECT * FROM operational_review_session_items WHERE review_session_id = ? ORDER BY id ASC",
                (session_row["id"],),
            ) as cur:
                item_rows = await cur.fetchall()
        session = dict(session_row)
        session["snapshot"] = _json_loads(session.pop("snapshot_json", None))
        session["items"] = []
        for row in item_rows:
            item = dict(row)
            item["snapshot"] = _json_loads(item.pop("snapshot_json", None))
            session["items"].append(item)
        return session

    async def _build_proposal_items(self) -> list[ReviewQueueItem]:
        async with self.db.acquire() as conn, conn.execute(
            """
                SELECT p.*, a.display_name AS created_by_actor_name
                FROM operational_extraction_proposals p
                LEFT JOIN operational_actors a ON a.id = p.created_by_actor_id
                WHERE p.status = 'pending'
                ORDER BY p.created_at ASC, p.id ASC
                """
        ) as cur:
            rows = await cur.fetchall()

        items: list[ReviewQueueItem] = []
        for raw_row in rows:
            row = dict(raw_row)
            extracted_fields = _json_loads(row.get("extracted_fields_json"))
            source_details = _json_loads(row.get("source_details_json"))
            effective_confidence = float(row.get("effective_confidence") or 0.0)
            if effective_confidence <= self.policy.low_confidence_threshold:
                item_type = ReviewItemType.EXTRACTION_PROPOSAL_LOW_CONFIDENCE.value
                severity = ReviewSeverity.HIGH.value if effective_confidence <= 0.4 else ReviewSeverity.MEDIUM.value
                reason = (
                    f"Extraction confidence is {effective_confidence:.2f}; human review is required before promotion."
                )
                next_actions = ["Review source evidence", "Edit proposal fields", "Accept or reject proposal"]
            else:
                item_type = ReviewItemType.EXTRACTION_PROPOSAL_PENDING_HUMAN_REVIEW.value
                severity = ReviewSeverity.LOW.value
                reason = "Proposal is still awaiting explicit human review before it becomes canonical."
                next_actions = ["Review proposal", "Accept if accurate", "Reject if unsupported"]
            item_id = self._proposal_item_id(item_type=item_type, proposal_id=int(row["id"]))
            proposal_payload = self._proposal_row_to_public_dict(row)
            owner_id = _coerce_optional_int(extracted_fields.get("owner_id"))
            items.append(
                ReviewQueueItem(
                    id=item_id,
                    item_type=item_type,
                    severity=severity,
                    created_at=row.get("created_at"),
                    detected_at=row.get("created_at"),
                    last_seen_at=row.get("updated_at") or row.get("created_at"),
                    owner_id=owner_id,
                    project_scope=str(extracted_fields.get("project_scope") or source_details.get("project_scope") or "").strip() or None,
                    workspace_scope=str(extracted_fields.get("workspace_scope") or source_details.get("workspace_scope") or "").strip() or None,
                    proposal_id=int(row["id"]),
                    underlying_entity_type="operational_extraction_proposal",
                    underlying_entity_id=str(row["id"]),
                    created_by_actor_id=_coerce_optional_int(row.get("created_by_actor_id")),
                    reason=reason,
                    recommended_next_actions=next_actions,
                    source_references=[
                        {
                            "entity_type": row.get("source_entity_type"),
                            "entity_id": row.get("source_entity_id"),
                            "label": source_details.get("label") or row.get("title"),
                            "summary": source_details.get("summary") or row.get("summary"),
                            "source_context_id": row.get("source_context_id"),
                        }
                    ],
                    proposal=proposal_payload,
                )
            )
        return items

    async def _build_continuity_items(self) -> list[ReviewQueueItem]:
        async with self.db.acquire() as conn, conn.execute(
            """
                SELECT s.*, r.title, r.summary, r.state AS record_state, r.owner_id, r.project_scope,
                       r.workspace_scope, r.created_at AS record_created_at, r.updated_at AS record_updated_at,
                       r.review_at, r.due_at, r.stale_after_at, r.created_by_actor_id,
                       a.display_name AS owner_display_name
                FROM operational_continuity_states s
                JOIN operational_records r ON r.id = s.record_id
                LEFT JOIN operational_actors a ON a.id = r.owner_id
                WHERE s.status = 'active'
                  AND (
                    (s.record_type = 'action' AND s.continuity_state IN ('overdue', 'unowned'))
                    OR (s.record_type = 'decision' AND s.continuity_state = 'unresolved')
                    OR (s.record_type = 'blocker' AND s.continuity_state IN ('stale', 'escalated'))
                  )
                ORDER BY s.first_observed_at ASC, s.id ASC
                """
        ) as cur:
            rows = await cur.fetchall()

        grouped_blockers: dict[int, list[dict[str, Any]]] = {}
        items: list[ReviewQueueItem] = []
        for raw_row in rows:
            row = dict(raw_row)
            record_ref = self._record_source_payload(row)
            state = str(row.get("continuity_state") or "")
            if row.get("record_type") == "blocker":
                grouped_blockers.setdefault(int(row["record_id"]), []).append(row)
                continue
            if row.get("record_type") == "action" and state == "overdue":
                details = _json_loads(row.get("details_json"))
                severity = ReviewSeverity.CRITICAL.value if int(details.get("days_overdue") or 0) >= 7 else ReviewSeverity.HIGH.value
                items.append(
                    ReviewQueueItem(
                        id=self._record_item_id(ReviewItemType.ACTION_OVERDUE.value, int(row["record_id"])),
                        item_type=ReviewItemType.ACTION_OVERDUE.value,
                        severity=severity,
                        created_at=row.get("record_created_at"),
                        detected_at=row.get("first_observed_at"),
                        last_seen_at=row.get("last_observed_at"),
                        owner_id=_coerce_optional_int(row.get("owner_id")),
                        owner_display_name=row.get("owner_display_name"),
                        project_scope=row.get("project_scope"),
                        workspace_scope=row.get("workspace_scope"),
                        operational_record_id=int(row["record_id"]),
                        underlying_entity_type="operational_record",
                        underlying_entity_id=str(row["record_id"]),
                        created_by_actor_id=_coerce_optional_int(row.get("created_by_actor_id")),
                        reason=str(row.get("reason") or "Action is overdue."),
                        recommended_next_actions=["Assign an owner", "Replan due date if justified", "Resolve the action"],
                        record=record_ref,
                        continuity_state_ids=[int(row["id"])],
                        continuity_states=[state],
                    )
                )
                continue
            if row.get("record_type") == "action" and state == "unowned":
                items.append(
                    ReviewQueueItem(
                        id=self._record_item_id(ReviewItemType.ACTION_UNOWNED.value, int(row["record_id"])),
                        item_type=ReviewItemType.ACTION_UNOWNED.value,
                        severity=ReviewSeverity.MEDIUM.value,
                        created_at=row.get("record_created_at"),
                        detected_at=row.get("first_observed_at"),
                        last_seen_at=row.get("last_observed_at"),
                        owner_id=_coerce_optional_int(row.get("owner_id")),
                        owner_display_name=row.get("owner_display_name"),
                        project_scope=row.get("project_scope"),
                        workspace_scope=row.get("workspace_scope"),
                        operational_record_id=int(row["record_id"]),
                        underlying_entity_type="operational_record",
                        underlying_entity_id=str(row["record_id"]),
                        created_by_actor_id=_coerce_optional_int(row.get("created_by_actor_id")),
                        reason=str(row.get("reason") or "Action has no owner."),
                        recommended_next_actions=["Assign an owner", "Confirm accountability", "Escalate if ownership is unclear"],
                        record=record_ref,
                        continuity_state_ids=[int(row["id"])],
                        continuity_states=[state],
                    )
                )
                continue
            if row.get("record_type") == "decision" and state == "unresolved":
                details = _json_loads(row.get("details_json"))
                severity = ReviewSeverity.HIGH.value if int(details.get("days_unresolved") or 0) >= 7 else ReviewSeverity.MEDIUM.value
                items.append(
                    ReviewQueueItem(
                        id=self._record_item_id(ReviewItemType.DECISION_UNRESOLVED.value, int(row["record_id"])),
                        item_type=ReviewItemType.DECISION_UNRESOLVED.value,
                        severity=severity,
                        created_at=row.get("record_created_at"),
                        detected_at=row.get("first_observed_at"),
                        last_seen_at=row.get("last_observed_at"),
                        owner_id=_coerce_optional_int(row.get("owner_id")),
                        owner_display_name=row.get("owner_display_name"),
                        project_scope=row.get("project_scope"),
                        workspace_scope=row.get("workspace_scope"),
                        operational_record_id=int(row["record_id"]),
                        underlying_entity_type="operational_record",
                        underlying_entity_id=str(row["record_id"]),
                        created_by_actor_id=_coerce_optional_int(row.get("created_by_actor_id")),
                        reason=str(row.get("reason") or "Decision remains unresolved."),
                        recommended_next_actions=["Accept or reject the decision", "Record rationale", "Escalate if blocked"],
                        record=record_ref,
                        continuity_state_ids=[int(row["id"])],
                        continuity_states=[state],
                    )
                )

        for record_id, state_rows in grouped_blockers.items():
            continuity_states = sorted({str(row.get("continuity_state") or "") for row in state_rows})
            state_ids = [int(row["id"]) for row in state_rows]
            escalated_row = next((row for row in state_rows if row.get("continuity_state") == "escalated"), None)
            reference_row = escalated_row or state_rows[0]
            severity = ReviewSeverity.CRITICAL.value if escalated_row else ReviewSeverity.HIGH.value
            reason = str(reference_row.get("reason") or "Blocker requires escalation or stale review.")
            items.append(
                ReviewQueueItem(
                    id=self._record_item_id(ReviewItemType.BLOCKER_ESCALATED_OR_STALE.value, record_id),
                    item_type=ReviewItemType.BLOCKER_ESCALATED_OR_STALE.value,
                    severity=severity,
                    created_at=reference_row.get("record_created_at"),
                    detected_at=min((row.get("first_observed_at") for row in state_rows if row.get("first_observed_at")), default=None),
                    last_seen_at=max((row.get("last_observed_at") for row in state_rows if row.get("last_observed_at")), default=None),
                    owner_id=_coerce_optional_int(reference_row.get("owner_id")),
                    owner_display_name=reference_row.get("owner_display_name"),
                    project_scope=reference_row.get("project_scope"),
                    workspace_scope=reference_row.get("workspace_scope"),
                    operational_record_id=record_id,
                    underlying_entity_type="operational_record",
                    underlying_entity_id=str(record_id),
                    created_by_actor_id=_coerce_optional_int(reference_row.get("created_by_actor_id")),
                    reason=reason,
                    recommended_next_actions=["Mitigate blocker", "Escalate ownership", "Resolve or restate current blocker status"],
                    record=self._record_source_payload(reference_row),
                    continuity_state_ids=state_ids,
                    continuity_states=continuity_states,
                )
            )

        record_ids = [item.operational_record_id for item in items if item.operational_record_id]
        source_refs = await self._load_record_source_references(record_ids)
        for item in items:
            if item.operational_record_id:
                item.source_references = source_refs.get(int(item.operational_record_id), [])
        return items

    async def _apply_overlay_state(self, items_by_id: dict[str, ReviewQueueItem]) -> None:
        if not items_by_id:
            return
        placeholders = ", ".join("?" for _ in items_by_id)
        async with self.db.acquire() as conn, conn.execute(
            f"SELECT * FROM operational_review_queue_state WHERE review_item_id IN ({placeholders})",
            tuple(items_by_id.keys()),
        ) as cur:
            rows = await cur.fetchall()
        now = _utc_now()
        for raw_row in rows:
            row = dict(raw_row)
            item = items_by_id.get(str(row.get("review_item_id")))
            if not item:
                continue
            severity_override = str(row.get("severity_override") or "").strip().lower() or None
            if severity_override and _severity_rank(severity_override) > _severity_rank(item.severity):
                item.severity = severity_override
            item.defer_count = int(row.get("defer_count") or 0)
            item.deferred_until = row.get("deferred_until")
            item.deferral_rationale = row.get("deferral_rationale")
            item.escalation_destination = _json_loads(row.get("escalation_destination_json"))
            item.escalated_at = row.get("escalated_at")
            item.last_action_at = row.get("last_action_at")
            item.last_action_type = row.get("last_action_type")
            deferred_until_dt = _normalize_dt(row.get("deferred_until"))
            item.is_deferred = deferred_until_dt is not None and deferred_until_dt > now and not row.get("resolved_at")
            item.must_surface_in_weekly_review = self._must_surface_in_weekly_review(item)

        for item in items_by_id.values():
            if not item.must_surface_in_weekly_review:
                item.must_surface_in_weekly_review = self._must_surface_in_weekly_review(item)

    async def _load_record_source_references(self, record_ids: Iterable[Optional[int]]) -> dict[int, list[dict[str, Any]]]:
        references: dict[int, list[dict[str, Any]]] = {}
        for record_id in sorted({int(record_id) for record_id in record_ids if record_id}):
            explanation = await self.provenance.explain_record_origin(record_id)
            refs = []
            for origin in explanation.get("origins", [])[:5]:
                source = origin.get("source") or {}
                refs.append(
                    {
                        "entity_type": source.get("entity_type") or origin.get("source_entity_type"),
                        "entity_id": source.get("entity_id") or origin.get("source_entity_id"),
                        "label": source.get("label"),
                        "summary": source.get("summary"),
                        "relationship": origin.get("relationship"),
                    }
                )
            references[record_id] = refs
        return references

    async def _handle_proposal_accept(
        self,
        *,
        queue_item: Mapping[str, Any],
        action: str,
        actor_id: int,
        final_fields: Optional[Mapping[str, Any]],
        merge_record_id: Optional[int],
        review_notes: Optional[str],
    ) -> dict[str, Any]:
        proposal_id = _coerce_optional_int(queue_item.get("proposal_id"))
        if not proposal_id:
            raise ValueError("Proposal queue actions require a proposal-backed item")
        result = await self.proposals.accept_proposal(
            proposal_id=proposal_id,
            actor_id=actor_id,
            final_fields=final_fields,
            merge_record_id=merge_record_id,
            review_notes=review_notes,
        )
        state_row = await self._get_overlay_state(str(queue_item["id"]))
        before_snapshot = self._overlay_snapshot(state_row)
        updated_row = await self._upsert_overlay_state(
            queue_item=queue_item,
            values={
                "resolved_at": _utc_now_iso(),
                "resolved_by_actor_id": actor_id,
                "resolution_rationale": review_notes,
                "updated_at": _utc_now_iso(),
                "last_action_at": _utc_now_iso(),
                "last_action_type": "accept_proposal",
                "last_action_by_actor_id": actor_id,
            },
        )
        await self._record_queue_action(
            queue_item=queue_item,
            action_type="proposal_accepted_from_queue",
            actor_id=actor_id,
            rationale=review_notes,
            payload={"merge_record_id": merge_record_id, "final_fields": dict(final_fields or {})},
            before=before_snapshot,
            after=self._overlay_snapshot(updated_row),
        )
        return {"success": True, "review_action": "accepted", **result}

    async def _handle_proposal_reject(
        self,
        *,
        queue_item: Mapping[str, Any],
        actor_id: int,
        rationale: Optional[str],
        review_notes: Optional[str],
    ) -> dict[str, Any]:
        proposal_id = _coerce_optional_int(queue_item.get("proposal_id"))
        if not proposal_id:
            raise ValueError("Proposal queue actions require a proposal-backed item")
        rationale_value = str(rationale or "").strip()
        if not rationale_value:
            raise ValueError("Rejecting a proposal requires rationale")
        proposal = await self.proposals.reject_proposal(
            proposal_id=proposal_id,
            actor_id=actor_id,
            reason=rationale_value,
            review_notes=review_notes,
        )
        state_row = await self._get_overlay_state(str(queue_item["id"]))
        before_snapshot = self._overlay_snapshot(state_row)
        updated_row = await self._upsert_overlay_state(
            queue_item=queue_item,
            values={
                "resolved_at": _utc_now_iso(),
                "resolved_by_actor_id": actor_id,
                "resolution_rationale": rationale_value,
                "updated_at": _utc_now_iso(),
                "last_action_at": _utc_now_iso(),
                "last_action_type": "reject_proposal",
                "last_action_by_actor_id": actor_id,
            },
        )
        await self._record_queue_action(
            queue_item=queue_item,
            action_type="proposal_rejected_from_queue",
            actor_id=actor_id,
            rationale=rationale_value,
            payload={"review_notes": review_notes},
            before=before_snapshot,
            after=self._overlay_snapshot(updated_row),
        )
        return {"success": True, "review_action": "rejected", "proposal": proposal}

    async def _handle_assign_owner(
        self,
        *,
        queue_item: Mapping[str, Any],
        actor_id: int,
        owner_id: Optional[int],
        rationale: Optional[str],
    ) -> dict[str, Any]:
        record_id = _coerce_optional_int(queue_item.get("operational_record_id"))
        target_owner_id = _coerce_optional_int(owner_id)
        if not record_id or not target_owner_id:
            raise ValueError("Assign owner requires a record-backed item and owner_id")
        record = await self.records.get_record(record_id)
        if not record:
            raise ValueError("Operational record not found")
        await self.records.update_record(
            record_id=record_id,
            actor_id=actor_id,
            owner_id=target_owner_id,
            source_context_id=f"review-queue:assign-owner:{queue_item['id']}",
            event_summary=str(rationale or "").strip() or "Assigned owner from unified review queue.",
        )
        if str(record.get("record_type") or "") == "action" and str(record.get("state") or "") == ActionState.UNOWNED.value:
            await self.records.transition_record(
                record_id=record_id,
                new_state=ActionState.OPEN.value,
                actor_id=actor_id,
                source_context_id=f"review-queue:assign-owner:{queue_item['id']}",
                summary="Moved action from unowned to open after owner assignment.",
                payload={"review_item_id": queue_item["id"]},
            )
        await self.continuity.run_sweep(actor_id=actor_id, source_context_id=f"review-queue:sweep:{queue_item['id']}")
        state_row = await self._get_overlay_state(str(queue_item["id"]))
        before_snapshot = self._overlay_snapshot(state_row)
        updated_row = await self._upsert_overlay_state(
            queue_item=queue_item,
            values={
                "resolved_at": _utc_now_iso(),
                "resolved_by_actor_id": actor_id,
                "resolution_rationale": rationale,
                "updated_at": _utc_now_iso(),
                "last_action_at": _utc_now_iso(),
                "last_action_type": "assign_owner",
                "last_action_by_actor_id": actor_id,
                "deferred_until": None,
            },
        )
        await self._record_queue_action(
            queue_item=queue_item,
            action_type="owner_assigned_from_queue",
            actor_id=actor_id,
            rationale=rationale,
            payload={"owner_id": target_owner_id},
            before=before_snapshot,
            after=self._overlay_snapshot(updated_row),
        )
        refreshed_record = await self.records.get_record(record_id)
        return {"success": True, "record": refreshed_record}

    async def _record_queue_action(
        self,
        *,
        queue_item: Mapping[str, Any],
        action_type: str,
        actor_id: int,
        rationale: Optional[str],
        payload: Optional[Mapping[str, Any]],
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
    ) -> None:
        action_id = f"review_action_{uuid.uuid4().hex}"
        await self.db.execute(
            """
            INSERT INTO operational_review_queue_actions
            (action_id, review_item_id, item_type, action_type, actor_id, rationale, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
            action_id,
            queue_item["id"],
            queue_item["item_type"],
            action_type,
            actor_id,
            rationale,
            _json_dumps(payload),
            _utc_now_iso(),
            ),
            )
        await self.audit.record_mutation(
            entity_type="review_queue_item",
            entity_id=queue_item["id"],
            action=action_type,
            before=before,
            after=after,
            actor_id=actor_id,
            source_context_id=f"review-queue:item:{queue_item['id']}",
            metadata={
                "item_type": queue_item.get("item_type"),
                "operational_record_id": queue_item.get("operational_record_id"),
                "proposal_id": queue_item.get("proposal_id"),
            },
        )

    async def _get_overlay_state(self, review_item_id: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM operational_review_queue_state WHERE review_item_id = ?",
(review_item_id,),)
        return dict(row) if row else None

    async def _upsert_overlay_state(self, *, queue_item: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
        existing = await self._get_overlay_state(str(queue_item["id"]))
        now = _utc_now_iso()
        async with self.db.acquire() as conn:
            if existing:
                updates = []
                params = []
                for key, value in values.items():
                    updates.append(f"{key} = ?")
                    params.append(value)
                updates.append("updated_at = ?")
                params.append(now)
                params.append(queue_item["id"])
                await conn.execute(
                    f"UPDATE operational_review_queue_state SET {', '.join(updates)} WHERE review_item_id = ?",
                    tuple(params),
                )
            else:
                payload = {
                    "review_item_id": queue_item["id"],
                    "item_type": queue_item["item_type"],
                    "underlying_entity_type": queue_item.get("underlying_entity_type") or "review_item",
                    "underlying_entity_id": str(queue_item.get("underlying_entity_id") or queue_item["id"]),
                    "proposal_id": queue_item.get("proposal_id"),
                    "operational_record_id": queue_item.get("operational_record_id"),
                    "created_at": now,
                    "updated_at": now,
                }
                payload.update(values)
                columns = list(payload.keys())
                placeholders = ", ".join("?" for _ in columns)
                await conn.execute(
                    f"INSERT INTO operational_review_queue_state ({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(payload[column] for column in columns),
                )
            await conn.commit()
        return await self._get_overlay_state(str(queue_item["id"])) or {}

    def _overlay_snapshot(self, row: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        if not row:
            return None
        return {
            "deferred_until": row.get("deferred_until"),
            "deferral_rationale": row.get("deferral_rationale"),
            "defer_count": row.get("defer_count"),
            "severity_override": row.get("severity_override"),
            "escalation_destination": _json_loads(row.get("escalation_destination_json")),
            "resolved_at": row.get("resolved_at"),
            "last_action_type": row.get("last_action_type"),
        }

    def _proposal_item_id(self, *, item_type: str, proposal_id: int) -> str:
        return f"reviewq__{item_type}__proposal_{proposal_id}"

    def _record_item_id(self, item_type: str, record_id: int) -> str:
        return f"reviewq__{item_type}__record_{record_id}"

    def _sort_key(self, item: ReviewQueueItem) -> tuple[int, float, str]:
        detected_dt = _normalize_dt(item.detected_at) or _utc_now()
        urgency = _severity_rank(item.severity) * 100
        if item.item_type == ReviewItemType.ACTION_OVERDUE.value:
            urgency += 20
        if item.item_type == ReviewItemType.BLOCKER_ESCALATED_OR_STALE.value and item.severity == ReviewSeverity.CRITICAL.value:
            urgency += 30
        age_seconds = (_utc_now() - detected_dt).total_seconds()
        return (-urgency, -age_seconds, item.id)

    def _scope_match(self, item: ReviewQueueItem, *, scope: str, current_actor_id: Optional[int]) -> bool:
        if scope == "all":
            return True
        if current_actor_id is None:
            return scope == "all"
        is_mine = item.owner_id == current_actor_id or item.created_by_actor_id == current_actor_id
        if scope == "mine":
            return is_mine
        if scope == "team":
            return not is_mine
        return True

    def _bump_severity(self, severity: str) -> str:
        normalized = str(severity or "").strip().lower()
        if normalized == ReviewSeverity.LOW.value:
            return ReviewSeverity.MEDIUM.value
        if normalized == ReviewSeverity.MEDIUM.value:
            return ReviewSeverity.HIGH.value
        return ReviewSeverity.CRITICAL.value

    def _must_surface_in_weekly_review(self, item: ReviewQueueItem) -> bool:
        if item.item_type == ReviewItemType.DECISION_UNRESOLVED.value:
            return _severity_rank(item.severity) >= _severity_rank(self.policy.weekly_unresolved_min_severity)
        return _severity_rank(item.severity) >= _severity_rank(ReviewSeverity.HIGH.value)

    def _record_source_payload(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["record_id"]),
            "title": row.get("title"),
            "summary": row.get("summary"),
            "record_type": row.get("record_type"),
            "state": row.get("record_state"),
            "owner_id": row.get("owner_id"),
            "workspace_scope": row.get("workspace_scope"),
            "project_scope": row.get("project_scope"),
            "due_at": row.get("due_at"),
            "review_at": row.get("review_at"),
            "stale_after_at": row.get("stale_after_at"),
        }

    def _proposal_row_to_public_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "proposal_id": row.get("proposal_id"),
            "record_type": row.get("record_type"),
            "status": row.get("status"),
            "title": row.get("title"),
            "summary": row.get("summary"),
            "effective_confidence": row.get("effective_confidence"),
            "record_confidence": row.get("record_confidence"),
            "extracted_fields": _json_loads(row.get("extracted_fields_json")),
            "source_entity_type": row.get("source_entity_type"),
            "source_entity_id": row.get("source_entity_id"),
            "source_context_id": row.get("source_context_id"),
            "source_details": _json_loads(row.get("source_details_json")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }