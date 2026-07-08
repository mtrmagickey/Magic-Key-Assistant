"""Pulse router — Unified operational attention surface.

Aggregates overdue actions, pending proposals, planning collisions,
invariant violations, high-priority gaps, and continuity states into
a single urgency-sorted feed.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from admin.dependencies import get_db, require_admin, templates

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["pulse"], dependencies=[Depends(require_admin)])


# ── Page route ────────────────────────────────────────────────────────────────

@router.get("/pulse", response_class=HTMLResponse)
async def pulse_page(request: Request):
    return templates.TemplateResponse(request, "pulse.html", {"active_page": "pulse"})


# ── Aggregated feed ───────────────────────────────────────────────────────────

@router.get("/api/v1/pulse/feed")
async def api_pulse_feed(db=Depends(get_db)):
    """Return a unified, urgency-sorted feed of items needing attention."""
    items = []

    async with db.acquire() as conn:
        # 1. Overdue actions
        try:
            sql = """
                SELECT id, title, due_date, priority, assigned_to, status
                FROM tasks
                WHERE status NOT IN ('done','cancelled')
                  AND due_date IS NOT NULL AND due_date < date('now')
                ORDER BY due_date ASC LIMIT 50
            """
            async with conn.execute(sql) as cur:
                for row in await cur.fetchall():
                    r = dict(row)
                    items.append({
                        "type": "overdue_action",
                        "id": r["id"],
                        "title": r["title"],
                        "detail": f"Due {r['due_date']} · {r.get('assigned_to') or 'Unassigned'}",
                        "severity": "critical" if r.get("priority") in ("critical", "high") else "warning",
                        "due": r["due_date"],
                        "entity_type": "action",
                        "entity_id": r["id"],
                    })
        except Exception as e:
            logger.debug("pulse: overdue actions query: %s", e)

        # 2. Pending extraction proposals
        try:
            sql = """
                SELECT id, artifact_type, title, confidence, created_at
                FROM extraction_proposals
                WHERE status = 'pending'
                ORDER BY created_at DESC LIMIT 30
            """
            async with conn.execute(sql) as cur:
                for row in await cur.fetchall():
                    r = dict(row)
                    items.append({
                        "type": "pending_proposal",
                        "id": r["id"],
                        "title": f"Review: {r.get('title') or r.get('artifact_type', 'extraction')}",
                        "detail": f"{r.get('artifact_type', 'unknown')} · confidence {r.get('confidence', '?')}",
                        "severity": "info",
                        "due": r.get("created_at"),
                        "entity_type": "proposal",
                        "entity_id": r["id"],
                    })
        except Exception as e:
            logger.debug("pulse: extraction proposals query: %s", e)

        # 3. High-priority open knowledge gaps
        try:
            sql = """
                SELECT id, question, priority, created_at
                FROM knowledge_gaps
                WHERE status = 'open' AND priority >= 7
                ORDER BY priority DESC, created_at ASC LIMIT 20
            """
            async with conn.execute(sql) as cur:
                for row in await cur.fetchall():
                    r = dict(row)
                    items.append({
                        "type": "knowledge_gap",
                        "id": r["id"],
                        "title": r["question"][:120],
                        "detail": f"Priority {r.get('priority', '?')}",
                        "severity": "warning",
                        "due": r.get("created_at"),
                        "entity_type": "gap",
                        "entity_id": r["id"],
                    })
        except Exception as e:
            logger.debug("pulse: knowledge gaps query: %s", e)

        # 4. Overdue obligations
        try:
            sql = """
                SELECT id, title, next_due, frequency, owner_username
                FROM obligations
                WHERE status = 'active'
                  AND next_due IS NOT NULL AND next_due < date('now')
                ORDER BY next_due ASC LIMIT 20
            """
            async with conn.execute(sql) as cur:
                for row in await cur.fetchall():
                    r = dict(row)
                    items.append({
                        "type": "overdue_obligation",
                        "id": r["id"],
                        "title": r["title"],
                        "detail": f"Due {r['next_due']} · {r.get('frequency', '')} · {r.get('owner_username') or 'Unassigned'}",
                        "severity": "warning",
                        "due": r["next_due"],
                        "entity_type": "obligation",
                        "entity_id": r["id"],
                    })
        except Exception as e:
            logger.debug("pulse: obligations query: %s", e)

        # 5. Unread inbox threads
        try:
            sql = """
                SELECT id, subject, created_at
                FROM inbox_threads
                WHERE status = 'unread'
                ORDER BY created_at DESC LIMIT 10
            """
            async with conn.execute(sql) as cur:
                for row in await cur.fetchall():
                    r = dict(row)
                    items.append({
                        "type": "unread_thread",
                        "id": r["id"],
                        "title": r.get("subject") or "New conversation",
                        "detail": "Unread",
                        "severity": "info",
                        "due": r.get("created_at"),
                        "entity_type": "thread",
                        "entity_id": r["id"],
                    })
        except Exception as e:
            logger.debug("pulse: inbox threads query: %s", e)

    # Sort: critical first, then warning, then info; within same severity, by due date
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda x: (severity_order.get(x.get("severity"), 9), x.get("due") or "9999"))

    return {"success": True, "items": items, "total": len(items)}


@router.get("/api/v1/pulse/stats")
async def api_pulse_stats(db=Depends(get_db)):
    """Quick counts for the Pulse badge."""
    counts = {"overdue_actions": 0, "pending_proposals": 0, "high_gaps": 0, "overdue_obligations": 0, "unread_threads": 0}
    async with db.acquire() as conn:
        try:
            async with conn.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','cancelled') AND due_date IS NOT NULL AND due_date < date('now')") as cur:
                counts["overdue_actions"] = (await cur.fetchone())[0]
        except Exception as e:
            logger.warning("api_pulse_stats: suppressed %s", e)
        try:
            async with conn.execute("SELECT COUNT(*) FROM extraction_proposals WHERE status = 'pending'") as cur:
                counts["pending_proposals"] = (await cur.fetchone())[0]
        except Exception as e:
            logger.warning("api_pulse_stats: suppressed %s", e)
        try:
            async with conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE status = 'open' AND priority >= 7") as cur:
                counts["high_gaps"] = (await cur.fetchone())[0]
        except Exception as e:
            logger.warning("api_pulse_stats: suppressed %s", e)
        try:
            async with conn.execute("SELECT COUNT(*) FROM obligations WHERE status = 'active' AND next_due IS NOT NULL AND next_due < date('now')") as cur:
                counts["overdue_obligations"] = (await cur.fetchone())[0]
        except Exception as e:
            logger.warning("api_pulse_stats: suppressed %s", e)
        try:
            async with conn.execute("SELECT COUNT(*) FROM inbox_threads WHERE status = 'unread'") as cur:
                counts["unread_threads"] = (await cur.fetchone())[0]
        except Exception as e:
            logger.warning("api_pulse_stats: suppressed %s", e)

    counts["total"] = sum(counts.values())
    return {"success": True, **counts}


@router.post("/api/v1/pulse/defer/{entity_type}/{entity_id}")
async def api_pulse_defer(entity_type: str, entity_id: int, db=Depends(get_db)):
    """Defer an item — extends due date by 7 days or marks as deferred."""
    async with db.acquire() as conn:
        if entity_type == "action":
            await conn.execute("UPDATE tasks SET due_date = date(due_date, '+7 days') WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Deferred by 7 days"}
        elif entity_type == "obligation":
            await conn.execute("UPDATE obligations SET next_due = date(next_due, '+7 days') WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Deferred by 7 days"}
        elif entity_type == "gap":
            await conn.execute("UPDATE knowledge_gaps SET status = 'deferred' WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Gap deferred"}

    return {"success": False, "error": "Unknown entity type"}


@router.post("/api/v1/pulse/resolve/{entity_type}/{entity_id}")
async def api_pulse_resolve(entity_type: str, entity_id: int, db=Depends(get_db)):
    """Quick-resolve an item from the Pulse feed."""
    async with db.acquire() as conn:
        if entity_type == "action":
            await conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Action marked done"}
        elif entity_type == "obligation":
            await conn.execute("UPDATE obligations SET status = 'completed' WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Obligation completed"}
        elif entity_type == "gap":
            await conn.execute("UPDATE knowledge_gaps SET status = 'resolved' WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Gap resolved"}
        elif entity_type == "proposal":
            await conn.execute("UPDATE extraction_proposals SET status = 'accepted' WHERE id = ?", [entity_id])
            await conn.commit()
            return {"success": True, "message": "Proposal accepted"}

    return {"success": False, "error": "Unknown entity type"}
