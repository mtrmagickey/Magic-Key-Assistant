"""Artifacts router — Actions, Leads, Meetings, Analytics, provenance, and extraction review."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.actors import ActorContext
from core.services import ExtractionProposalService, OperationalRecordService, ProvenanceService
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from admin.dependencies import get_current_actor, get_db, require_member, templates

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["artifacts"], dependencies=[Depends(require_member)])


# ── Pydantic models ──────────────────────────────────────────────────────────

class ActionCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: Optional[str] = "medium"
    assigned_to_username: Optional[str] = None
    due_date: Optional[str] = None
    project_id: Optional[int] = None
    tags: Optional[str] = None

class ActionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assigned_to_username: Optional[str] = None
    due_date: Optional[str] = None
    tags: Optional[str] = None

class LeadCreate(BaseModel):
    name: str
    source: Optional[str] = None
    contact_name: Optional[str] = None
    contact_info: Optional[str] = None
    value_estimate: Optional[str] = None
    notes: Optional[str] = None
    next_action: Optional[str] = None
    next_action_date: Optional[str] = None
    owner_username: Optional[str] = None

class LeadStageAdvance(BaseModel):
    new_stage: str
    note: Optional[str] = None

class LeadTouchpoint(BaseModel):
    activity_type: str = "follow_up"
    summary: str

class DecisionCreate(BaseModel):
    title: str
    decision: str
    rationale: Optional[str] = None
    decided_by: Optional[str] = None
    category: Optional[str] = None
    impact: Optional[str] = None
    related_project_id: Optional[int] = None

class MeetingCreate(BaseModel):
    title: str
    summary: Optional[str] = None
    raw_text: Optional[str] = None
    attendees: Optional[str] = None

class MeetingLink(BaseModel):
    target_id: int


class ProvenanceEntityRef(BaseModel):
    entity_type: str
    entity_id: str
    label: Optional[str] = None
    summary: Optional[str] = None
    url: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ProvenanceLinkCreate(BaseModel):
    source: ProvenanceEntityRef
    target: ProvenanceEntityRef
    relationship: str
    explanation: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ExtractionProposalReviewRequest(BaseModel):
    action: str
    final_fields: Optional[dict[str, Any]] = None
    merge_record_id: Optional[int] = None
    review_notes: Optional[str] = None
    reason: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor_display_name(actor: ActorContext) -> str:
    return str(actor.display_name or actor.username or actor.external_ref)


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _provenance_ref_details(ref: ProvenanceEntityRef) -> dict[str, Any]:
    payload = _model_dump(ref)
    payload.pop("entity_type", None)
    payload.pop("entity_id", None)
    return payload


async def _persist_actor_id(db, actor: ActorContext) -> int:
    if int(actor.actor_id or 0) > 0:
        return int(actor.actor_id)
    service = OperationalRecordService(db)
    persisted = await service.ensure_actor(
        actor_kind=actor.actor_kind,
        external_ref=actor.external_ref,
        display_name=actor.display_name or actor.username,
        stable_id=actor.stable_id,
    )
    return int(persisted["id"])


def _action_state_from_row(row: dict, *, owner_actor_id: Optional[int] = None) -> str:
    status = str(row.get("status") or "todo").strip().lower()
    if status == "done":
        return "done"
    if status == "cancelled":
        return "canceled"
    if status == "in_progress":
        return "in_progress"
    if status == "blocked":
        return "blocked"
    if not owner_actor_id:
        return "unowned"
    return "open"


def _action_payload(row: dict) -> dict:
    return {
        "legacy_table": "tasks",
        "legacy_id": row.get("id"),
        "priority": row.get("priority"),
        "assigned_to_username": row.get("assigned_to_username"),
        "project_id": row.get("project_id"),
        "tags": row.get("tags"),
        "meeting_id": row.get("meeting_id") or row.get("source_meeting_id"),
        "created_by_user_id": row.get("created_by_user_id"),
        "created_by_username": row.get("created_by_username"),
    }


async def _ensure_action_record(db, actor_id: int, row: dict):
    service = OperationalRecordService(db)
    record = await service.get_record_by_legacy_link(legacy_table="tasks", legacy_id=int(row["id"]))
    if record:
        meeting_id = row.get("meeting_id") or row.get("source_meeting_id")
        if meeting_id:
            provenance = ProvenanceService(db)
            await provenance.create_edge(
                source_entity_type="meeting",
            source_entity_id=meeting_id,
                target_entity_type="operational_record",
                target_entity_id=record["id"],
                relationship="origin",
                actor_id=actor_id,
                explanation="Action linked to meeting origin.",
                source_context_id=f"legacy:tasks:{row['id']}",
            )
        return record
    owner_actor_id = await _resolve_owner_actor_id(db, row.get("assigned_to_username"))
    record = await service.create_record(
        record_type="action",
        title=row["title"],
        summary=row.get("description"),
        state=_action_state_from_row(row, owner_actor_id=owner_actor_id),
        owner_id=owner_actor_id,
        created_by_actor_id=actor_id,
        updated_by_actor_id=actor_id,
        source_context_id=f"legacy:tasks:{row['id']}",
        workspace_scope="default",
        project_scope=str(row["project_id"]) if row.get("project_id") is not None else None,
        due_at=row.get("due_date"),
        stale_after_at=None,
        notes=row.get("notes"),
        canonical_payload=_action_payload(row),
    )
    await service.link_legacy_record(record_id=int(record["id"]), legacy_table="tasks", legacy_id=int(row["id"]))
    meeting_id = row.get("meeting_id") or row.get("source_meeting_id")
    if meeting_id:
        provenance = ProvenanceService(db)
        await provenance.create_edge(
            source_entity_type="meeting",
            source_entity_id=meeting_id,
            target_entity_type="operational_record",
            target_entity_id=record["id"],
            relationship="origin",
            actor_id=actor_id,
            explanation="Action linked to meeting origin.",
            source_context_id=f"legacy:tasks:{row['id']}",
        )
    return record


async def _resolve_owner_actor_id(db, assigned_to_username: Optional[str]) -> Optional[int]:
    if not assigned_to_username:
        return None
    normalized_username = str(assigned_to_username).strip().lower()
    if not normalized_username:
        return None
    row = await db.fetchone("""
