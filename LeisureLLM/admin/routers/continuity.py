"""Continuity router — Obligations and Feedback.

SOPs and Rails have been deprecated (removed per product direction 2026-07).
Migration tables are preserved for backward compatibility.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Optional

from core.services.operational_continuity_service import OperationalContinuityService
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from admin.dependencies import get_current_actor, get_db, require_admin, templates

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["continuity"], dependencies=[Depends(require_admin)])


def _obligation_completion_packet_key(obligation_id: int) -> str:
    return f"obligation-complete:{obligation_id}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor_display_name(actor) -> str:
    return str(actor.display_name or actor.username or actor.external_ref)


def _actor_ref(actor) -> str:
    return str(actor.stable_id or actor.external_ref)


def _resolve_actor(actor):
    if hasattr(actor, "actor_kind") and hasattr(actor, "stable_id"):
        return actor
    return SimpleNamespace(
        actor_kind="service",
        stable_id="actor_continuity_fallback",
        external_ref="continuity-fallback",
        display_name="Continuity Service",
        username="continuity-service",
    )


def _append_actor_note(existing_notes: Optional[str], actor, action: str, detail: Optional[str] = None) -> str:
    stamped = f"[{action} by {_actor_display_name(actor)} at {_utc_now_iso()}]"
    body = stamped if not detail else f"{stamped}\n{detail.strip()}"
    prefix = (existing_notes or "").strip()
    return body if not prefix else f"{prefix}\n\n{body}"


# ── Pydantic models ──────────────────────────────────────────────────────────

class ObligationCreate(BaseModel):
    title: str
    description: Optional[str] = None
    frequency: str = "monthly"
    owner_username: Optional[str] = None
    next_due: Optional[str] = None
    checklist: Optional[List[str]] = None
    evidence_links: Optional[List[str]] = None
    category: Optional[str] = None
    notes: Optional[str] = None

class ObligationUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    frequency: Optional[str] = None
    owner_username: Optional[str] = None
    next_due: Optional[str] = None
    status: Optional[str] = None
    checklist: Optional[List[str]] = None
    evidence_links: Optional[List[str]] = None
    category: Optional[str] = None
    notes: Optional[str] = None

class FeedbackCreate(BaseModel):
    summary: str
    category: Optional[str] = None
    severity: Optional[str] = None
    context: Optional[str] = None
    submitted_by: Optional[str] = None

class FeedbackUpdate(BaseModel):
    status: Optional[str] = None
    resolution: Optional[str] = None


# =============================================================================
# Page routes
# =============================================================================

@router.get("/obligations", response_class=HTMLResponse)
async def obligations_page(request: Request):
    return templates.TemplateResponse(request, "obligations.html", {"active_page": "obligations"})

@router.get("/provenance", response_class=HTMLResponse)
async def provenance_page(request: Request):
    return templates.TemplateResponse(request, "provenance.html", {"active_page": "provenance"})

@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request):
    return templates.TemplateResponse(request, "feedback.html", {"active_page": "feedback"})


# =============================================================================
# Obligations
# =============================================================================

@router.get("/api/v1/obligations")
async def api_list_obligations(
    status: Optional[str] = None, category: Optional[str] = None,
    page: int = 1, per_page: int = 50, db=Depends(get_db),
):
    try:
        async with db.acquire() as conn:
            where, params = [], []
            if status:
                where.append("status = ?")
                params.append(status)
            if category:
                where.append("category = ?")
                params.append(category)
            w = f"WHERE {' AND '.join(where)}" if where else ""
            async with conn.execute(f"SELECT COUNT(*) FROM obligations {w}", params) as cur:
                total = (await cur.fetchone())[0]
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            async with conn.execute(f"SELECT * FROM obligations {w} ORDER BY next_due ASC LIMIT ? OFFSET ?", params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return {"success": True, "obligations": rows, "total": total, "page": page, "per_page": per_page}
    except Exception as e:
        logger.error(f"list obligations: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/obligations")
async def api_create_obligation(data: ObligationCreate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        from core.services.obligation_service import ObligationService
        svc = ObligationService(db)
        obl_id = await svc.create(
            data.title, description=data.description, frequency=data.frequency,
            owner_username=data.owner_username, next_due=data.next_due,
            checklist=data.checklist, evidence_links=data.evidence_links,
            category=data.category,
            notes=_append_actor_note(data.notes, current_actor, "Created obligation"),
        )
        return {"success": True, "id": obl_id}
    except Exception as e:
        logger.error(f"create obligation: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/obligations/stats")
async def api_obligation_stats(db=Depends(get_db)):
    try:
        from core.services.obligation_service import ObligationService
        svc = ObligationService(db)
        return {"success": True, "stats": await svc.stats()}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/obligations/{obl_id}")
async def api_get_obligation(obl_id: int, db=Depends(get_db)):
    try:
        from core.services.obligation_service import ObligationService
        svc = ObligationService(db)
        ob = await svc.get(obl_id)
        if not ob:
            return {"success": False, "error": "Not found"}
        return {"success": True, "obligation": ob}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.patch("/api/v1/obligations/{obl_id}")
async def api_update_obligation(obl_id: int, data: ObligationUpdate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        from core.services.obligation_service import ObligationService
        svc = ObligationService(db)
        fields = {k: v for k, v in data.model_dump().items() if v is not None}
        existing = await svc.get(obl_id)
        if not existing:
            return {"success": False, "error": "Not found"}
        note_detail = None
        if "notes" in fields:
            note_detail = str(fields.pop("notes") or "")
        fields["notes"] = _append_actor_note(existing.get("notes"), current_actor, "Updated obligation", note_detail)
        return {"success": await svc.update(obl_id, **fields)}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/obligations/{obl_id}/complete")
async def api_complete_obligation(obl_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        from core.services.obligation_service import ObligationService
        from core.services.work_packet_service import WorkPacketService

        svc = ObligationService(db)
        obligation = await svc.get(obl_id)
        if not obligation:
            return {"success": False, "error": "Obligation not found"}

        packet_svc = WorkPacketService(db)
        packet = await packet_svc.create_packet(
            packet_key=f"{_obligation_completion_packet_key(obl_id)}:{obligation.get('next_due') or 'unscheduled'}",
            packet_type="obligation_followup",
            title=f"Complete obligation #{obl_id}",
            objective="Mark the linked obligation as completed.",
            status="active",
            lane="deterministic",
            owner_kind=current_actor.actor_kind,
            owner_ref=_actor_ref(current_actor),
            next_step="Write completion state to the obligation authority table.",
            current_summary="Deterministic obligation completion is in progress.",
            created_from_type="obligation",
            created_from_id=str(obl_id),
            actor_kind=current_actor.actor_kind,
            actor_ref=_actor_ref(current_actor),
            summary=f"Created work packet for obligation completion by {_actor_display_name(current_actor)}.",
        )
        await packet_svc.ensure_link(
            packet["id"],
            link_role="primary_target",
            target_type="obligation",
            target_id=obl_id,
            is_primary=True,
            note=f"Primary obligation #{obl_id}.",
            actor_kind=current_actor.actor_kind,
            actor_ref=_actor_ref(current_actor),
        )

        ok = await svc.mark_completed(obl_id)
        if ok:
            await svc.update(
                obl_id,
                notes=_append_actor_note(obligation.get("notes"), current_actor, "Completed obligation"),
            )
        obligation = await svc.get(obl_id)
        if ok and obligation and obligation.get("status") == "completed":
            await packet_svc.transition(
                packet["id"],
                status="completed",
                lane="deterministic",
                completion_summary="Obligation marked completed in the authoritative obligations table.",
                terminal_reason="authority_confirmed",
                event_type="packet_completed",
                actor_kind=current_actor.actor_kind,
                actor_ref=_actor_ref(current_actor),
                summary=f"Obligation completion confirmed by authoritative state for {_actor_display_name(current_actor)}.",
                requires_confirmation=False,
                confirmation_status="not_required",
            )
        else:
            await packet_svc.transition(
                packet["id"],
                status="failed",
                lane="deterministic",
                blocked_reason="Obligation did not transition to completed.",
                terminal_reason="authority_not_updated",
                event_type="packet_failed",
                actor_kind=current_actor.actor_kind,
                actor_ref=_actor_ref(current_actor),
                summary=f"Obligation completion could not be confirmed for {_actor_display_name(current_actor)}.",
                requires_confirmation=False,
                confirmation_status="not_required",
            )
        return {"success": ok}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Feedback
# =============================================================================

@router.get("/api/v1/feedback")
async def api_list_feedback(
    status: Optional[str] = None, category: Optional[str] = None,
    page: int = 1, per_page: int = 50, db=Depends(get_db),
):
    try:
        from core.services.feedback_service import FeedbackService
        svc = FeedbackService(db)
        items = await svc.list_all(status=status, category=category, limit=per_page)
        return {"success": True, "feedback": items, "total": len(items), "page": page, "per_page": per_page}
    except Exception as e:
        logger.error(f"list feedback: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/feedback")
async def api_create_feedback(data: FeedbackCreate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        from core.services.feedback_service import FeedbackService
        svc = FeedbackService(db)
        fb_id = await svc.create(
            data.summary, category=data.category, severity=data.severity,
            context=data.context, submitted_by=data.submitted_by or _actor_display_name(current_actor),
        )
        return {"success": True, "id": fb_id}
    except Exception as e:
        logger.error(f"create feedback: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/feedback/report-problem")
async def api_report_problem(data: FeedbackCreate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Create feedback, auto-attach a support bundle, return both."""
    try:
        current_actor = _resolve_actor(current_actor)
        from core.backup_restore import create_support_bundle
        from core.services.feedback_service import FeedbackService

        svc = FeedbackService(db)
        fb_id = await svc.create(
            data.summary,
            category=data.category or "bug",
            severity=data.severity or "medium",
            context=data.context,
            submitted_by=data.submitted_by or _actor_display_name(current_actor),
        )
        # Auto-generate a support bundle
        bundle = create_support_bundle(db.database_path)
        # Link bundle path into the feedback record
        await svc.update(fb_id, context=f"{data.context or ''}\n\n[support_bundle:{bundle.name}]")
        return {"success": True, "feedback_id": fb_id, "bundle": bundle.name}
    except Exception as e:
        logger.error(f"report-problem: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/feedback/{fb_id}/resolve")
