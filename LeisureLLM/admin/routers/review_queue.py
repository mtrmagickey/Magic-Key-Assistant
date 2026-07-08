"""Unified review queue API for proposals and operational continuity work."""

from __future__ import annotations

from typing import Any, Optional

from core.actors import ActorContext
from core.services import OperationalRecordService, ReviewQueueService
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from admin.dependencies import get_current_actor, get_db, require_member

router = APIRouter(tags=["review_queue"], dependencies=[Depends(require_member)])


class ReviewQueueActionRequest(BaseModel):
    action: str
    rationale: Optional[str] = None
    final_fields: Optional[dict[str, Any]] = None
    merge_record_id: Optional[int] = None
    owner_id: Optional[int] = None
    defer_until: Optional[str] = None
    new_state: Optional[str] = None
    severity: Optional[str] = None
    escalation_destination: Optional[dict[str, Any]] = None
    review_notes: Optional[str] = None


class ReviewQueueBulkActionRequest(BaseModel):
    item_ids: list[str] = Field(default_factory=list)
    action: str
    rationale: Optional[str] = None
    final_fields: Optional[dict[str, Any]] = None
    merge_record_id: Optional[int] = None
    owner_id: Optional[int] = None
    defer_until: Optional[str] = None
    new_state: Optional[str] = None
    severity: Optional[str] = None
    escalation_destination: Optional[dict[str, Any]] = None
    review_notes: Optional[str] = None


class ReviewSessionCreateRequest(BaseModel):
    cadence: str
    scope: str = "all"
    owner_id: Optional[int] = None
    workspace_scope: Optional[str] = None
    project_scope: Optional[str] = None


class ReviewSessionCompleteRequest(BaseModel):
    completion_notes: Optional[str] = None


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


@router.get("/api/v1/review-queue")
async def api_list_review_queue(
    owner_id: Optional[int] = None,
    project_scope: Optional[str] = None,
    workspace_scope: Optional[str] = None,
    severity: Optional[str] = None,
    item_type: Optional[str] = None,
    scope: str = "all",
    include_deferred: bool = False,
    limit: int = 200,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ReviewQueueService(db)
        items = await service.list_items(
            owner_id=owner_id,
            project_scope=project_scope,
            workspace_scope=workspace_scope,
            severity=severity,
            item_type=item_type,
            scope=scope,
            current_actor_id=actor_id,
            include_deferred=include_deferred,
            limit=limit,
        )
        return {
            "success": True,
            "items": items,
            "count": len(items),
            "filters": {
                "owner_id": owner_id,
                "project_scope": project_scope,
                "workspace_scope": workspace_scope,
                "severity": severity,
                "item_type": item_type,
                "scope": scope,
                "include_deferred": include_deferred,
            },
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/review-queue/{item_id}")
async def api_get_review_queue_item(
    item_id: str,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ReviewQueueService(db)
        item = await service.get_item(item_id, current_actor_id=actor_id, include_deferred=True)
        if not item:
            return {"success": False, "error": "Not found"}
        return {"success": True, "item": item}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/review-queue/{item_id}/actions")
async def api_review_queue_action(
    item_id: str,
    data: ReviewQueueActionRequest,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ReviewQueueService(db)
        result = await service.apply_action(
            item_id=item_id,
            action=data.action,
            actor_id=actor_id,
            rationale=data.rationale,
            final_fields=data.final_fields,
            merge_record_id=data.merge_record_id,
            owner_id=data.owner_id,
            defer_until=data.defer_until,
            new_state=data.new_state,
            severity=data.severity,
            escalation_destination=data.escalation_destination,
            review_notes=data.review_notes,
        )
        return {"success": True, **result}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/review-queue/bulk-actions")
async def api_review_queue_bulk_action(
    data: ReviewQueueBulkActionRequest,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ReviewQueueService(db)
        result = await service.bulk_apply_action(
            item_ids=data.item_ids,
            action=data.action,
            actor_id=actor_id,
            payload={
                "rationale": data.rationale,
                "final_fields": data.final_fields,
                "merge_record_id": data.merge_record_id,
                "owner_id": data.owner_id,
                "defer_until": data.defer_until,
                "new_state": data.new_state,
                "severity": data.severity,
                "escalation_destination": data.escalation_destination,
                "review_notes": data.review_notes,
            },
        )
        return {"success": True, **result}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/review-queue/sessions")
async def api_create_review_session(
    data: ReviewSessionCreateRequest,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ReviewQueueService(db)
        session = await service.generate_review_session(
            cadence=data.cadence,
            actor_id=actor_id,
            scope=data.scope,
            owner_id=data.owner_id,
            workspace_scope=data.workspace_scope,
            project_scope=data.project_scope,
        )
        return {"success": True, "session": session}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/review-queue/sessions/{session_id}")
async def api_get_review_session(session_id: str, db=Depends(get_db)):
    try:
        service = ReviewQueueService(db)
        session = await service.get_review_session(session_id)
        if not session:
            return {"success": False, "error": "Not found"}
        return {"success": True, "session": session}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/review-queue/sessions/{session_id}/complete")
async def api_complete_review_session(
    session_id: str,
    data: ReviewSessionCompleteRequest,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    try:
        actor_id = await _persist_actor_id(db, current_actor)
        service = ReviewQueueService(db)
        session = await service.complete_review_session(
            session_id=session_id,
            actor_id=actor_id,
            completion_notes=data.completion_notes,
        )
        return {"success": True, "session": session}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}