SELECT wa.actor_id
FROM web_accounts wa
WHERE wa.username_normalized = ? AND wa.is_active = 1
""",
(normalized_username,),)
    return int(row[0]) if row else None


def _decision_payload(row: dict) -> dict:
    return {
        "legacy_table": "decisions",
        "legacy_id": row.get("id"),
        "decision": row.get("decision"),
        "decided_by": row.get("decided_by"),
        "category": row.get("category"),
        "impact": row.get("impact"),
        "related_project_id": row.get("related_project_id"),
        "meeting_id": row.get("meeting_id") or row.get("source_meeting_id"),
    }


async def _ensure_decision_record(db, actor_id: int, row: dict):
    service = OperationalRecordService(db)
    record = await service.get_record_by_legacy_link(legacy_table="decisions", legacy_id=int(row["id"]))
    if record:
        meeting_id = row.get("meeting_id") or row.get("source_meeting_id")
        if meeting_id:
            provenance = ProvenanceService(db)
            await provenance.create_edge(
                source_entity_type="meeting",
            source_entity_id=meeting_id,
                target_entity_type="operational_record",
                target_entity_id=record["id"],
                relationship="origin",
                actor_id=actor_id,
                explanation="Decision linked to meeting origin.",
                source_context_id=f"legacy:decisions:{row['id']}",
            )
        return record
    record = await service.create_record(
        record_type="decision",
        title=row["title"],
        summary=row.get("description") or row.get("context"),
        state="accepted" if not row.get("superseded_by_decision_id") else "superseded",
        created_by_actor_id=actor_id,
        updated_by_actor_id=actor_id,
        source_context_id=f"legacy:decisions:{row['id']}",
        workspace_scope="default",
        project_scope=str(row["related_project_id"]) if row.get("related_project_id") is not None else None,
        review_at=row.get("reviewed_at"),
        rationale=row.get("rationale"),
        canonical_payload=_decision_payload(row),
    )
    await service.link_legacy_record(record_id=int(record["id"]), legacy_table="decisions", legacy_id=int(row["id"]))
    meeting_id = row.get("meeting_id") or row.get("source_meeting_id")
    if meeting_id:
        provenance = ProvenanceService(db)
        await provenance.create_edge(
            source_entity_type="meeting",
            source_entity_id=meeting_id,
            target_entity_type="operational_record",
            target_entity_id=record["id"],
            relationship="origin",
            actor_id=actor_id,
            explanation="Decision linked to meeting origin.",
            source_context_id=f"legacy:decisions:{row['id']}",
        )
    return record


async def _get_action_record_for_provenance(db, actor_id: int, action_id: int):
    service = OperationalRecordService(db)
    record = await service.get_record_by_legacy_link(legacy_table="tasks", legacy_id=action_id)
    if record:
        return record
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (action_id,))
    if not row:
        return None
    return await _ensure_action_record(db, actor_id, dict(row))


async def _get_decision_record_for_provenance(db, actor_id: int, decision_id: int):
    service = OperationalRecordService(db)
    record = await service.get_record_by_legacy_link(legacy_table="decisions", legacy_id=decision_id)
    if record:
        return record
    row = await db.fetchone("SELECT * FROM decisions WHERE id = ?", (decision_id,))
    if not row:
        return None
    return await _ensure_decision_record(db, actor_id, dict(row))


# =============================================================================
# Page routes
# =============================================================================

@router.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request):
    return templates.TemplateResponse(request, "actions.html", {"active_page": "tasks"})

@router.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request):
    return templates.TemplateResponse(request, "leads.html", {"active_page": "leads"})

# Decision Registry UI removed; API endpoints kept for backward-compat.
# @router.get("/decisions", response_class=HTMLResponse)
# async def decisions_page(request: Request):
#     return templates.TemplateResponse("decisions.html", {"request": request, "active_page": "decisions"})

@router.get("/meetings", response_class=HTMLResponse)
async def meetings_page(request: Request):
    return templates.TemplateResponse(request, "meetings.html", {"active_page": "meetings"})

@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return templates.TemplateResponse(request, "analytics.html", {"active_page": "analytics"})


# =============================================================================
# Actions / Tasks
# =============================================================================

@router.get("/api/v1/actions")
async def api_list_actions(
    status: Optional[str] = None, priority: Optional[str] = None,
    assigned_to: Optional[str] = None, search: Optional[str] = None,
    sort_by: str = "created_at", sort_dir: str = "desc",
    page: int = 1, per_page: int = 50, db=Depends(get_db),
):
    try:
        async with db.acquire() as conn:
            where, params = [], []
            if status:
                where.append("status = ?")
                params.append(status)
            if priority:
                where.append("priority = ?")
                params.append(priority)
            if assigned_to:
                where.append("assigned_to_username = ?")
                params.append(assigned_to)
            if search:
                where.append("(title LIKE ? OR description LIKE ?)")
                params.extend([f"%{search}%"] * 2)
            w = f"WHERE {' AND '.join(where)}" if where else ""
            valid_cols = ["id", "title", "status", "priority", "due_date", "created_at", "completed_at"]
            col = sort_by if sort_by in valid_cols else "created_at"
            d = "DESC" if sort_dir.lower() == "desc" else "ASC"
            async with conn.execute(f"SELECT COUNT(*) FROM tasks {w}", params) as cur:
                total = (await cur.fetchone())[0]
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            async with conn.execute(f"SELECT * FROM tasks {w} ORDER BY {col} {d} LIMIT ? OFFSET ?", params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return {"success": True, "actions": rows, "total": total, "page": page,
                    "per_page": per_page, "total_pages": max(1, (total + per_page - 1) // per_page)}
    except Exception as e:
        logger.error(f"list actions: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/actions")
async def api_create_action(data: ActionCreate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        now = _utc_now_iso()
        actor_id = await _persist_actor_id(db, current_actor)
        action_id = None
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO tasks (
                       title, description, status, priority, assigned_to_username,
                       due_date, tags, created_by_user_id, created_by_username,
                       created_at, updated_at
                   )
                   VALUES (?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.title,
                    data.description,
                    data.priority,
                    data.assigned_to_username,
                    data.due_date,
                    data.tags,
                    current_actor.account_id,
                    _actor_display_name(current_actor),
                    now,
                    now,
                ),
            ) as cur:
                action_id = cur.lastrowid
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (action_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        record = await _ensure_action_record(db, actor_id, dict(row))
        provenance = ProvenanceService(db)
        await provenance.record_manual_origin(
            record_id=int(record["id"]),
            actor_id=actor_id,
            actor_label=_actor_display_name(current_actor),
            surface="web",
            source_context_id=f"legacy:tasks:{action_id}",
        )
        return {"success": True, "id": action_id}
    except Exception as e:
        if 'action_id' in locals() and action_id:
            try:
                async with db.acquire() as conn:
                    await conn.execute("DELETE FROM tasks WHERE id = ?", (action_id,))
                    await conn.commit()
            except Exception:
                logger.warning("Failed to roll back orphaned legacy action %s", action_id, exc_info=True)
        logger.error(f"create action: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/actions/stats")
async def api_action_stats(db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            stats = {}
            async with conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status") as cur:
                stats["by_status"] = {r[0]: r[1] for r in await cur.fetchall()}
            async with conn.execute("SELECT priority, COUNT(*) FROM tasks WHERE status NOT IN ('done','cancelled') GROUP BY priority") as cur:
                stats["by_priority"] = {r[0]: r[1] for r in await cur.fetchall()}
            async with conn.execute("SELECT COUNT(*) FROM tasks WHERE due_date < date('now') AND status NOT IN ('done','cancelled')") as cur:
                stats["overdue"] = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*) FROM tasks") as cur:
                stats["total"] = (await cur.fetchone())[0]
            return {"success": True, "stats": stats}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/actions/{action_id}")
async def api_get_action(action_id: int, db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (action_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return {"success": False, "error": "Not found"}
            return {"success": True, "action": dict(row)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.patch("/api/v1/actions/{action_id}")
async def api_update_action(action_id: int, data: ActionUpdate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        updates, params = [], []
        for field in ("title", "description", "status", "priority", "assigned_to_username", "due_date", "tags"):
            val = getattr(data, field, None)
            if val is not None:
                updates.append(f"{field} = ?")
                params.append(val)
        if not updates:
            return {"success": False, "error": "Nothing to update"}
        if data.status == "done":
            updates.append("completed_at = ?")
            params.append(_utc_now_iso())
        updates.append("updated_at = ?")
        params.append(_utc_now_iso())
        params.append(action_id)
        async with db.acquire() as conn:
            await conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (action_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        if not row:
            return {"success": False, "error": "Not found"}

        row_dict = dict(row)
        service = OperationalRecordService(db)
        record = await _ensure_action_record(db, actor_id, row_dict)
        owner_actor_id = await _resolve_owner_actor_id(db, row_dict.get("assigned_to_username"))
        await service.update_record(
            record_id=int(record["id"]),
            actor_id=actor_id,
            title=row_dict.get("title"),
            summary=row_dict.get("description"),
            owner_id=owner_actor_id,
            due_at=row_dict.get("due_date"),
            notes=row_dict.get("notes"),
            canonical_payload=_action_payload(row_dict),
            source_context_id=f"legacy:tasks:{action_id}",
            event_summary="Updated action metadata from web console.",
        )
        target_state = _action_state_from_row(row_dict, owner_actor_id=owner_actor_id)
        current_state = str(record.get("state") or "")
        if current_state != target_state:
            await service.transition_record(
                record_id=int(record["id"]),
                new_state=target_state,
                actor_id=actor_id,
                source_context_id=f"legacy:tasks:{action_id}",
                summary=f"Action state updated by {_actor_display_name(current_actor)}.",
                payload=_action_payload(row_dict),
            )
        return {"success": True, "id": action_id}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/actions/{action_id}/done")
async def api_mark_action_done(action_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        now = _utc_now_iso()
        async with db.acquire() as conn:
            await conn.execute("UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?", (now, now, action_id))
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (action_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        if row:
            row_dict = dict(row)
            service = OperationalRecordService(db)
            record = await _ensure_action_record(db, actor_id, row_dict)
            owner_actor_id = await _resolve_owner_actor_id(db, row_dict.get("assigned_to_username"))
            await service.update_record(
                record_id=int(record["id"]),
                actor_id=actor_id,
                title=row_dict.get("title"),
                summary=row_dict.get("description"),
                owner_id=owner_actor_id,
                due_at=row_dict.get("due_date"),
                notes=row_dict.get("notes"),
                canonical_payload=_action_payload(row_dict),
                source_context_id=f"legacy:tasks:{action_id}",
                event_summary="Marked action done from web console.",
            )
            if str(record.get("state")) != "done":
                await service.transition_record(
                    record_id=int(record["id"]),
                    new_state="done",
                    actor_id=actor_id,
                    source_context_id=f"legacy:tasks:{action_id}",
                    summary=f"Action marked done by {_actor_display_name(current_actor)}.",
                    payload=_action_payload(row_dict),
                )
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/actions/{action_id}/cancel")
async def api_cancel_action(action_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        now = _utc_now_iso()
        async with db.acquire() as conn:
            await conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?", (now, action_id))
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (action_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        if row:
            row_dict = dict(row)
            service = OperationalRecordService(db)
            record = await _ensure_action_record(db, actor_id, row_dict)
            owner_actor_id = await _resolve_owner_actor_id(db, row_dict.get("assigned_to_username"))
            await service.update_record(
                record_id=int(record["id"]),
                actor_id=actor_id,
                title=row_dict.get("title"),
                summary=row_dict.get("description"),
                owner_id=owner_actor_id,
                due_at=row_dict.get("due_date"),
                notes=row_dict.get("notes"),
                canonical_payload=_action_payload(row_dict),
                source_context_id=f"legacy:tasks:{action_id}",
                event_summary="Canceled action from web console.",
            )
            if str(record.get("state")) != "canceled":
                await service.transition_record(
                    record_id=int(record["id"]),
                    new_state="canceled",
                    actor_id=actor_id,
                    source_context_id=f"legacy:tasks:{action_id}",
                    summary=f"Action canceled by {_actor_display_name(current_actor)}.",
                    payload=_action_payload(row_dict),
                )
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Leads
# =============================================================================

@router.get("/api/v1/leads")
async def api_list_leads(
    stage: Optional[str] = None, search: Optional[str] = None,
    sort_by: str = "updated_at", sort_dir: str = "desc",
    page: int = 1, per_page: int = 50, db=Depends(get_db),
):
    try:
        async with db.acquire() as conn:
            where, params = [], []
            if stage:
                where.append("status = ?")
                params.append(stage)
            if search:
                where.append("(name LIKE ? OR contact_name LIKE ? OR notes LIKE ?)")
                params.extend([f"%{search}%"] * 3)
            w = f"WHERE {' AND '.join(where)}" if where else ""
            col = sort_by if sort_by in ("id", "name", "status", "updated_at", "created_at", "last_activity", "value_estimate") else "updated_at"
            d = "DESC" if sort_dir.lower() == "desc" else "ASC"
            async with conn.execute(f"SELECT COUNT(*) FROM leads {w}", params) as cur:
                total = (await cur.fetchone())[0]
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            async with conn.execute(f"SELECT * FROM leads {w} ORDER BY {col} {d} LIMIT ? OFFSET ?", params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return {"success": True, "leads": rows, "total": total, "page": page,
                    "total_pages": max(1, (total + per_page - 1) // per_page)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/leads/pipeline")
async def api_leads_pipeline(db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status") as cur:
                return {"success": True, "pipeline": {r[0]: r[1] for r in await cur.fetchall()}}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/leads")
async def api_create_lead(data: LeadCreate, db=Depends(get_db)):
    try:
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO leads (name, source, status, contact_name, contact_info,
                   value_estimate, notes, next_action, next_action_date, owner_username,
                   created_at, updated_at, last_activity) VALUES (?,?,'cold',?,?,?,?,?,?,?,?,?,?)""",
                (data.name, data.source, data.contact_name, data.contact_info,
                 data.value_estimate, data.notes, data.next_action, data.next_action_date,
                 data.owner_username, now, now, now),
            ) as cur:
                lead_id = cur.lastrowid
            await conn.execute("INSERT INTO lead_activity (lead_id, activity_type, summary) VALUES (?,?,?)",
                               (lead_id, "creation", f"Lead created from {data.source or 'manual'}"))
            await conn.commit()
        return {"success": True, "id": lead_id}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/leads/{lead_id}")