async def api_resolve_feedback(fb_id: int, data: FeedbackUpdate, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    try:
        current_actor = _resolve_actor(current_actor)
        from core.services.feedback_service import FeedbackService
        svc = FeedbackService(db)
        resolution = (data.resolution or "Resolved").strip()
        return {"success": await svc.resolve(fb_id, f"Resolved by {_actor_display_name(current_actor)}: {resolution}")}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Reasoning API — World model, causal graph, symbolic rules
# =============================================================================

@router.get("/api/v1/planning/collisions")
async def api_planning_collisions(
    lookahead_days: int = 14,
    threshold: int = 3,
    db=Depends(get_db),
):
    """Detect upcoming date collisions (world model: forward simulation)."""
    try:
        from core.forward_planner import detect_collisions
        collisions = await detect_collisions(
            db, lookahead_days=lookahead_days, threshold=threshold,
        )
        return {
            "success": True,
            "collisions": [
                {"date": c.window_start.isoformat(), "count": c.count, "items": c.items}
                for c in collisions
            ],
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/planning/next-due/{obligation_id}")
async def api_planning_next_due(obligation_id: int, horizon: int = 6, db=Depends(get_db)):
    """Project the next N occurrence dates for an obligation."""
    try:
        from core.forward_planner import compute_next_due
        from core.services.obligation_service import ObligationService
        svc = ObligationService(db)
        obl = await svc.get(obligation_id)
        if not obl:
            return {"success": False, "error": "Obligation not found"}
        dates = compute_next_due(
            frequency=obl["frequency"],
            last_completed=obl.get("last_completed"),
            current_next_due=obl.get("next_due"),
            horizon=horizon,
        )
        return {"success": True, "obligation_id": obligation_id, "dates": [d.isoformat() for d in dates]}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/planning/slip/{task_id}")
async def api_planning_slip(task_id: int, slip_days: int = 7, db=Depends(get_db)):
    """Simulate the cascade if a task slips by N days."""
    try:
        from core.forward_planner import propagate_slip
        effect = await propagate_slip(db, task_id, slip_days)
        return {
            "success": True,
            "source_task_id": effect.source_task_id,
            "slip_days": effect.slip_days,
            "affected_count": len(effect.affected),
            "affected": effect.affected,
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/graph/provenance/{entity_type}/{entity_id}")
async def api_graph_provenance(entity_type: str, entity_id: int, db=Depends(get_db)):
    """Trace an entity back to its originating meeting/decision."""
    try:
        from core.causal_graph import provenance_trace
        node = await provenance_trace(db, entity_type, entity_id)
        return {"success": True, "provenance": node.to_dict()}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/graph/impact/{entity_type}/{entity_id}")
async def api_graph_impact(entity_type: str, entity_id: int, db=Depends(get_db)):
    """Find all entities affected if this entity changes."""
    try:
        from core.causal_graph import impact_trace
        affected = await impact_trace(db, entity_type, entity_id)
        return {"success": True, "affected_count": len(affected), "affected": affected}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/graph/root-cause/{task_id}")
async def api_graph_root_cause(task_id: int, db=Depends(get_db)):
    """Explain why a task is blocked or overdue."""
    try:
        from core.causal_graph import root_cause
        result = await root_cause(db, task_id)
        return {
            "success": True,
            "task_id": result.target_id,
            "title": result.target_title,
            "chain": result.chain,
            "summary": result.summary,
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/graph/decision-chain/{decision_id}")
async def api_graph_decision_chain(decision_id: int, db=Depends(get_db)):
    """Follow the full supersession chain for a decision."""
    try:
        from core.causal_graph import decision_chain
        chain = await decision_chain(db, decision_id)
        return {"success": True, "chain": chain}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/invariants")
async def api_invariants(db=Depends(get_db)):
    """Run cross-entity invariant checks (symbolic verification)."""
    try:
        from core.symbolic_rules import check_invariants
        violations = await check_invariants(db)
        return {
            "success": True,
            "violation_count": len(violations),
            "errors": sum(1 for v in violations if v.severity == "error"),
            "warnings": sum(1 for v in violations if v.severity == "warning"),
            "violations": [
                {"rule": v.rule, "entity_type": v.entity_type,
                 "entity_id": v.entity_id, "detail": v.detail,
                 "severity": v.severity}
                for v in violations
            ],
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/continuity/operational-states")
async def api_list_operational_continuity_states(
    continuity_state: Optional[str] = None,
    record_type: Optional[str] = None,
    active_only: bool = True,
    limit: int = 100,
    db=Depends(get_db),
):
    """List computed operational continuity states across all surfaces."""
    try:
        service = OperationalContinuityService(db)
        states = await service.list_states(
            continuity_state=continuity_state,
            record_type=record_type,
            active_only=active_only,
            limit=limit,
        )
        return {
            "success": True,
            "states": states,
            "count": len(states),
            "active_only": active_only,
        }
    except Exception as exc:
        logger.error("list operational continuity states: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


