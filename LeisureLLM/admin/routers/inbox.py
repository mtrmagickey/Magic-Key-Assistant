"""
Inbox API router — persistent async question & interview threads.

Replaces the real-time chat with an email-like inbox where:
- Questions are processed asynchronously (pipeline runs in background)
- Interview sessions walk through knowledge gaps
- Responses persist across page reloads
- Messages can be marked for ingestion into the knowledge base
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from admin.dependencies import get_current_actor, get_db, require_admin

logger = logging.getLogger("AdminServer.inbox")
router = APIRouter(tags=["inbox"], dependencies=[Depends(require_admin)])

# ── Lazy retriever (shared with chat.py) ─────────────────────────────────────

_vectorstore = None
_retriever = None
_retriever_lock = asyncio.Lock()


async def _ensure_retriever():
    global _retriever, _vectorstore
    if _retriever is not None:
        return _retriever
    async with _retriever_lock:
        if _retriever is not None:
            return _retriever
        try:
            from core.chroma_factory import get_vectorstore
            _vectorstore = get_vectorstore()
            _retriever = _vectorstore.as_retriever(search_kwargs={"k": 20})
            logger.info("Inbox retriever initialised (Chroma, k=20)")
        except Exception as exc:
            logger.error("Failed to init inbox retriever: %s", exc)
            return None
    return _retriever


# ── Request / Response models ─────────────────────────────────────────────────

class CreateThreadRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10_000)
    stream: bool = False  # When True, skip background processing (caller will stream SSE instead)

class ReplyRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10_000)
    stream: bool = False  # When True, skip background processing (caller will stream SSE instead)


class CreateCompletedThreadRequest(BaseModel):
    """Create a thread with a pre-existing response (from live streaming)."""
    message: str = Field(..., min_length=1, max_length=10_000)
    response: str = Field(..., min_length=1, max_length=100_000)
    sources: List[str] = []
    chunk_sources: List[str] = []
    processing_time_ms: int = 0
    trace_id: Optional[str] = None
    conversation_id: Optional[str] = None


class ReplyCompletedRequest(BaseModel):
    """Save a reply with a pre-existing response (from live streaming)."""
    message: str = Field(..., min_length=1, max_length=10_000)
    response: str = Field(..., min_length=1, max_length=100_000)
    sources: List[str] = []
    chunk_sources: List[str] = []
    processing_time_ms: int = 0
    trace_id: Optional[str] = None


class PatchThreadRequest(BaseModel):
    status: Optional[str] = None  # read | archived
    is_starred: Optional[bool] = None

class InterviewStartRequest(BaseModel):
    topic: Optional[str] = None

class InterviewAnswerRequest(BaseModel):
    answer: str

class FeedbackRequest(BaseModel):
    question: str
    answer: str
    feedback: str  # "helpful" | "not_helpful"
    chunk_sources: List[str] = []
    thread_id: Optional[int] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "untitled").lower().strip())
    return s.strip("-")[:max_len]


def _relative_time(iso_str: str) -> str:
    """Human-friendly relative timestamp."""
    try:
        dt = datetime.fromisoformat(iso_str)
        diff = datetime.utcnow() - dt
        secs = int(diff.total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _get_db_direct():
    """Get DB reference outside of FastAPI dependency injection."""
    from admin.dependencies import get_bot
    bot = get_bot()
    if bot and hasattr(bot, "db") and bot.db:
        return bot.db
    return None


def _inbox_packet_key(thread_id: int) -> str:
    return f"inbox-thread:{thread_id}"


def _resolve_actor(actor):
    if hasattr(actor, "actor_kind") and hasattr(actor, "stable_id"):
        return actor
    return SimpleNamespace(
        actor_id=0,
        actor_kind="system",
        stable_id="actor_inbox_fallback",
        external_ref="inbox-fallback",
        display_name="Inbox Service",
        username="inbox-service",
        account_id=0,
    )


def _actor_display_name(actor) -> str:
    return str(actor.display_name or actor.username or actor.external_ref)


def _actor_ref(actor) -> str:
    return str(actor.stable_id or actor.external_ref)


async def _ensure_inbox_work_packet(db, thread_id: int, *, actor_kind: str = "system", actor_ref: str = "inbox", actor_id: Optional[int] = None):
    try:
        from core.services.work_packet_service import WorkPacketService

        svc = WorkPacketService(db)
        packet = await svc.create_packet(
            packet_key=_inbox_packet_key(thread_id),
            packet_type="inbox_followup",
            title=f"Inbox thread #{thread_id}",
            objective="Respond to the linked inbox thread.",
            status="active",
            lane="assistive",
            owner_kind=actor_kind,
            owner_ref=actor_ref,
            next_step="Generate or refresh the assistant response.",
            current_summary="Inbox thread created and queued for response work.",
            created_from_type="inbox_thread",
            created_from_id=str(thread_id),
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            actor_id=actor_id,
            summary="Created work packet for inbox-originated work.",
            related_inbox_thread_id=thread_id,
        )
        await svc.ensure_link(
            packet["id"],
            link_role="primary_target",
            target_type="inbox_thread",
            target_id=thread_id,
            is_primary=True,
            note=f"Primary inbox thread #{thread_id}.",
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            actor_id=actor_id,
        )
        return packet
    except Exception as exc:
        logger.warning("Inbox work packet setup failed for thread %s: %s", thread_id, exc)
        return None


async def _set_inbox_packet_active(db, thread_id: int, summary: str, *, actor_kind: str = "system", actor_ref: str = "inbox", actor_id: Optional[int] = None):
    try:
        from core.services.work_packet_service import WorkPacketService

        svc = WorkPacketService(db)
        packet = await _ensure_inbox_work_packet(db, thread_id, actor_kind=actor_kind, actor_ref=actor_ref, actor_id=actor_id)
        if not packet:
            return None
        return await svc.transition(
            packet["id"],
            status="active",
            lane="assistive",
            approval_required=False,
            approval_status="not_required",
            current_summary=summary,
            completion_summary=None,
            terminal_reason=None,
            next_step="Generate or refresh the assistant response.",
            event_type="packet_status_changed",
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            actor_id=actor_id,
            summary=summary,
            related_inbox_thread_id=thread_id,
            requires_confirmation=False,
            confirmation_status="not_required",
        )
    except Exception as exc:
        logger.warning("Inbox packet activation failed for thread %s: %s", thread_id, exc)
        return None


async def _set_inbox_packet_awaiting_human(db, thread_id: int, summary: str, *, actor_kind: str = "system", actor_ref: str = "inbox", actor_id: Optional[int] = None):
    try:
        from core.services.work_packet_service import WorkPacketService

        svc = WorkPacketService(db)
        packet = await _ensure_inbox_work_packet(db, thread_id, actor_kind=actor_kind, actor_ref=actor_ref, actor_id=actor_id)
        if not packet:
            return None
        return await svc.transition(
            packet["id"],
            status="awaiting_human",
            lane="assistive",
            approval_required=True,
            approval_status="pending",
            current_summary=summary,
            next_step="Review the response and either close the thread or reply with follow-up input.",
            event_type="approval_requested",
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            actor_id=actor_id,
            summary="Response is ready; awaiting human review.",
            related_inbox_thread_id=thread_id,
            requires_confirmation=True,
            confirmation_status="pending",
        )
    except Exception as exc:
        logger.warning("Inbox packet review transition failed for thread %s: %s", thread_id, exc)
        return None


async def _finalize_inbox_packet(db, thread_id: int, *, approved: bool, actor_kind: str = "human", actor_ref: str = "admin", actor_id: Optional[int] = None):
    try:
        from core.services.work_packet_service import WorkPacketService

        svc = WorkPacketService(db)
        packet = await svc.get_by_key(_inbox_packet_key(thread_id))
        if not packet:
            return None

        approval_event = "approval_received" if approved else "approval_rejected"
        approval_status = "approved" if approved else "rejected"
        await svc.record_event(
            packet["id"],
            event_type=approval_event,
            from_status=packet.get("status"),
            to_status=packet.get("status"),
            lane=packet.get("lane"),
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            actor_id=actor_id,
            summary="Inbox review accepted." if approved else "Inbox review rejected.",
            snapshot_json=svc._snapshot_json(packet),
            related_inbox_thread_id=thread_id,
            requires_confirmation=True,
            confirmation_status=approval_status,
        )
        if approved:
            return await svc.transition(
                packet["id"],
                status="completed",
                lane="assistive",
                approval_required=True,
                approval_status="approved",
                completion_summary="Inbox thread review completed.",
                terminal_reason="human_approved",
                event_type="packet_completed",
                actor_kind=actor_kind,
                actor_ref=actor_ref,
                actor_id=actor_id,
                summary="Inbox work packet completed after human review.",
                related_inbox_thread_id=thread_id,
                requires_confirmation=True,
                confirmation_status="approved",
            )
        return await svc.transition(
            packet["id"],
            status="cancelled",
            lane="assistive",
            approval_required=True,
            approval_status="rejected",
            terminal_reason="human_rejected",
            event_type="packet_cancelled",
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            actor_id=actor_id,
            summary="Inbox work packet cancelled after rejection.",
            related_inbox_thread_id=thread_id,
            requires_confirmation=True,
            confirmation_status="rejected",
        )
    except Exception as exc:
        logger.warning("Inbox packet finalization failed for thread %s: %s", thread_id, exc)
        return None


# ── Unread count (for sidebar badge on every page) ───────────────────────────

@router.get("/api/v1/inbox/unread-count", dependencies=[Depends(require_admin)])
async def api_unread_count(db=Depends(get_db)):
    try:
        async with db.acquire() as conn, conn.execute(
            "SELECT COUNT(*) FROM inbox_threads WHERE status = 'ready'"
        ) as cur:
            row = await cur.fetchone()
            return {"count": int(row[0]) if row else 0}
    except Exception:
        return {"count": 0}


# ── Thread CRUD ───────────────────────────────────────────────────────────────

@router.get("/api/v1/inbox/threads", dependencies=[Depends(require_admin)])
async def api_list_threads(db=Depends(get_db)):
    """List all threads (newest first) with preview and unread count."""
    try:
        async with db.acquire() as conn:
            async with conn.execute(
                """
                SELECT t.id, t.subject, t.thread_type, t.status,
                       t.processing_status, t.is_starred,
                       t.created_at, t.updated_at, t.gap_id,
                       (SELECT COUNT(*) FROM inbox_messages WHERE thread_id = t.id) AS msg_count,
                       (SELECT content FROM inbox_messages
                        WHERE thread_id = t.id ORDER BY created_at DESC LIMIT 1) AS preview
                FROM inbox_threads t
                WHERE t.status != 'archived'
                ORDER BY t.updated_at DESC
                """
            ) as cur:
                rows = await cur.fetchall()

            threads = []
            for r in (rows or []):
                preview_text = (r[10] or "")[:120]
                threads.append({
                    "id": r[0],
                    "subject": r[1],
                    "thread_type": r[2],
                    "status": r[3],
                    "processing_status": r[4],
                    "is_starred": bool(r[5]),
                    "created_at": r[6],
                    "updated_at": r[7],
                    "gap_id": r[8],
                    "message_count": r[9] or 0,
                    "preview": preview_text,
                    "time_ago": _relative_time(r[7] or r[6]),
                })

            # Unread = status 'ready' (processed but not yet opened)
            async with conn.execute(
                "SELECT COUNT(*) FROM inbox_threads WHERE status = 'ready'"
            ) as cur:
                row = await cur.fetchone()
                unread = int(row[0]) if row else 0

        return {"threads": threads, "unread_count": unread}
    except Exception as e:
        logger.error("Failed to list threads: %s", e)
        return {
            "threads": [],
            "unread_count": 0,
            "success": False,
            "error": "threads_unavailable",
        }


@router.post("/api/v1/inbox/threads", dependencies=[Depends(require_admin)])
async def api_create_thread(payload: CreateThreadRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Create a new question thread and start background processing."""
    current_actor = _resolve_actor(current_actor)
    message = payload.message.strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")

    subject = message[:120]

    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO inbox_threads (subject, thread_type, status, processing_status)
               VALUES (?, 'question', 'processing', 'Searching knowledge base…')""",
            (subject,),
        )
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            thread_id = (await cur.fetchone())[0]

        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, message),
        )
        await conn.commit()

    await _ensure_inbox_work_packet(
        db,
        thread_id,
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
    )

    # Fire-and-forget background processing (skip when caller will stream SSE)
    if not payload.stream:
        asyncio.create_task(_process_question(thread_id, message, []))

    return {"thread_id": thread_id, "status": "processing"}


@router.get("/api/v1/inbox/threads/{thread_id}", dependencies=[Depends(require_admin)])
async def api_get_thread(thread_id: int, db=Depends(get_db)):
    """Get a single thread with all its messages."""
    async with db.acquire() as conn:
        async with conn.execute(
            """SELECT id, subject, thread_type, status, processing_status,
                      is_starred, created_at, updated_at, gap_id, interview_session_id
               FROM inbox_threads WHERE id = ?""",
            (thread_id,),
        ) as cur:
            t = await cur.fetchone()

        if not t:
            raise HTTPException(404, "Thread not found")

        async with conn.execute(
            """SELECT id, role, content, sources_json, chunk_sources_json,
                      pipeline_stages_json, models_used_json, processing_time_ms,
                      is_ingested, created_at
               FROM inbox_messages WHERE thread_id = ? ORDER BY created_at ASC""",
            (thread_id,),
        ) as cur:
            msg_rows = await cur.fetchall()

    messages = []
    for m in (msg_rows or []):
        messages.append({
            "id": m[0],
            "role": m[1],
            "content": m[2],
            "sources": json.loads(m[3]) if m[3] else [],
            "chunk_sources": json.loads(m[4]) if m[4] else [],
            "pipeline_stages": json.loads(m[5]) if m[5] else None,
            "models_used": json.loads(m[6]) if m[6] else None,
            "processing_time_ms": m[7],
            "is_ingested": bool(m[8]),
            "created_at": m[9],
        })

    return {
        "id": t[0], "subject": t[1], "thread_type": t[2], "status": t[3],
        "processing_status": t[4], "is_starred": bool(t[5]),
        "created_at": t[6], "updated_at": t[7],
        "gap_id": t[8], "interview_session_id": t[9],
        "messages": messages,
    }


@router.patch("/api/v1/inbox/threads/{thread_id}", dependencies=[Depends(require_admin)])
async def api_patch_thread(thread_id: int, payload: PatchThreadRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Update thread status or star."""
    current_actor = _resolve_actor(current_actor)
    updates = []
    params: list = []
    if payload.status is not None:
        if payload.status not in ("read", "archived", "ready"):
            raise HTTPException(400, "Invalid status")
        updates.append("status = ?")
        params.append(payload.status)
    if payload.is_starred is not None:
        updates.append("is_starred = ?")
        params.append(1 if payload.is_starred else 0)

    if not updates:
        return {"ok": True}

    updates.append("updated_at = datetime('now')")
    params.append(thread_id)

    await db.execute(
        f"UPDATE inbox_threads SET {', '.join(updates)} WHERE id = ?", tuple(params)
        )
    if payload.status == "read":
        await _finalize_inbox_packet(
            db,
            thread_id,
            approved=True,
            actor_kind=current_actor.actor_kind,
            actor_ref=_actor_ref(current_actor),
            actor_id=current_actor.actor_id,
        )
    elif payload.status == "archived":
        await _finalize_inbox_packet(
            db,
            thread_id,
            approved=False,
            actor_kind=current_actor.actor_kind,
            actor_ref=_actor_ref(current_actor),
            actor_id=current_actor.actor_id,
        )

    return {"ok": True}