async def api_get_lead(lead_id: int, db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return {"success": False, "error": "Not found"}
            lead = dict(row)
            async with conn.execute("SELECT * FROM lead_activity WHERE lead_id = ? ORDER BY created_at DESC LIMIT 20", (lead_id,)) as cur:
                lead["activities"] = [dict(r) for r in await cur.fetchall()]
            return {"success": True, "lead": lead}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/leads/{lead_id}/advance")
async def api_advance_lead(lead_id: int, data: LeadStageAdvance, db=Depends(get_db)):
    valid = {"cold", "warm", "hot", "proposal", "won", "lost"}
    if data.new_stage not in valid:
        return {"success": False, "error": f"Invalid stage. Must be one of: {valid}"}
    try:
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        async with db.acquire() as conn:
            async with conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return {"success": False, "error": "Not found"}
            old = row[0]
            await conn.execute("UPDATE leads SET status=?, updated_at=?, last_activity=? WHERE id=?",
                               (data.new_stage, now, now, lead_id))
            summary = f"Stage: {old} → {data.new_stage}" + (f" — {data.note}" if data.note else "")
            await conn.execute("INSERT INTO lead_activity (lead_id, activity_type, summary) VALUES (?,?,?)",
                               (lead_id, "status_change", summary))
            await conn.commit()
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/leads/{lead_id}/touchpoint")
async def api_lead_touchpoint(lead_id: int, data: LeadTouchpoint, db=Depends(get_db)):
    try:
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        async with db.acquire() as conn:
            await conn.execute("UPDATE leads SET last_activity=?, updated_at=? WHERE id=?", (now, now, lead_id))
            await conn.execute("INSERT INTO lead_activity (lead_id, activity_type, summary) VALUES (?,?,?)",
                               (lead_id, data.activity_type, data.summary))
            await conn.commit()
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Decisions
# =============================================================================

@router.get("/api/v1/decisions")
async def api_list_decisions(
    search: Optional[str] = None, category: Optional[str] = None,
    sort_by: str = "created_at", sort_dir: str = "desc",
    page: int = 1, per_page: int = 50, db=Depends(get_db),
):
    try:
        async with db.acquire() as conn:
            where, params = [], []
            if search:
                where.append("(title LIKE ? OR decision LIKE ? OR rationale LIKE ?)")
                params.extend([f"%{search}%"] * 3)
            if category:
                where.append("category = ?")
                params.append(category)
            w = f"WHERE {' AND '.join(where)}" if where else ""
            col = sort_by if sort_by in ("id", "title", "category", "created_at", "decided_by") else "created_at"
            d = "DESC" if sort_dir.lower() == "desc" else "ASC"
            async with conn.execute(f"SELECT COUNT(*) FROM decisions {w}", params) as cur:
                total = (await cur.fetchone())[0]
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            async with conn.execute(f"SELECT * FROM decisions {w} ORDER BY {col} {d} LIMIT ? OFFSET ?", params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return {"success": True, "decisions": rows, "total": total, "page": page,
                    "total_pages": max(1, (total + per_page - 1) // per_page)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/decisions")
async def api_create_decision(data: DecisionCreate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        now = _utc_now_iso()
        actor_id = await _persist_actor_id(db, current_actor)
        decision_id = None
        decided_by = data.decided_by or _actor_display_name(current_actor)
        async with db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO decisions (title, decision, rationale, decided_by,
                   category, impact, related_project_id, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (data.title, data.decision, data.rationale, decided_by,
                 data.category, data.impact, data.related_project_id, now),
            ) as cur:
                decision_id = cur.lastrowid
            async with conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        record = await _ensure_decision_record(db, actor_id, dict(row))
        provenance = ProvenanceService(db)
        await provenance.record_manual_origin(
            record_id=int(record["id"]),
            actor_id=actor_id,
            actor_label=_actor_display_name(current_actor),
            surface="web",
            source_context_id=f"legacy:decisions:{decision_id}",
        )
        return {"success": True, "id": decision_id}
    except Exception:
        if 'decision_id' in locals() and decision_id:
            try:
                async with db.acquire() as conn:
                    await conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
                    await conn.commit()
            except Exception:
                logger.warning("Failed to roll back orphaned legacy decision %s", decision_id, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/decisions/categories")
async def api_decision_categories(db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT DISTINCT category FROM decisions WHERE category IS NOT NULL ORDER BY category") as cur:
                return {"success": True, "categories": [r[0] for r in await cur.fetchall()]}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/decisions/{decision_id}")
async def api_get_decision(decision_id: int, db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return {"success": False, "error": "Not found"}
            return {"success": True, "decision": dict(row)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Meetings
# =============================================================================

@router.get("/api/v1/meetings")
async def api_list_meetings(
    search: Optional[str] = None, page: int = 1, per_page: int = 50,
    db=Depends(get_db),
):
    try:
        async with db.acquire() as conn:
            where, params = [], []
            if search:
                where.append("(title LIKE ? OR summary LIKE ?)")
                params.extend([f"%{search}%"] * 2)
            w = f"WHERE {' AND '.join(where)}" if where else ""
            async with conn.execute(f"SELECT COUNT(*) FROM meeting_notes {w}", params) as cur:
                total = (await cur.fetchone())[0]
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            async with conn.execute(f"SELECT * FROM meeting_notes {w} ORDER BY created_at DESC LIMIT ? OFFSET ?", params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return {"success": True, "meetings": rows, "total": total, "page": page,
                    "total_pages": max(1, (total + per_page - 1) // per_page)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/meetings")
async def api_create_meeting(data: MeetingCreate, db=Depends(get_db)):
    try:
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        async with db.acquire() as conn:
            async with conn.execute(
                "INSERT INTO meeting_notes (title, summary, raw_text, attendees, created_at) VALUES (?,?,?,?,?)",
                (data.title, data.summary, data.raw_text, data.attendees, now),
            ) as cur:
                mid = cur.lastrowid
            await conn.commit()
        return {"success": True, "id": mid}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/meetings/{meeting_id}")
async def api_get_meeting(meeting_id: int, db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            async with conn.execute("SELECT * FROM meeting_notes WHERE id = ?", (meeting_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return {"success": False, "error": "Not found"}
            meeting = dict(row)
            async with conn.execute("SELECT * FROM tasks WHERE source_meeting_id = ?", (meeting_id,)) as cur:
                meeting["linked_actions"] = [dict(r) for r in await cur.fetchall()]
            # linked_decisions removed from UI
            meeting["linked_decisions"] = []
            return {"success": True, "meeting": meeting}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/meetings/{meeting_id}/link-action")
async def api_link_meeting_action(meeting_id: int, data: MeetingLink, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        async with db.acquire() as conn:
            await conn.execute("UPDATE tasks SET source_meeting_id = ? WHERE id = ?", (meeting_id, data.target_id))
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (data.target_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        if row:
            await _ensure_action_record(db, actor_id, dict(row))
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/meetings/{meeting_id}/link-decision")
async def api_link_meeting_decision(meeting_id: int, data: MeetingLink, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        async with db.acquire() as conn:
            await conn.execute("UPDATE decisions SET source_meeting_id = ? WHERE id = ?", (meeting_id, data.target_id))
            async with conn.execute("SELECT * FROM decisions WHERE id = ?", (data.target_id,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
        if row:
            await _ensure_decision_record(db, actor_id, dict(row))
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/provenance-links")
async def api_create_provenance_link(
    data: ProvenanceLinkCreate,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        provenance = ProvenanceService(db)
        edge = await provenance.create_edge(
            source_entity_type=data.source.entity_type,
            source_entity_id=data.source.entity_id,
            target_entity_type=data.target.entity_type,
            target_entity_id=data.target.entity_id,
            relationship=data.relationship,
            actor_id=actor_id,
            explanation=data.explanation,
            metadata=data.metadata,
            source_details=_provenance_ref_details(data.source),
            target_details=_provenance_ref_details(data.target),
        )
        return {"success": True, "edge": edge}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/provenance-links")
async def api_list_provenance_links(
    entity_type: str,
    entity_id: str,
    direction: str = "both",
    relationship: Optional[str] = None,
    db=Depends(get_db),
):
    try:
        provenance = ProvenanceService(db)
        edges = await provenance.list_edges(
            entity_type=entity_type,
            entity_id=entity_id,
            direction=direction,
            relationship=relationship,
        )
        return {"success": True, "edges": edges, "count": len(edges)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/operational-records/{record_id}/provenance")
async def api_get_record_provenance(record_id: int, db=Depends(get_db)):
    try:
        provenance = ProvenanceService(db)
        explanation = await provenance.explain_record_origin(record_id)
        return {"success": True, **explanation}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/actions/{action_id}/provenance")
async def api_get_action_provenance(action_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        record = await _get_action_record_for_provenance(db, actor_id, action_id)
        if not record:
            return {"success": False, "error": "Not found"}
        provenance = ProvenanceService(db)
        explanation = await provenance.explain_record_origin(int(record["id"]))
        return {"success": True, "action_id": action_id, **explanation}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/decisions/{decision_id}/provenance")
async def api_get_decision_provenance(decision_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        record = await _get_decision_record_for_provenance(db, actor_id, decision_id)
        if not record:
            return {"success": False, "error": "Not found"}
        provenance = ProvenanceService(db)
        explanation = await provenance.explain_record_origin(int(record["id"]))
        return {"success": True, "decision_id": decision_id, **explanation}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/extraction-proposals/review")
async def api_list_review_proposals(
    max_effective_confidence: float = 0.65,
    limit: int = 50,
    record_type: Optional[str] = None,
    db=Depends(get_db),
):
    try:
        service = ExtractionProposalService(db)
        proposals = await service.list_proposals(
            status="pending",
            record_type=record_type,
            max_effective_confidence=max_effective_confidence,
            limit=limit,
        )
        return {
            "success": True,
            "proposals": proposals,
            "count": len(proposals),
            "max_effective_confidence": max_effective_confidence,
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/extraction-proposals/{proposal_id}")
async def api_get_extraction_proposal(proposal_id: int, db=Depends(get_db)):
    try:
        service = ExtractionProposalService(db)
        proposal = await service.get_proposal(proposal_id)
        if not proposal:
            return {"success": False, "error": "Not found"}
        events = await service.list_events(proposal_id)
        return {"success": True, "proposal": proposal, "events": events}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/extraction-proposals/{proposal_id}/review")
async def api_review_extraction_proposal(
    proposal_id: int,
    data: ExtractionProposalReviewRequest,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ExtractionProposalService(db)
        action = str(data.action or "").strip().lower()
        if action in {"accept", "edit_accept", "accept_with_edits"}:
            result = await service.accept_proposal(
                proposal_id=proposal_id,
                actor_id=actor_id,
                final_fields=data.final_fields,
                review_notes=data.review_notes,
            )
            return {"success": True, "review_action": "accepted", **result}
        if action == "merge":
            result = await service.accept_proposal(
                proposal_id=proposal_id,
                actor_id=actor_id,
                final_fields=data.final_fields,
                merge_record_id=data.merge_record_id,
                review_notes=data.review_notes,
            )
            return {"success": True, "review_action": "merged", **result}
        if action == "reject":
            proposal = await service.reject_proposal(
                proposal_id=proposal_id,
                actor_id=actor_id,
                reason=data.reason or "Rejected during human review.",
                review_notes=data.review_notes,
            )
            return {"success": True, "review_action": "rejected", "proposal": proposal}
        return {"success": False, "error": "Unknown review action"}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Analytics
# =============================================================================

@router.get("/api/v1/analytics/overview")
async def api_analytics_overview(db=Depends(get_db)):
    try:
        async with db.acquire() as conn:
            data = {}
            try:
                async with conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status") as cur:
                    data["tasks_by_status"] = {r[0]: r[1] for r in await cur.fetchall()}
            except Exception:
                data["tasks_by_status"] = {}
            try:
                async with conn.execute("SELECT COUNT(*) FROM tasks WHERE due_date < date('now') AND status NOT IN ('done','cancelled')") as cur:
                    data["tasks_overdue"] = (await cur.fetchone())[0]
            except Exception:
                data["tasks_overdue"] = 0
            try:
                async with conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status") as cur:
                    data["leads_pipeline"] = {r[0]: r[1] for r in await cur.fetchall()}
            except Exception:
                data["leads_pipeline"] = {}
            try:
                async with conn.execute("SELECT COUNT(*) FROM decisions") as cur:
                    data["decisions_total"] = (await cur.fetchone())[0]
            except Exception:
                data["decisions_total"] = 0
            try:
                async with conn.execute("SELECT COUNT(*) FROM meeting_notes") as cur:
                    data["meetings_total"] = (await cur.fetchone())[0]
            except Exception:
                data["meetings_total"] = 0
            try:
                async with conn.execute("SELECT status, COUNT(*) FROM knowledge_gaps GROUP BY status") as cur:
                    data["gaps_by_status"] = {r[0]: r[1] for r in await cur.fetchall()}
            except Exception:
                data["gaps_by_status"] = {}
            try:
                async with conn.execute(
                    "SELECT title, updated_at FROM tasks WHERE status='done' ORDER BY updated_at DESC LIMIT 10"
                ) as cur:
                    data["recent_done"] = [
                        {"title": r[0], "done_date": r[1]} for r in await cur.fetchall()
                    ]
            except Exception:
                data["recent_done"] = []
            try:
                async with conn.execute(
                    "SELECT job_name, status, COUNT(*) FROM job_runs WHERE run_date >= date('now','-7 days') GROUP BY job_name, status"
                ) as cur:
                    jobs = {}
                    for r in await cur.fetchall():
                        jobs.setdefault(r[0], {})[r[1]] = r[2]
                    data["recent_jobs"] = jobs
            except Exception:
                data["recent_jobs"] = {}
            try:
                async with conn.execute(
                    "SELECT command_name, COUNT(*) FROM bot_command_usage WHERE used_at >= datetime('now','-7 days') GROUP BY command_name ORDER BY COUNT(*) DESC LIMIT 10"
                ) as cur:
                    data["top_commands"] = {r[0]: r[1] for r in await cur.fetchall()}
            except Exception:
                data["top_commands"] = {}
            return {"success": True, "analytics": data}
    except Exception as e:
        logger.error(f"analytics overview: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}

