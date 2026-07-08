"""Activity Feed router — unified timeline of system events."""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.services.audit_service import AuditService
from fastapi import APIRouter, Depends
from services.request_tracing import get_request_trace_detail, list_request_traces

from admin.dependencies import get_db, require_admin

logger = logging.getLogger("AdminServer.activity")
router = APIRouter(tags=["activity"], dependencies=[Depends(require_admin)])


def _parse_json_field(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


@router.get("/api/v1/audit-events")
async def api_list_audit_events(
    limit: int = 100,
    offset: int = 0,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    db=Depends(get_db),
):
    try:
        service = AuditService(db)
        events = await service.list_events(
            entity_type=entity_type,
            entity_id=entity_id,
            correlation_id=correlation_id,
            limit=limit,
            offset=offset,
        )
        return {"success": True, "events": events, "count": len(events), "limit": limit, "offset": offset}
    except Exception as exc:
        logger.debug("audit events unavailable: %s", exc)
        return {"success": True, "events": [], "count": 0, "limit": limit, "offset": offset}


@router.get("/api/v1/audit-events/{entity_type}/{entity_id}")
async def api_entity_audit_history(entity_type: str, entity_id: str, limit: int = 200, db=Depends(get_db)):
    try:
        service = AuditService(db)
        events = await service.get_entity_history(entity_type=entity_type, entity_id=entity_id, limit=limit)
        return {"success": True, "events": events, "count": len(events)}
    except Exception as exc:
        logger.debug("entity audit history unavailable: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/work-packets")
async def api_list_work_packets(
    limit: int = 25,
    include_completed: bool = True,
    db=Depends(get_db),
):
    """List recent work packets for lightweight admin inspection."""
    try:
        from core.services.work_packet_service import WorkPacketService

        svc = WorkPacketService(db)
        packets = await svc.list_packets(limit=limit, include_completed=include_completed)
        return {"success": True, "packets": packets, "count": len(packets)}
    except Exception as exc:
        logger.debug("work_packets not available: %s", exc)
        return {"success": True, "packets": [], "count": 0}


@router.get("/api/v1/work-packets/{packet_id}/events")
async def api_work_packet_events(packet_id: int, limit: int = 100, db=Depends(get_db)):
    """Return lifecycle events for a single work packet."""
    try:
        from core.services.work_packet_service import WorkPacketService

        svc = WorkPacketService(db)
        packet = await svc.get(packet_id)
        if not packet:
            return {"success": False, "error": "Work packet not found"}
        events = await svc.list_events(packet_id, limit=limit)
        return {"success": True, "packet": packet, "events": events, "count": len(events)}
    except Exception as exc:
        logger.debug("packet_events not available: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/request-traces")
async def api_list_request_traces(
    limit: int = 50,
    offset: int = 0,
    lane: Optional[str] = None,
    failures_only: bool = False,
    db=Depends(get_db),
):
    """List recent persisted request traces for chat and related user-visible flows."""
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    try:
        return await list_request_traces(
            db,
            limit=limit,
            offset=offset,
            lane=lane,
            failures_only=failures_only,
        )
    except Exception as exc:
        logger.debug("request_traces not available: %s", exc)
        return {"success": True, "traces": [], "count": 0, "total": 0, "limit": limit, "offset": offset}


@router.get("/api/v1/request-traces/{request_id}")
async def api_request_trace_detail(request_id: str, db=Depends(get_db)):
    """Return one request trace with all recorded stage events."""
    try:
        return await get_request_trace_detail(db, request_id)
    except Exception as exc:
        logger.debug("request trace detail unavailable: %s", exc)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/activity")
async def api_activity_feed(
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,
    db=Depends(get_db),
):
    """
    Return a unified, reverse-chronological activity feed pulled from
    multiple tables: job_runs, receipts, learning_loop_events.

    Each row has: timestamp, source, title, detail, status.
    """
    limit = min(max(1, limit), 200)

    # We UNION across available tables, each mapped to a common shape.
    # Not every table may exist yet (early installs), so we try/except.
    events: list[dict] = []

    async with db.acquire() as conn:
        # 1. job_runs
        try:
            sql = """
                SELECT started_at, 'job' AS source,
                       job_name AS title,
                       COALESCE(error_message, '') AS detail,
                       status
                FROM job_runs
                ORDER BY started_at DESC
                LIMIT 200
            """
            async with conn.execute(sql) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "timestamp": r[0], "source": r[1],
                        "title": r[2], "detail": r[3], "status": r[4],
                    })
        except Exception as exc:
            logger.debug("job_runs not available: %s", exc)

        # 2. receipts (command audit trail)
        try:
            sql = """
                SELECT executed_at, 'command' AS source,
                       command_name AS title,
                       COALESCE(user_id, '') AS detail,
                       result_status AS status
                FROM receipts
                ORDER BY executed_at DESC
                LIMIT 200
            """
            async with conn.execute(sql) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "timestamp": r[0], "source": r[1],
                        "title": r[2], "detail": str(r[3]), "status": r[4],
                    })
        except Exception as exc:
            logger.debug("receipts not available: %s", exc)

        # 3. learning_loop_events
        try:
            sql = """
                SELECT created_at, 'learning' AS source,
                       event_type AS title,
                       COALESCE(description, '') AS detail,
                       'info' AS status
                FROM learning_loop_events
                ORDER BY created_at DESC
                LIMIT 200
            """
            async with conn.execute(sql) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "timestamp": r[0], "source": r[1],
                        "title": r[2], "detail": r[3], "status": r[4],
                    })
        except Exception as exc:
            logger.debug("learning_loop_events not available: %s", exc)

        # 4. tool_executions (agentic chat tools)
        try:
            sql = """
                SELECT executed_at, 'tool' AS source,
                       tool_name AS title,
                       COALESCE(message, '') AS detail,
                       CASE WHEN success THEN 'success' ELSE 'failed' END AS status
                FROM tool_executions
                ORDER BY executed_at DESC
                LIMIT 200
            """
            async with conn.execute(sql) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "timestamp": r[0], "source": r[1],
                        "title": r[2], "detail": r[3], "status": r[4],
                    })
        except Exception as exc:
            logger.debug("tool_executions not available: %s", exc)

        # 5. packet_events (minimum work packet lifecycle)
        try:
            sql = """
                SELECT pe.created_at,
                       'packet' AS source,
                       wp.title AS title,
                       (
                           pe.event_type ||
                           CASE WHEN pe.related_inbox_thread_id IS NOT NULL THEN ' · inbox#' || pe.related_inbox_thread_id ELSE '' END ||
                           CASE WHEN pe.related_tool_execution_id IS NOT NULL THEN ' · tool#' || pe.related_tool_execution_id ELSE '' END ||
                           CASE WHEN pe.related_job_run_id IS NOT NULL THEN ' · job#' || pe.related_job_run_id ELSE '' END
                       ) AS detail,
                       COALESCE(pe.to_status, pe.confirmation_status, 'info') AS status
                FROM packet_events pe
                JOIN work_packets wp ON wp.id = pe.packet_id
                ORDER BY pe.created_at DESC, pe.id DESC
                LIMIT 200
            """
            async with conn.execute(sql) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "timestamp": r[0], "source": r[1],
                        "title": r[2], "detail": r[3], "status": r[4],
                    })
        except Exception as exc:
            logger.debug("packet_events not available: %s", exc)

    # Filter by source if requested
    if source:
        events = [e for e in events if e["source"] == source]

    # Sort by timestamp descending (unified across tables)
    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)

    # Paginate
    total = len(events)
    page = events[offset : offset + limit]

    return {"success": True, "events": page, "total": total, "limit": limit, "offset": offset}