@router.post("/api/v1/inbox/threads/{thread_id}/reply", dependencies=[Depends(require_admin)])
async def api_reply(thread_id: int, payload: ReplyRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Add a follow-up message to an existing thread and process."""
    current_actor = _resolve_actor(current_actor)
    message = payload.message.strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")

    # Fetch thread + existing messages for history
    async with db.acquire() as conn:
        async with conn.execute(
            "SELECT id, thread_type FROM inbox_threads WHERE id = ?", (thread_id,)
        ) as cur:
            thread = await cur.fetchone()
        if not thread:
            raise HTTPException(404, "Thread not found")

        # Build history from existing messages
        async with conn.execute(
            "SELECT role, content FROM inbox_messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        ) as cur:
            history_rows = await cur.fetchall()

        history = [{"role": r[0], "content": r[1]} for r in (history_rows or [])]

        # Save user message
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, message),
        )
        await conn.execute(
            """UPDATE inbox_threads
               SET status = 'processing', processing_status = 'Searching knowledge base…',
                   updated_at = datetime('now')
               WHERE id = ?""",
            (thread_id,),
        )
        await conn.commit()

    await _set_inbox_packet_active(
        db,
        thread_id,
        "Inbox thread received follow-up input and was reopened.",
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
    )

    # Fire-and-forget background processing (skip when caller will stream SSE)
    if not payload.stream:
        asyncio.create_task(_process_question(thread_id, message, history))

    return {"ok": True, "status": "processing"}


# ── Completed thread/reply (from live-streamed chat) ─────────────────────────

@router.post("/api/v1/inbox/threads/completed", dependencies=[Depends(require_admin)])
async def api_create_completed_thread(payload: CreateCompletedThreadRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Create a thread with an already-streamed response (no background processing)."""
    current_actor = _resolve_actor(current_actor)
    message = payload.message.strip()
    response = payload.response.strip()
    if not message or not response:
        raise HTTPException(400, "Both message and response are required")

    subject = message[:120]

    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO inbox_threads (subject, thread_type, status, processing_status)
               VALUES (?, 'question', 'ready', NULL)""",
            (subject,),
        )
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            thread_id = (await cur.fetchone())[0]

        # User message
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, message),
        )
        # Assistant response
        await conn.execute(
            """INSERT INTO inbox_messages
               (thread_id, role, content, sources_json, chunk_sources_json, processing_time_ms)
               VALUES (?, 'assistant', ?, ?, ?, ?)""",
            (
                thread_id,
                response,
                json.dumps(payload.sources) if payload.sources else None,
                json.dumps(payload.chunk_sources) if payload.chunk_sources else None,
                payload.processing_time_ms,
            ),
        )
        await conn.commit()

    await _ensure_inbox_work_packet(
        db,
        thread_id,
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
    )
    await _set_inbox_packet_awaiting_human(
        db,
        thread_id,
        "Assistant response saved for inbox thread; awaiting human review.",
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
    )

    return {"thread_id": thread_id, "status": "ready"}


@router.post("/api/v1/inbox/threads/{thread_id}/reply-completed", dependencies=[Depends(require_admin)])
async def api_reply_completed(thread_id: int, payload: ReplyCompletedRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Save a follow-up reply with an already-streamed response."""
    current_actor = _resolve_actor(current_actor)
    message = payload.message.strip()
    response = payload.response.strip()
    if not message or not response:
        raise HTTPException(400, "Both message and response are required")

    async with db.acquire() as conn:
        async with conn.execute(
            "SELECT id FROM inbox_threads WHERE id = ?", (thread_id,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, "Thread not found")

        # User message
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, message),
        )
        # Assistant response
        await conn.execute(
            """INSERT INTO inbox_messages
               (thread_id, role, content, sources_json, chunk_sources_json, processing_time_ms)
               VALUES (?, 'assistant', ?, ?, ?, ?)""",
            (
                thread_id,
                response,
                json.dumps(payload.sources) if payload.sources else None,
                json.dumps(payload.chunk_sources) if payload.chunk_sources else None,
                payload.processing_time_ms,
            ),
        )
        await conn.execute(
            """UPDATE inbox_threads
               SET status = 'ready', processing_status = NULL,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (thread_id,),
        )
        await conn.commit()

    await _set_inbox_packet_awaiting_human(
        db,
        thread_id,
        "Assistant follow-up saved for inbox thread; awaiting human review.",
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
    )

    return {"ok": True, "status": "ready"}


class SaveResponseRequest(BaseModel):
    """Save only the assistant response (user message already exists in DB)."""
    response: str = Field(..., min_length=1, max_length=100_000)
    sources: List[str] = []
    chunk_sources: List[str] = []
    processing_time_ms: int = 0
    trace_id: Optional[str] = None


@router.post("/api/v1/inbox/threads/{thread_id}/save-response", dependencies=[Depends(require_admin)])
async def api_save_response(thread_id: int, payload: SaveResponseRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Save the assistant response for a thread whose user message is already persisted."""
    current_actor = _resolve_actor(current_actor)
    response = payload.response.strip()
    if not response:
        raise HTTPException(400, "Response cannot be empty")

    async with db.acquire() as conn:
        async with conn.execute(
            "SELECT id FROM inbox_threads WHERE id = ?", (thread_id,)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, "Thread not found")

        await conn.execute(
            """INSERT INTO inbox_messages
               (thread_id, role, content, sources_json, chunk_sources_json, processing_time_ms)
               VALUES (?, 'assistant', ?, ?, ?, ?)""",
            (
                thread_id,
                response,
                json.dumps(payload.sources) if payload.sources else None,
                json.dumps(payload.chunk_sources) if payload.chunk_sources else None,
                payload.processing_time_ms,
            ),
        )
        await conn.execute(
            """UPDATE inbox_threads
               SET status = 'ready', processing_status = NULL,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (thread_id,),
        )
        await conn.commit()

    await _set_inbox_packet_awaiting_human(
        db,
        thread_id,
        "Assistant response saved for inbox thread; awaiting human review.",
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
    )

    return {"ok": True, "status": "ready"}


async def requeue_inbox_thread(
    db,
    thread_id: int,
    *,
    actor_kind: str = "system",
    actor_ref: str = "inbox-recovery",
    actor_id: Optional[int] = None,
    packet_summary: str = "Inbox thread was reprocessed and work resumed.",
    processing_status: str = "Searching knowledge base…",
) -> dict:
    """Requeue an inbox question thread for background processing."""
    async with db.acquire() as conn:
        async with conn.execute(
            "SELECT id FROM inbox_threads WHERE id = ?",
            (thread_id,),
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, "Thread not found")

        async with conn.execute(
            "SELECT role, content FROM inbox_messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            raise HTTPException(400, "No messages in thread")

        last_user_msg = None
        last_user_index = None
        for idx in range(len(rows) - 1, -1, -1):
            row = rows[idx]
            if row[0] == "user":
                last_user_msg = row[1]
                last_user_index = idx
                break
        if not last_user_msg:
            raise HTTPException(400, "No user message found")

        history_rows = rows[:last_user_index] if last_user_index is not None else rows[:-1]
        history = [{"role": r[0], "content": r[1]} for r in history_rows]

        await conn.execute(
            """UPDATE inbox_threads
               SET status = 'processing', processing_status = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (processing_status, thread_id),
        )
        await conn.commit()

    await _set_inbox_packet_active(
        db,
        thread_id,
        packet_summary,
        actor_kind=actor_kind,
        actor_ref=actor_ref,
        actor_id=actor_id,
    )

    asyncio.create_task(_process_question(thread_id, last_user_msg, history))
    return {"ok": True, "status": "processing", "thread_id": thread_id}


async def recover_stale_inbox_threads(
    db,
    *,
    stale_after_seconds: int = 900,
    limit: int = 10,
    actor_ref: str = "inbox-recovery",
) -> dict:
    """Requeue processing inbox threads whose heartbeat has gone stale."""
    stale_after_seconds = max(60, int(stale_after_seconds))
    limit = max(1, int(limit))
    cutoff_modifier = f"-{stale_after_seconds} seconds"

    async with db.acquire() as conn, conn.execute(
        """SELECT id
               FROM inbox_threads
               WHERE thread_type = 'question'
                 AND status = 'processing'
                 AND updated_at <= datetime('now', ?)
               ORDER BY updated_at ASC
               LIMIT ?""",
        (cutoff_modifier, limit),
    ) as cur:
        rows = await cur.fetchall()

    requeued_ids: list[int] = []
    errors: list[dict[str, Any]] = []
    for row in rows or []:
        thread_id = int(row[0])
        try:
            await requeue_inbox_thread(
                db,
                thread_id,
                actor_kind="system",
                actor_ref=actor_ref,
                actor_id=None,
                packet_summary="Inbox stalled thread recovered and work resumed.",
                processing_status="Recovering stalled response…",
            )
            requeued_ids.append(thread_id)
        except HTTPException as exc:
            errors.append({"thread_id": thread_id, "status_code": exc.status_code, "detail": str(exc.detail)})
        except Exception as exc:
            errors.append({"thread_id": thread_id, "detail": str(exc)})

    return {
        "scanned_count": len(rows or []),
        "requeued_count": len(requeued_ids),
        "requeued_thread_ids": requeued_ids,
        "errors": errors,
        "stale_after_seconds": stale_after_seconds,
    }


@router.post("/api/v1/inbox/threads/{thread_id}/reprocess", dependencies=[Depends(require_admin)])
async def api_reprocess(thread_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Trigger background processing for a thread (fallback when streaming fails)."""
    current_actor = _resolve_actor(current_actor)
    return await requeue_inbox_thread(
        db,
        thread_id,
        actor_kind=current_actor.actor_kind,
        actor_ref=_actor_ref(current_actor),
        actor_id=current_actor.actor_id,
        packet_summary="Inbox thread was reprocessed and work resumed.",
        processing_status="Searching knowledge base…",
    )


# ── Message actions ───────────────────────────────────────────────────────────

@router.post("/api/v1/inbox/messages/{message_id}/ingest", dependencies=[Depends(require_admin)])
async def api_ingest_message(message_id: int, db=Depends(get_db)):
    """Mark a message for ingestion — saves content as a doc file."""
    async with db.acquire() as conn:
        async with conn.execute(
            """SELECT m.id, m.content, m.role, m.thread_id, t.subject
               FROM inbox_messages m JOIN inbox_threads t ON t.id = m.thread_id
               WHERE m.id = ?""",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            raise HTTPException(404, "Message not found")

        content, thread_id, subject = row[1], row[3], row[4]

        # Get the user question for context
        async with conn.execute(
            "SELECT content FROM inbox_messages WHERE thread_id = ? AND role = 'user' ORDER BY created_at ASC LIMIT 1",
            (thread_id,),
        ) as cur:
            q_row = await cur.fetchone()
        question = q_row[0] if q_row else ""

    # Save as doc file
    today = datetime.utcnow().strftime("%Y-%m-%d")
    slug = _slugify(subject)
    # __file__ = admin/routers/inbox.py -> .parent x3 = LeisureLLM/
    docs_dir = Path(__file__).parent.parent.parent / "docs" / "web_inbox"
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / f"{today}_{slug}.md"

    doc_content = (
        f"---\n"
        f"source: web_inbox\n"
        f"date: {today}\n"
        f"subject: {subject}\n"
        f"type: qa_exchange\n"
        f"---\n\n"
        f"## Question\n{question}\n\n"
        f"## Answer\n{content}\n"
    )
    doc_path.write_text(doc_content, encoding="utf-8")

    await db.execute(
        "UPDATE inbox_messages SET is_ingested = 1, ingested_at = datetime('now') WHERE id = ?",
        (message_id,),
        )
    logger.info("Ingested message %d → %s", message_id, doc_path)
    return {"ok": True, "path": str(doc_path.relative_to(doc_path.parent.parent.parent))}


@router.post("/api/v1/inbox/feedback", dependencies=[Depends(require_admin)])
async def api_feedback(payload: FeedbackRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Record thumbs-up / thumbs-down feedback (reuses response_feedback table)."""
    try:
        current_actor = _resolve_actor(current_actor)
        chunk_json = json.dumps(payload.chunk_sources) if payload.chunk_sources else None
        await db.execute(
            """INSERT INTO response_feedback
            (user_id, username, question, answer, feedback, channel_id, message_id, chunk_sources)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (current_actor.account_id or 0, _actor_display_name(current_actor), payload.question, payload.answer[:2000],
            payload.feedback, None, None, chunk_json),
            )
        if payload.thread_id is not None:
            await _set_inbox_packet_awaiting_human(
                db,
                payload.thread_id,
                "Assistant response generated in the background; awaiting human review.",
                actor_kind=current_actor.actor_kind,
                actor_ref=_actor_ref(current_actor),
                actor_id=current_actor.actor_id,
            )

        # Moat: feed into learning loop for prompt/chunk quality tuning
        try:
            from services.feedback_learning_loop import FeedbackLearningLoop
            fll = FeedbackLearningLoop(db)
            await fll.ensure_tables()
            await fll.process_feedback(
                question=payload.question,
                answer=payload.answer[:2000],
                feedback_type=payload.feedback,
                chunk_ids=payload.chunk_sources or [],
                user_id=current_actor.stable_id,
            )
        except Exception as fll_err:
            logger.warning("Feedback learning loop error (non-fatal): %s", fll_err)

        return {"ok": True}
    except Exception as e:
        logger.error("Feedback error: %s", e)
        raise HTTPException(500, "Failed to record feedback")


# ── Interview endpoints ───────────────────────────────────────────────────────

@router.post("/api/v1/inbox/interview/start", dependencies=[Depends(require_admin)])
async def api_start_interview(payload: InterviewStartRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Start an interview session — creates thread + first question."""
    current_actor = _resolve_actor(current_actor)

    # Find open gaps
    gap = await _pick_next_gap(db, exclude_gap_id=None)
    if not gap:
        return {"ok": False, "error": "No open knowledge gaps available for interview. Great — you're fully covered!"}

    topic_label = gap["topic"] or "General"
    subject = f"Interview: {topic_label}"

    async with db.acquire() as conn:
        # Create interview session
        await conn.execute(
            """INSERT INTO interview_sessions
               (interviewer_user_id, interviewer_username, channel_id, status, questions_asked, questions_answered, memos_created)
               VALUES (?, ?, 0, 'active', 0, 0, 0)""",
            (current_actor.account_id or 0, _actor_display_name(current_actor)),
        )
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            session_id = (await cur.fetchone())[0]

        # Create thread
        await conn.execute(
            """INSERT INTO inbox_threads
               (subject, thread_type, status, gap_id, interview_session_id)
               VALUES (?, 'interview', 'ready', ?, ?)""",
            (subject, gap["id"], session_id),
        )
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            thread_id = (await cur.fetchone())[0]

        # Post the first question as an assistant message
        question_content = _format_interview_question(gap)
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'assistant', ?)",
            (thread_id, question_content),
        )

        # Update gap + session
        await conn.execute(
            "UPDATE knowledge_gaps SET status = 'in_progress' WHERE id = ?", (gap["id"],)
        )
        await conn.execute(
            "UPDATE interview_sessions SET questions_asked = questions_asked + 1 WHERE id = ?",
            (session_id,),
        )
        await conn.commit()

    return {"ok": True, "thread_id": thread_id}


@router.post("/api/v1/inbox/interview/{thread_id}/answer", dependencies=[Depends(require_admin)])
async def api_interview_answer(thread_id: int, payload: InterviewAnswerRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Submit an interview answer — saves memo, resolves gap, gets next question."""
    current_actor = _resolve_actor(current_actor)
    answer = payload.answer.strip()
    if not answer:
        raise HTTPException(400, "Answer cannot be empty")

    async with db.acquire() as conn:
        async with conn.execute(
            "SELECT gap_id, interview_session_id FROM inbox_threads WHERE id = ? AND thread_type = 'interview'",
            (thread_id,),
        ) as cur:
            thread = await cur.fetchone()
        if not thread:
            raise HTTPException(404, "Interview thread not found")

        gap_id, session_id = thread[0], thread[1]

        # Get gap details
        async with conn.execute(
            "SELECT topic, question, context FROM knowledge_gaps WHERE id = ?", (gap_id,)
        ) as cur:
            gap_row = await cur.fetchone()

        topic = gap_row[0] if gap_row else "Unknown"
        question = gap_row[1] if gap_row else ""

        # Save user answer
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, answer),
        )

        # Save memo file
        _save_interview_memo(topic, question, answer, gap_id, session_id)

        # Resolve the gap
        await conn.execute(
            """UPDATE knowledge_gaps
               SET status = 'resolved', last_asked = datetime('now'), times_asked = times_asked + 1
               WHERE id = ?""",
            (gap_id,),
        )

        # Record in interview_questions
        try:
            await conn.execute(
                """INSERT INTO interview_questions
                   (session_id, gap_id, question, answer)
                   VALUES (?, ?, ?, ?)""",
                (session_id, gap_id, question, answer[:4000]),
            )
        except Exception:
            pass  # table might not exist in older schemas

        # Update session counters
        await conn.execute(
            """UPDATE interview_sessions
               SET questions_answered = questions_answered + 1,
                   memos_created = memos_created + 1
               WHERE id = ?""",
            (session_id,),
        )

        # Post confirmation
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'system', ?)",
            (thread_id, f"✅ Answer recorded and memo saved for **{topic}**. Gap resolved."),
        )

        await conn.commit()

    # Get next question
    next_gap = await _pick_next_gap(db, exclude_gap_id=gap_id)
    if next_gap:
        async with db.acquire() as conn:
            question_content = _format_interview_question(next_gap)
            await conn.execute(
                "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'assistant', ?)",
                (thread_id, question_content),
            )
            await conn.execute(
                "UPDATE inbox_threads SET gap_id = ?, updated_at = datetime('now') WHERE id = ?",
                (next_gap["id"], thread_id),
            )
            await conn.execute(
                "UPDATE knowledge_gaps SET status = 'in_progress' WHERE id = ?",
                (next_gap["id"],),
            )
            await conn.execute(
                "UPDATE interview_sessions SET questions_asked = questions_asked + 1 WHERE id = ?",
                (session_id,),
            )
            await conn.commit()
        return {"ok": True, "has_next": True}
    else:
        # Interview complete
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'system', ?)",
                (thread_id, "🎉 **Interview complete!** No more knowledge gaps to cover. Great work!"),
            )
            await conn.execute(
                """UPDATE interview_sessions SET status = 'completed', completed_at = datetime('now')
                   WHERE id = ?""",
                (session_id,),
            )
            await conn.execute(
                "UPDATE inbox_threads SET gap_id = NULL, updated_at = datetime('now') WHERE id = ?",
                (thread_id,),
            )
            await conn.commit()
        return {"ok": True, "has_next": False}


@router.post("/api/v1/inbox/interview/{thread_id}/skip", dependencies=[Depends(require_admin)])
async def api_interview_skip(thread_id: int, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Skip current interview question — sets gap back to open, fetches next."""
    current_actor = _resolve_actor(current_actor)
    async with db.acquire() as conn:
        async with conn.execute(
            "SELECT gap_id, interview_session_id FROM inbox_threads WHERE id = ? AND thread_type = 'interview'",
            (thread_id,),
        ) as cur:
            thread = await cur.fetchone()
        if not thread:
            raise HTTPException(404, "Interview thread not found")

        gap_id, session_id = thread[0], thread[1]

        # Set gap back to open
        if gap_id:
            await conn.execute(
                "UPDATE knowledge_gaps SET status = 'open' WHERE id = ?", (gap_id,)
            )

        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'system', ?)",
            (thread_id, "⏭️ Question skipped."),
        )
        await conn.commit()

    # Get next question
    next_gap = await _pick_next_gap(db, exclude_gap_id=gap_id)
    if next_gap:
        async with db.acquire() as conn:
            question_content = _format_interview_question(next_gap)
            await conn.execute(
                "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'assistant', ?)",
                (thread_id, question_content),
            )
            await conn.execute(
                "UPDATE inbox_threads SET gap_id = ?, updated_at = datetime('now') WHERE id = ?",
                (next_gap["id"], thread_id),
            )
            await conn.execute(
                "UPDATE knowledge_gaps SET status = 'in_progress' WHERE id = ?",
                (next_gap["id"],),
            )
            await conn.execute(
                "UPDATE interview_sessions SET questions_asked = questions_asked + 1 WHERE id = ?",
                (session_id,),
            )
            await conn.commit()
        return {"ok": True, "has_next": True}
    else:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'system', ?)",
                (thread_id, "🎉 **Interview complete!** No more knowledge gaps to cover."),
            )
            await conn.execute(
                """UPDATE interview_sessions SET status = 'completed', completed_at = datetime('now')
                   WHERE id = ?""",
                (session_id,),
            )
            await conn.execute(
                "UPDATE inbox_threads SET gap_id = NULL, updated_at = datetime('now') WHERE id = ?",
                (thread_id,),
            )
            await conn.commit()
        return {"ok": True, "has_next": False}


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _pick_next_gap(db, *, exclude_gap_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Pick the best open knowledge gap for an interview question.

    Prefers ``keep``-curated gaps, but falls back to ``defer``-curated gaps so
    the user always has something to work on when open gaps exist.  Also
    recovers stale ``in_progress`` gaps (stuck > 24 h) by resetting them.
    """
    _ORDER = """
        ORDER BY
            (last_asked IS NULL) DESC,
            (CASE
                WHEN question GLOB '*[0-9]*' THEN 2
                WHEN question LIKE '%/%' OR question LIKE '%.%' THEN 1
                ELSE 0
            END) DESC,
            priority_score DESC,
            times_asked DESC,
            last_asked DESC
        LIMIT 15
    """

    def _first_usable(rows, eid):
        for r in rows:
            if r[0] != eid:
                return {
                    "id": r[0], "topic": r[1], "question": r[2],
                    "context": r[3], "times_asked": r[4], "priority_score": r[5],
                }
        if rows:
            r = rows[0]
            return {
                "id": r[0], "topic": r[1], "question": r[2],
                "context": r[3], "times_asked": r[4], "priority_score": r[5],
            }
        return None

    try:
        async with db.acquire() as conn:
            # ── Recover stale in_progress gaps (stuck > 24 h) ─────────────
            await conn.execute(
                """
                UPDATE knowledge_gaps
                SET status = 'open'
                WHERE status = 'in_progress'
                  AND last_asked < datetime('now', '-24 hours')
                """
            )
            await conn.commit()

            # ── 1. Prefer keep-curated gaps ───────────────────────────────
            async with conn.execute(
                f"""
                SELECT id, topic, question, context, times_asked, priority_score
                FROM knowledge_gaps
                WHERE status = 'open'
                  AND COALESCE(curation_status, 'keep') = 'keep'
                {_ORDER}
                """
            ) as cur:
                rows = await cur.fetchall()

            result = _first_usable(rows, exclude_gap_id)
            if result:
                return result

            # ── 2. Fall back to deferred gaps ─────────────────────────────
            async with conn.execute(
                f"""
                SELECT id, topic, question, context, times_asked, priority_score
                FROM knowledge_gaps
                WHERE status = 'open'
                  AND curation_status = 'defer'
                {_ORDER}
                """
            ) as cur:
                rows = await cur.fetchall()

            return _first_usable(rows, exclude_gap_id)

    except Exception as e:
        logger.error("Failed to pick next gap: %s", e)
        return None


def _format_interview_question(gap: Dict[str, Any]) -> str:
    """Format a knowledge gap into an interview question message."""
    from cogs.KnowledgeGapTracker import build_fallback_prompt

    prompt = build_fallback_prompt(gap)
    followups = prompt.get("followups", [])
    followup_text = "\n".join(f"- {q}" for q in followups[:6])

    times = gap.get("times_asked", 0)
    times_label = f"(Asked {times} time{'s' if times != 1 else ''})" if times else "(New question)"

    return (
        f"**Topic:** {gap.get('topic', 'General')}\n\n"
        f"**{prompt.get('primary', gap.get('question', ''))}**\n\n"
        f"Follow-ups (answer any you can):\n{followup_text}\n\n"
        f"{times_label}\n\n"
        f"*Type your answer below, or click Skip to move on.*"
    )


def _save_interview_memo(
    topic: str, question: str, answer: str, gap_id: int, session_id: int
) -> Optional[Path]:
    """Save a Q&A memo file to docs/interview/ (same location as Discord interviews)."""
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        year_month = datetime.utcnow().strftime("%Y/%m")
        slug = _slugify(topic)

        # __file__ = admin/routers/inbox.py -> .parent x3 = LeisureLLM/
        docs_dir = Path(__file__).parent.parent.parent / "docs" / "interview" / year_month
        docs_dir.mkdir(parents=True, exist_ok=True)
        doc_path = docs_dir / f"{today}_{slug}.md"

        # Avoid overwriting — add a suffix if file exists
        counter = 1
        while doc_path.exists():
            counter += 1
            doc_path = docs_dir / f"{today}_{slug}_{counter}.md"

        content = (
            f"---\n"
            f"topic: {topic}\n"
            f"source: web_interview\n"
            f"session_id: {session_id}\n"
            f"gap_id: {gap_id}\n"
            f"date: {today}\n"
            f"---\n\n"
            f"## Question\n{question}\n\n"
            f"## Answer\n{answer}\n"
        )
        doc_path.write_text(content, encoding="utf-8")
        logger.info("Saved interview memo -> %s", doc_path)
        return doc_path
    except Exception as e:
        logger.error("Failed to save interview memo: %s", e)
        return None


# ── Background question processing ───────────────────────────────────────────

async def _process_question(thread_id: int, message: str, history: List[Dict[str, str]]):
    """Background task: run RAG pipeline and save result to inbox_messages."""
    from services.alpha_logging import log_alpha_event
    db = _get_db_direct()
    if not db:
        logger.error("No DB for background processing of thread %d", thread_id)
        return

    t0 = time.time()

    try:
        # ── 1. Retrieval ─────────────────────────────────────────────────
        await _update_thread_status(db, thread_id, "Searching knowledge base…")

        retriever = await _ensure_retriever()
        filtered = []
        sources: list = []
        chunk_source_paths: list = []
        context = ""

        if retriever and _vectorstore is not None:
            from cogs.LLM import (
                extract_source_citations,
                filter_superseded_docs,
                format_docs_for_context,
            )
            from services.hyde_retrieval import hyde_retrieve, make_generate_fn_from_router

            generate_fn = None
            try:
                from admin.dependencies import get_model_router
                mr = get_model_router()
                if mr:
                    generate_fn = make_generate_fn_from_router(mr)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            raw_docs = await hyde_retrieve(
                _vectorstore, message, generate_fn=generate_fn, k=20,
            )
            filtered = filter_superseded_docs(raw_docs)
            context = format_docs_for_context(filtered)
            try:
                from cogs.LLM import _context_matches_query
                if filtered and not _context_matches_query(message, filtered):
                    logger.info("Inbox: dropping context due to topic mismatch.")
                    filtered = []
                    context = ""
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
            sources = extract_source_citations(filtered)
            chunk_source_paths = list({
                (d.metadata.get("source_relpath") or d.metadata.get("source") or "")
                for d in filtered if d.metadata
            })
            chunk_source_paths = [p for p in chunk_source_paths if p]

        # ── 1b. Web augmentation (when local context is weak) ────────────
        _context_words = len(context.split()) if context else 0
        try:
            from cogs.LLM import _context_is_relevant, _needs_web_search

            sparse = _context_words < 80
            intent = _needs_web_search(message)
            weak_relevance = not _context_is_relevant(filtered)
            if sparse or intent or weak_relevance:
                wac_enabled = True
                try:
                    from core.config_loader import WorkflowConfig
                    _wf = WorkflowConfig.load()
                    wac_enabled = _wf.cq_web_chat_enabled
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

                if wac_enabled:
                    import os as _os

                    from services.secrets import get_secrets_manager
                    from services.tavily_service import TavilyService

                    secrets = get_secrets_manager()
                    tavily_key = _os.getenv("TAVILY_API_KEY") or secrets.get("tavily")
                    if tavily_key:
                        tavily = TavilyService(tavily_key)
                        if tavily.is_configured:
                            # Status: explain why we're searching the web
                            if weak_relevance and not sparse:
                                await _update_thread_status(db, thread_id, "Knowledge base didn't have a strong match — searching the web…")
                            elif intent:
                                await _update_thread_status(db, thread_id, "Looking up current information on the web…")
                            else:
                                await _update_thread_status(db, thread_id, "Limited local knowledge — searching the web…")

                            from services.web_research import chat_web_augment

                            web_block = await chat_web_augment(
                                tavily, message, max_results=4,
                            )
                            if web_block:
                                context = context + "\n\n" + web_block if context else web_block
                                logger.info(
                                    "Inbox web augmentation: added %d chars (RAG had %d words)",
                                    len(web_block), _context_words,
                                )

                                # Cache web results into corpus
                                try:
                                    from services.autonomous_research import cache_web_result
                                    asyncio.create_task(
                                        cache_web_result(
                                            question=message,
                                            web_block=web_block,
                                        )
                                    )
                                except Exception as e:
                                    logger.warning("operation: suppressed %s", e)
        except Exception as exc:
            logger.debug("Inbox web augmentation skipped: %s", exc)

        # ── 2. Pipeline ──────────────────────────────────────────────────
        await _update_thread_status(db, thread_id, "Running multi-model analysis pipeline…")

        reply_text = ""
        pipeline_stages: Optional[Dict] = None
        models_used: Optional[Dict] = None

        try:
            from admin.dependencies import get_model_router
            mr = get_model_router()
            if mr and mr.pipeline:
                from cogs.LLM import template as system_template

                sys_prompt = (
                    system_template
                    .replace("{context}", "")
                    .replace("{question}", "")
                    .replace("{history}", "")
                    .replace("{user}", "web-user")
                    .replace("{channel}", "web-inbox")
                )

                # Inject proactive suggestion context for agentic behaviour
                try:
                    from services.proactive_suggestions import get_proactive_engine
                    engine = get_proactive_engine(db)
                    nudge_ctx = await engine.build_nudge_context(
                        query=message, max_results=2,
                    )
                    if nudge_ctx:
                        sys_prompt += "\n" + nudge_ctx
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

                history_str = ""
                for h in history:
                    prefix = "User" if h.get("role") == "user" else "Assistant"
                    history_str += f"\n{prefix}: {h.get('content', '')}"

                full_context = context
                if history_str:
                    full_context = f"[Conversation History]\n{history_str}\n\n{full_context}"

                result = await mr.generate_pipeline(
                    user_prompt=message,
                    context=full_context,
                    system_prompt=sys_prompt,
                )
                reply_text = result["final"]
                pipeline_stages = result.get("stages")
                models_used = result.get("models_used")
        except Exception as e:
            logger.warning("Pipeline failed for thread %d: %s", thread_id, e)

        # ── 3. Fallback: single model ────────────────────────────────────
        if not reply_text:
            await _update_thread_status(db, thread_id, "Generating response…")
            try:

                # Try Ollama-compatible single-shot via model router
                from admin.dependencies import get_model_router
                mr = get_model_router()
                if mr and mr.clients:
                    backend_name = next(iter(mr.clients))
                    backend_cfg = mr.backends.get(backend_name)
                    # Skip embedding-only models (they don't support chat)
                    _EMBED_KEYWORDS = ("embed", "nomic-embed", "bge-", "e5-", "gte-")
                    chat_models = [
                        m for m in (backend_cfg.available_models or [])
                        if not any(k in m.lower() for k in _EMBED_KEYWORDS)
                    ]
                    default = backend_cfg.default_model
                    if default and any(k in default.lower() for k in _EMBED_KEYWORDS):
                        default = None  # ignore embedding model as default
                    model = default or (
                        chat_models[0] if chat_models else "llama3.3:70b-instruct-q4_K_M"
                    )
                    reply_text = await mr.generate_single(
                        backend_name=backend_name,
                        model=model,
                        prompt=f"Context:\n{context[:4000]}\n\nQuestion:\n{message}",
                        system_prompt="You are Magic Key Assistant, an operations harness. Answer based on the provided context, citing specifics. Lead with the answer, then reasoning. Surface risks or gaps. Suggest concrete next steps.",
                        temperature=0.3,
                    )
                else:
                    reply_text = "⚠️ No model backends configured. Please set up a backend in the Model Router settings."
            except Exception as e:
                logger.error("Single-model fallback failed for thread %d: %s", thread_id, e)
                reply_text = f"⚠️ Error generating response: {e}"

        elapsed_ms = int((time.time() - t0) * 1000)

        # ── 4. Save result ───────────────────────────────────────────────
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO inbox_messages
                   (thread_id, role, content, sources_json, chunk_sources_json,
                    pipeline_stages_json, models_used_json, processing_time_ms)
                   VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?)""",
                (
                    thread_id,
                    reply_text,
                    json.dumps(sources) if sources else None,
                    json.dumps(chunk_source_paths) if chunk_source_paths else None,
                    json.dumps(pipeline_stages) if pipeline_stages else None,
                    json.dumps(models_used) if models_used else None,
                    elapsed_ms,
                ),
            )
            await conn.execute(
                """UPDATE inbox_threads
                   SET status = 'ready', processing_status = NULL,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (thread_id,),
            )
            await conn.commit()

        log_alpha_event(
            "inbox_thread_processed",
            {
                "thread_id": thread_id,
                "message": message,
                "history_len": len(history or []),
                "reply": reply_text,
                "reply_len": len(reply_text or ""),
                "pipeline_stages": pipeline_stages,
                "models_used": models_used,
                "sources_count": len(sources or []),
                "context_words": len(context.split()) if context else 0,
                "elapsed_ms": elapsed_ms,
            },
        )

        logger.info("Thread %d processed in %dms", thread_id, elapsed_ms)

    except Exception as e:
        log_alpha_event(
            "inbox_thread_error",
            {
                "thread_id": thread_id,
                "message": message,
                "history_len": len(history or []),
                "error": str(e),
            },
        )
        logger.error("Background processing failed for thread %d: %s", thread_id, e, exc_info=True)
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'assistant', ?)",
                    (thread_id, f"⚠️ Processing failed: {e}"),
                )
                await conn.execute(
                    "UPDATE inbox_threads SET status = 'ready', processing_status = NULL WHERE id = ?",
                    (thread_id,),
                )
                await conn.commit()
            from core.services.work_packet_service import WorkPacketService

            svc = WorkPacketService(db)
            packet = await svc.get_by_key(_inbox_packet_key(thread_id))
            if packet:
                await svc.transition(
                    packet["id"],
                    status="failed",
                    lane="assistive",
                    current_summary="Inbox processing failed.",
                    terminal_reason=str(e),
                    event_type="packet_failed",
                    actor_kind="system",
                    actor_ref="inbox",
                    summary="Inbox processing failed.",
                    related_inbox_thread_id=thread_id,
                    requires_confirmation=False,
                    confirmation_status="not_required",
                )
        except Exception as e:
            logger.warning("operation: suppressed %s", e)


async def _update_thread_status(db, thread_id: int, status_text: str):
    """Update the processing_status field for live progress display."""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE inbox_threads SET processing_status = ?, updated_at = datetime('now') WHERE id = ?",
                (status_text, thread_id),
            )
            await conn.commit()
    except Exception as e:
        logger.warning("_update_thread_status: suppressed %s", e)
