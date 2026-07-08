"""
Chat API router — streaming web chat with the same RAG quality as Discord /ask,
plus source citations, feedback, the 3-phase model-router pipeline, and
**agentic tool-calling** for bounded artifact operations.

The chat is MKA's primary command surface: users can ask questions (RAG)
or give instructions ("create an action for …") and the LLM routes to
the appropriate tool, with a confirmation gate for mutations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from services.interaction_memory import _extract_keywords

from admin.dependencies import get_current_actor, get_db, require_admin

logger = logging.getLogger("AdminServer.chat")
router = APIRouter(tags=["chat"], dependencies=[Depends(require_admin)])


def _resolve_actor(actor):
    if hasattr(actor, "actor_kind") and hasattr(actor, "stable_id"):
        return actor
    return SimpleNamespace(
        actor_kind="system",
        stable_id="actor_chat_fallback",
        external_ref="chat-fallback",
        display_name="Chat Service",
        username="chat-service",
        account_id=0,
    )


def _actor_display_name(actor) -> str:
    return str(actor.display_name or actor.username or actor.external_ref)


# ── Gap indicators (shared source of truth in rag_pipeline) ──────────────────

_GAP_INDICATORS: list[str] = [
    "i don't have information",
    "i'm not sure",
    "i don't know",
    "i can't find",
    "no information about",
    "unclear from",
    "not documented",
    "no docs on that",
    "knowledge base doesn't cover",
    "don't have docs",
    "no relevant documents",
    "not in the knowledge base",
    "gap in the knowledge base",
]

_SPARSE_CONTEXT_THRESHOLD = 50  # word count
_ASSISTIVE_RETRIEVAL_CONTEXT_MAX_CHARS = 5000
_DEEP_RETRIEVAL_CONTEXT_MAX_CHARS = 28000
_ASSISTIVE_GENERATION_CONTEXT_MAX_CHARS = 5000
_ASSISTIVE_PROMPT_FRAGMENT_MAX_CHARS = 1200
_DEEP_PROMPT_FRAGMENT_MAX_CHARS = 3000
_ASSISTIVE_GENERATION_MAX_TOKENS = 900
_ASSISTIVE_LOCAL_MODEL_OVERRIDE_ENV = "ASSISTIVE_LOCAL_MODEL_OVERRIDE"
_ASSISTIVE_LOCAL_BACKEND_OVERRIDE_ENV = "ASSISTIVE_LOCAL_BACKEND_OVERRIDE"

_ARTIFACT_FAILURE_ROUTED_TO_ASSISTIVE = "artifact_routed_to_assistive"
_ARTIFACT_FAILURE_PLAN_NOT_EXECUTED = "artifact_tool_plan_not_executed"
_ARTIFACT_FAILURE_CONFIRMATION_DECLINED = "artifact_confirmation_declined"
_ARTIFACT_FAILURE_WRITE_FAILED = "artifact_write_failed"
_ARTIFACT_FAILURE_REF_MISSING = "artifact_created_ref_missing"
_ARTIFACT_FAILURE_NULL_OUTCOME = "artifact_null_outcome"

_MAX_HISTORY_TURNS = 8
_HISTORY_SUMMARY_MAX_CHARS = 800

_TOPIC_RESET_CLUES = [
    "we're talking about",
    "we are talking about",
    "not talking about",
    "not about",
    "i mean",
    "i meant",
    "no, i mean",
    "no i mean",
    "actually, i meant",
    "correction",
    "wrong topic",
    "different topic",
    "not that",
]


def _detect_topic_reset(text: str) -> bool:
    low = (text or "").lower()
    if any(clue in low for clue in _TOPIC_RESET_CLUES):
        return True
    if re.search(r"\bnot\s+\w+\s+but\b", low):
        return True
    return False


def _summarize_history_messages(messages: List["ChatMessage"]) -> str:
    if not messages:
        return ""

    text = " ".join(m.content for m in messages if m.content)
    topics = _extract_keywords(text, max_keywords=6)
    parts = []
    if topics:
        parts.append("Topics: " + ", ".join(topics))

    last_user = next((m for m in reversed(messages) if m.role == "user"), None)
    if last_user:
        parts.append("Last user: " + last_user.content.strip()[:160])

    last_assistant = next((m for m in reversed(messages) if m.role == "assistant"), None)
    if last_assistant:
        parts.append("Last assistant: " + last_assistant.content.strip()[:160])

    summary = " | ".join(parts).strip()
    return summary[:_HISTORY_SUMMARY_MAX_CHARS]


def _trim_generation_context(text: str, lane: str) -> str:
    if not text or lane != "assistive":
        return text
    if len(text) <= _ASSISTIVE_GENERATION_CONTEXT_MAX_CHARS:
        return text
    return text[:_ASSISTIVE_GENERATION_CONTEXT_MAX_CHARS].rstrip()


def _resolve_assistive_backend_and_model(initial_cfg) -> tuple[str, str]:
    backend_name = os.getenv(_ASSISTIVE_LOCAL_BACKEND_OVERRIDE_ENV) or initial_cfg.backend_name
    model = os.getenv(_ASSISTIVE_LOCAL_MODEL_OVERRIDE_ENV) or initial_cfg.model
    return backend_name, model


def _new_artifact_funnel(*, artifact_request: bool, lane: str) -> dict[str, Any]:
    return {
        "intent_detected": artifact_request,
        "lane_selected": lane if artifact_request else None,
        "tool_selected": False,
        "tool_name": None,
        "confirmation_requested": False,
        "write_attempted": False,
        "artifact_row_created": False,
        "artifact_ref_emitted": False,
        "terminal_state": None,
    }


def _build_history_string(messages: List["ChatMessage"], topic_reset: bool) -> str:
    if topic_reset or not messages:
        return ""

    if len(messages) <= _MAX_HISTORY_TURNS:
        lines = []
        for msg in messages:
            prefix = "User" if msg.role == "user" else "Assistant"
            lines.append(f"{prefix}: {msg.content}")
        return "\n".join(lines)

    earlier = messages[:-_MAX_HISTORY_TURNS]
    recent = messages[-_MAX_HISTORY_TURNS:]
    summary = _summarize_history_messages(earlier)
    parts = []
    if summary:
        parts.append("[Earlier Summary]\n" + summary)
    for msg in recent:
        prefix = "User" if msg.role == "user" else "Assistant"
        parts.append(f"{prefix}: {msg.content}")
    return "\n".join(parts)


async def _maybe_log_knowledge_gap(
    db,
    question: str,
    reply_text: str,
    context_word_count: int,
    doc_count: int,
    context: str = "",
) -> Tuple[Optional[int], Optional[Any]]:
    """Auto-detect and log a knowledge gap from a web chat response.

    Uses LLM self-assessment when available, falls back to heuristic detection.
    Returns ``(gap_id, assessment)`` — gap_id is None if no gap was created,
    and assessment is always the SelfAssessmentResult (or None on error).
    """
    try:
        from services.answer_self_assessment import (
            assess_answer_quality,
            find_near_misses,
            format_near_misses_for_context,
        )

        assessment = await assess_answer_quality(
            question=question,
            response=reply_text,
            context=context,
        )

        if not assessment.gap_detected:
            return None, assessment

        from cogs.KnowledgeGapTracker import classify_gap_curation

        topic = (
            assessment.suggested_topic
            or " ".join(w for w in question.split()[:8] if len(w) > 3)
            or question[:60]
        )

        # Near-miss retrieval
        near_misses = await find_near_misses(question)
        near_miss_text = format_near_misses_for_context(near_misses)

        gap_context = (
            f"Auto-detected from web chat | "
            f"Self-assessed confidence: {assessment.confidence}/10 | "
            f"Grounded: {assessment.grounded} | "
            f"Context quality: {context_word_count} words | "
            f"Retrieved: {doc_count} docs"
        )
        if assessment.missing_knowledge:
            gap_context += f"\nMissing: {assessment.missing_knowledge}"
        if near_miss_text:
            gap_context += f"\n{near_miss_text}"

        cur_status, cur_reason = classify_gap_curation(topic, question, gap_context)

        async with db.acquire() as conn:
            # Check for existing open gap with matching question
            async with conn.execute(
                "SELECT id, times_asked FROM knowledge_gaps WHERE question = ? AND status = 'open'",
                (question,),
            ) as cursor:
                existing = await cursor.fetchone()

            if existing:
                gap_id = existing[0]
                await conn.execute(
                    """UPDATE knowledge_gaps
                       SET times_asked = times_asked + 1,
                           last_asked = datetime('now'),
                           priority_score = times_asked + 1
                       WHERE id = ?""",
                    (gap_id,),
                )
                await conn.commit()
                logger.info("Updated knowledge gap #%d from web chat (asked %d times)", gap_id, existing[1] + 1)
                return gap_id, assessment
            else:
                try:
                    async with conn.execute(
                        """INSERT INTO knowledge_gaps
                              (topic, question, context, priority_score,
                               curation_status, curation_reason, times_asked)
                           VALUES (?, ?, ?, ?, ?, ?, 1)""",
                        (topic, question, gap_context, 1 if cur_status == "keep" else 0,
                         cur_status, cur_reason or None),
                    ) as cur:
                        gap_id = cur.lastrowid
                except Exception:
                    # Backward-compatible insert (older schema)
                    async with conn.execute(
                        """INSERT INTO knowledge_gaps
                              (topic, question, context, priority_score, times_asked)
                           VALUES (?, ?, ?, ?, 1)""",
                        (topic, question, gap_context, 1 if cur_status == "keep" else 0),
                    ) as cur:
                        gap_id = cur.lastrowid
                await conn.commit()
                logger.info("Created knowledge gap #%d from web chat (confidence %d/10): %s",
                            gap_id, assessment.confidence, topic[:50])
                return gap_id, assessment
    except Exception as exc:
        logger.warning("Self-assessment gap detection failed (non-fatal): %s", exc)
        return None, None

# ── Lazy-loaded tool registry (built once, shared across requests) ───────────

_tool_registry = None
_tool_registry_lock = asyncio.Lock()


async def _ensure_tool_registry():
    """Lazy-load the tool registry on first agentic chat request."""
    global _tool_registry
    if _tool_registry is not None:
        return _tool_registry
    async with _tool_registry_lock:
        if _tool_registry is not None:
            return _tool_registry
        try:
            from core.tools_builtin import build_default_registry
            _tool_registry = build_default_registry()
            logger.info("Tool registry initialised: %d tools", _tool_registry.tool_count)
        except Exception as exc:
            logger.error("Failed to initialise tool registry: %s", exc)
            return None
    return _tool_registry


def _load_workflows_config() -> Optional[Dict[str, Any]]:
    """Load workflows.yaml as a flat dict for tool gating."""
    try:
        import yaml
        wf_path = Path(__file__).resolve().parent.parent.parent / "config" / "workflows.yaml"
        if wf_path.exists():
            with open(wf_path, "r") as f:
                return yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Could not load workflows config: %s", exc)
    return None

# ── Lazy-loaded vectorstore & retriever (shared across requests) ─────────────

_vectorstore = None
_retriever = None
_retriever_init_lock = asyncio.Lock()


async def _ensure_retriever():
    """Lazy-load the ChromaDB vectorstore and retriever on first chat request."""
    global _retriever, _vectorstore

    if _retriever is not None:
        return _retriever

    async with _retriever_init_lock:
        if _retriever is not None:
            return _retriever

        try:
            from core.chroma_factory import get_vectorstore

            _vectorstore = get_vectorstore()
            _retriever = _vectorstore.as_retriever(
                search_kwargs={
                    "k": 20,
                    "filter": {
                        "$and": [
                            {"archived": {"$ne": True}},
                            {"superseded_by": {"$eq": ""}},
                        ]
                    },
                },
            )
            logger.info("Chat retriever initialised (Chroma, k=20, archived/superseded filtered)")
        except Exception as exc:
            logger.error("Failed to initialise chat retriever: %s", exc)
            return None

    return _retriever


# ── Request / Response models ─────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str = Field(..., max_length=20_000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10_000)
    history: List[ChatMessage] = Field(default_factory=list, max_length=50)
    conversation_id: Optional[str] = Field(None, max_length=64)


class ChatResponse(BaseModel):
    reply: str
    conversation_id: Optional[str] = None
    sources: List[str] = []


class FeedbackRequest(BaseModel):
    question: str = Field(..., max_length=10_000)
    answer: str = Field(..., max_length=50_000)
    feedback: str  # "helpful" or "not_helpful"
    chunk_sources: List[str] = Field(default_factory=list, max_length=50)


class ToolConfirmRequest(BaseModel):
    """User confirms (or rejects) a pending tool execution."""
    tool_name: str
    arguments: Dict[str, Any]
    confirmed: bool = True
    request_id: Optional[str] = Field(None, max_length=64)


# ── SSE helper ────────────────────────────────────────────────────────────────


def _sse(event_type: str, data) -> str:
    """Format a single Server-Sent Event line."""
    payload = json.dumps({"type": event_type, "content": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


def _truncate_prompt_fragment(text: str, max_chars: int) -> str:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 16].rstrip() + "\n[…truncated]"


def _derive_artifact_subject(query: str) -> str:
    cleaned = re.sub(
        r"^(create|draft|write|prepare|build|generate)\s+(a|an|the)?\s*",
        "",
        (query or "").strip(),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(short\s+)?(action item|decision record|memo|brief|report|outline|proposal|plan|spec|strategy)\s+(for|about|on)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip().rstrip(". ")
    return cleaned[:180]


def _build_artifact_document_content(query: str, reply_text: str) -> str:
    reply = (reply_text or "").strip()
    if reply:
        return reply

    subject = _derive_artifact_subject(query) or "Requested artifact"
    request_text = (query or "").strip().rstrip(".") or subject
    return "\n".join(
        [
            f"# {subject[:180]}",
            "",
            "## Requested Outcome",
            request_text,
            "",
            "## Draft Notes",
            "This artifact was created from the user's request after the chat path did not emit a complete draft. Expand or refine it with operational details as needed.",
        ]
    ).strip()


def _build_artifact_tool_fallback(query: str, reply_text: str) -> Optional[Dict[str, Any]]:
    low = (query or "").lower()
    subject = _derive_artifact_subject(query)
    reply = _build_artifact_document_content(query, reply_text)

    if "action item" in low:
        title = subject or (query or "").strip().rstrip(".")
        return {
            "name": "create_action",
            "arguments": {
                "title": title[:180],
                "description": reply[:500] if reply else "",
                "priority": "medium",
            },
        }

    if "decision record" in low:
        decision_text = subject or (query or "").strip().rstrip(".")
        title = decision_text[:200]
        if title and not title.lower().startswith("decision"):
            title = f"Decision: {title}"
        return {
            "name": "create_decision",
            "arguments": {
                "title": title[:200],
                "decision": decision_text[:1000],
                "rationale": reply[:500] if reply else "",
            },
        }

    if any(term in low for term in ("memo", "brief", "report", "outline", "proposal", "plan", "spec", "strategy")):
        title = subject or (query or "Document").strip().rstrip(".")
        return {
            "name": "create_document",
            "arguments": {
                "title": title[:180],
                "content": reply,
                "category": "chat_artifact",
            },
        }

    return None


# ── Proactive nudges endpoint ────────────────────────────────────────────────


@router.get("/api/v1/chat/nudges", dependencies=[Depends(require_admin)])
async def api_get_nudges(db=Depends(get_db)):
    """Return current proactive suggestions (max 2) for display in the UI."""
    try:
        # Check config gate
        wf = _load_workflows_config()
        if wf:
            ps_cfg = (wf.get("memory") or {}).get("proactive_suggestions") or {}
            if not ps_cfg.get("enabled", True):
                return {"nudges": []}

        from services.proactive_suggestions import get_proactive_engine
        engine = get_proactive_engine(db)
        nudges = await engine.get_suggestions(max_results=2)
        return {
            "nudges": engine.format_nudges_for_display(nudges),
        }
    except Exception as exc:
        logger.debug("Nudge endpoint failed: %s", exc)
        return {"nudges": []}


# ── Streaming chat endpoint ──────────────────────────────────────────────────


@router.post("/api/v1/chat/stream", dependencies=[Depends(require_admin)])
async def api_chat_stream(payload: ChatRequest):
    """
    SSE-streaming chat with agentic tool-calling.  Events:
      status              - progress text (searching, analysing...)
      token               - a chunk of the reply
      sources             - list of source citations
      tool_call           - LLM wants to invoke a tool (read-only: auto-executed)
      tool_confirmation   - LLM wants to invoke a MUTATING tool (needs user OK)
      tool_result         - result of an executed tool
      knowledge_gap       - auto-detected gap logged for follow-up
      done                - stream finished
    """
    return StreamingResponse(
        _stream_response(payload),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


async def _stream_response(payload: ChatRequest):
    """Async generator that yields SSE events, including agentic tool calls."""
    from services.agentic_chat import (
        build_confirmation_event,
        build_tool_system_prompt,
        extract_tool_call,
        run_planning_loop,
    )
    from services.alpha_logging import log_alpha_event
    from services.chat_policy import (
        AuxCallBudget,
        async_enhance_policy,
        decide_chat_policy,
        should_escalate_after_retrieval,
    )
    from services.chat_telemetry import ChatRequestTelemetry, build_local_only_blocked_reply
    from services.hyde_retrieval import hyde_retrieve, make_generate_fn_from_router
    from services.interaction_memory import ConversationStore, InteractionMemory
    from services.knowledge_health import apply_confidence_decay_to_results
    from services.rag_pipeline import (
        count_trusted_candidates,
        extract_source_citations,
        filter_superseded_docs,
        format_docs_for_context,
        promote_trusted_candidates,
        run_retriever_query,
    )
    from services.rag_pipeline import (
        get_pipeline_router as _get_pipeline_router,
    )
    from services.rag_pipeline import (
        prompt as llm_prompt,
    )
    from services.rag_pipeline import (
        template as system_template,
    )
    from services.request_tracing import new_request_id, persist_request_trace
    from services.response_cache import get_response_cache
    from services.retrieval_debug_log import retrieval_logger

    from config import (
        CHAT_ADAPTIVE_GATING_ENABLED,
        CHAT_MAX_AUX_RETRIEVAL_CALLS,
        CHAT_MAX_GENERATION_STAGES,
        CHAT_SIMPLE_QUERY_WORD_THRESHOLD,
        LOCAL_LLM_ONLY,
        gpt_key,
    )

    # Tracking accumulators for interaction memory
    start_ts = time.time()
    alpha_logged = False
    _tools_invoked: List[str] = []
    _artifact_refs: List[str] = []
    _sources_used: List[str] = []
    _response_summary: str = ""
    _full_reply: str = ""  # accumulated for gap detection
    _models_used: Optional[Dict[str, Any]] = None
    _pipeline_stages: Dict[str, Any] = {}
    _pipeline_used: bool = False
    _context_words: int = 0
    _web_searched = False
    _retrieval_calls = 0
    _background_jobs_spawned: List[str] = []
    _cache_context_for_trace = ""
    _escalated_after_retrieval = False
    _tool_confirmation_pending = False
    _pre_policy = locals().get("policy")
    _artifact_funnel = _new_artifact_funnel(
        artifact_request=getattr(_pre_policy, "artifact_request", False),
        lane="assistive",
    )
    _artifact_failure_mode: Optional[str] = None
    _artifact_completed_successfully = True
    _artifact_funnel_logged = False
    request_id = new_request_id()
    request_db = None
    _request_trace_saved = False
    topic_reset = _detect_topic_reset(payload.message)
    telemetry = ChatRequestTelemetry(payload.message)
    policy = decide_chat_policy(
        payload.message,
        adaptive_enabled=CHAT_ADAPTIVE_GATING_ENABLED,
        max_aux_retrieval_calls=CHAT_MAX_AUX_RETRIEVAL_CALLS,
        max_generation_stages=CHAT_MAX_GENERATION_STAGES,
        simple_query_word_threshold=CHAT_SIMPLE_QUERY_WORD_THRESHOLD,
    )
    policy = await async_enhance_policy(policy, payload.message)
    aux_budget = AuxCallBudget(policy.max_aux_retrieval_calls)
    telemetry.policy.complexity = policy.complexity
    telemetry.policy.reason = policy.reason
    telemetry.policy.max_aux_retrieval_calls = policy.max_aux_retrieval_calls
    telemetry.policy.max_generation_stages = policy.max_generation_stages
    _trace = retrieval_logger.start_trace(payload.message, entry_point="web_chat")
    _artifact_funnel = _new_artifact_funnel(artifact_request=policy.artifact_request, lane=policy.lane)

    def _mark_artifact_tool_selected(tool_name: str, *, confirmation_requested: bool = False) -> None:
        if not policy.artifact_request:
            return
        _artifact_funnel["tool_selected"] = True
        _artifact_funnel["tool_name"] = tool_name
        if confirmation_requested:
            _artifact_funnel["confirmation_requested"] = True

    def _mark_artifact_write_attempt(*, success: bool, artifact_refs: Optional[List[str]] = None) -> None:
        nonlocal _artifact_failure_mode, _artifact_completed_successfully
        if not policy.artifact_request:
            return
        _artifact_funnel["write_attempted"] = True
        _artifact_funnel["artifact_row_created"] = bool(success)
        refs = list(artifact_refs or [])
        _artifact_funnel["artifact_ref_emitted"] = bool(refs)
        if success and refs:
            _artifact_funnel["terminal_state"] = "artifact_created"
            _artifact_failure_mode = None
            _artifact_completed_successfully = True
        elif success:
            _artifact_funnel["terminal_state"] = "artifact_created_ref_missing"
            _artifact_failure_mode = _ARTIFACT_FAILURE_REF_MISSING
            _artifact_completed_successfully = False
        else:
            _artifact_funnel["terminal_state"] = "write_failed"
            _artifact_failure_mode = _ARTIFACT_FAILURE_WRITE_FAILED
            _artifact_completed_successfully = False

    def _mark_artifact_terminal(state: str, *, failure_mode: Optional[str] = None, completed_successfully: bool = True) -> None:
        nonlocal _artifact_failure_mode, _artifact_completed_successfully
        if not policy.artifact_request:
            return
        _artifact_funnel["terminal_state"] = state
        _artifact_failure_mode = failure_mode
        _artifact_completed_successfully = completed_successfully

    def _log_artifact_funnel_stage() -> None:
        nonlocal _artifact_funnel_logged
        if _artifact_funnel_logged or not policy.artifact_request:
            return
        _trace.log_stage("artifact_funnel", detail=dict(_artifact_funnel), doc_count=0)
        _artifact_funnel_logged = True

    if policy.artifact_request and policy.lane != "deep":
        _mark_artifact_terminal(
            "routed_to_assistive",
            failure_mode=_ARTIFACT_FAILURE_ROUTED_TO_ASSISTIVE,
            completed_successfully=False,
        )

    def _log_alpha(error: Optional[str] = None) -> None:
        nonlocal alpha_logged
        if alpha_logged:
            return
        alpha_logged = True
        reply_text = _full_reply or _response_summary
        telemetry.context_words = _context_words
        telemetry_payload = telemetry.to_log_payload()
        log_alpha_event(
            "chat_stream",
            {
                "message": payload.message,
                "history_len": len(payload.history or []),
                "pipeline_used": _pipeline_used,
                "models_used": _models_used,
                "tools_invoked": _tools_invoked,
                "sources_count": len(_sources_used or []),
                **telemetry_payload,
                "reply": reply_text,
                "reply_len": len(reply_text or ""),
                "elapsed_ms": telemetry_payload["latency"]["total_ms"],
                "error": error,
            },
        )

    def _schedule_background(coro: Any, job_name: str) -> None:
        _background_jobs_spawned.append(job_name)
        task = asyncio.create_task(coro)

        def _report_background_result(done_task: asyncio.Task) -> None:
            try:
                done_task.result()
            except Exception as exc:
                logger.warning("Background task %s failed: %s", job_name, exc)

        task.add_done_callback(_report_background_result)

    async def _lookup_job_run_id(job_name: str, run_key: str) -> Optional[int]:
        if request_db is None:
            return None
        try:
            async with request_db.acquire() as conn, conn.execute(
                """SELECT id FROM job_runs
                       WHERE job_name = ? AND run_date = ?
                       ORDER BY started_at DESC LIMIT 1""",
                (job_name, run_key),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            return int(row[0])
        except Exception as exc:
            logger.debug("Job run lookup failed for %s: %s", job_name, exc)
            return None

    async def _schedule_durable_background(
        *,
        coro: Any,
        job_name: str,
        title: str,
        objective: str,
        next_step: str,
        start_summary: str,
        success_summary_builder,
        failure_summary: str,
    ) -> None:
        if request_db is None:
            _schedule_background(coro, job_name)
            return

        from core.services.work_packet_service import WorkPacketService

        packet_id: Optional[int] = None
        run_key = f"{request_id}:{job_name}"
        job_run_id: Optional[int] = None

        try:
            svc = WorkPacketService(request_db)
            packet = await svc.create_packet(
                packet_key=f"chat-bg:{request_id}:{job_name}",
                packet_type="chat_background_job",
                title=title,
                objective=objective,
                status="active",
                lane="maintenance",
                owner_kind="system",
                owner_ref="chat",
                next_step=next_step,
                current_summary=start_summary,
                created_from_type="chat_request",
                created_from_id=request_id,
                actor_kind="system",
                actor_ref="chat",
                summary=start_summary,
            )
            packet_id = packet.get("id")
            if packet_id is not None:
                await svc.ensure_link(
                    packet_id,
                    link_role="source_request",
                    target_type="request_trace",
                    target_id=request_id,
                    is_primary=True,
                    note=f"Originated from chat request {request_id}.",
                    actor_kind="system",
                    actor_ref="chat",
                )
        except Exception as exc:
            logger.warning("Failed to create durable packet for %s: %s", job_name, exc)

        try:
            if hasattr(request_db, "record_job_run"):
                await request_db.record_job_run(job_name, run_key)
                job_run_id = await _lookup_job_run_id(job_name, run_key)
        except Exception as exc:
            logger.warning("Failed to record job run for %s: %s", job_name, exc)

        async def _runner() -> None:
            try:
                result = await coro
                if packet_id is not None:
                    try:
                        svc = WorkPacketService(request_db)
                        summary = success_summary_builder(result)
                        await svc.transition(
                            packet_id,
                            status="completed",
                            lane="maintenance",
                            approval_required=False,
                            approval_status="not_required",
                            completion_summary=summary,
                            terminal_reason="job_completed",
                            event_type="packet_completed",
                            actor_kind="system",
                            actor_ref="chat",
                            summary=summary,
                            related_job_run_id=job_run_id,
                            requires_confirmation=False,
                            confirmation_status="not_required",
                        )
                    except Exception as exc:
                        logger.warning("Failed to complete packet for %s: %s", job_name, exc)
                if hasattr(request_db, "complete_job_run"):
                    await request_db.complete_job_run(job_name, run_key)
            except Exception as exc:
                logger.warning("Background task %s failed: %s", job_name, exc)
                if packet_id is not None:
                    try:
                        svc = WorkPacketService(request_db)
                        await svc.transition(
                            packet_id,
                            status="failed",
                            lane="maintenance",
                            approval_required=False,
                            approval_status="not_required",
                            completion_summary=failure_summary,
                            terminal_reason="job_failed",
                            event_type="packet_failed",
                            actor_kind="system",
                            actor_ref="chat",
                            summary=f"{failure_summary}: {exc}",
                            related_job_run_id=job_run_id,
                            requires_confirmation=False,
                            confirmation_status="not_required",
                        )
                    except Exception as packet_exc:
                        logger.warning("Failed to mark packet failed for %s: %s", job_name, packet_exc)
                if hasattr(request_db, "complete_job_run"):
                    try:
                        await request_db.complete_job_run(job_name, run_key, error=str(exc))
                    except Exception as complete_exc:
                        logger.warning("Failed to mark job run failed for %s: %s", job_name, complete_exc)

        _background_jobs_spawned.append(job_name)
        task = asyncio.create_task(_runner())

        def _report_durable_background_result(done_task: asyncio.Task) -> None:
            try:
                done_task.result()
            except Exception as exc:
                logger.warning("Durable background wrapper %s failed: %s", job_name, exc)

        task.add_done_callback(_report_durable_background_result)

    async def _persist_request_trace_once(
        *,
        failure_mode: Optional[str] = None,
        completed_successfully: bool = True,
    ) -> None:
        nonlocal _request_trace_saved
        if _request_trace_saved or request_db is None:
            return
        effective_failure_mode = failure_mode if failure_mode is not None else _artifact_failure_mode
        effective_completed_successfully = (
            completed_successfully if failure_mode is not None else _artifact_completed_successfully
        )
        _log_artifact_funnel_stage()
        telemetry.finish()
        persisted = await persist_request_trace(
            request_db,
            request_id=request_id,
            trace_id=_trace.trace_id,
            started_at=start_ts,
            finished_at=time.time(),
            entrypoint="web_chat",
            route_name="/api/v1/chat/stream",
            user_visible_flow="chat_stream",
            lane=policy.lane,
            query_text=payload.message,
            conversation_id=session_id,
            used_cache=telemetry.cache_hit,
            retrieval_used=bool(filtered),
            retrieval_doc_count=len(filtered),
            context_word_count=_context_words,
            web_augmented=_web_searched,
            llm_calls=telemetry.llm_calls,
            retrieval_calls=_retrieval_calls,
            tool_calls=len(_tools_invoked),
            models_used=_models_used,
            pipeline_stages=_pipeline_stages,
            first_token_ms=telemetry.latency.first_token_ms,
            retrieval_ms=telemetry.latency.retrieval_ms,
            generation_ms=telemetry.latency.generation_ms,
            total_ms=telemetry.latency.total_ms,
            input_tokens_est=telemetry.tokens.input_tokens_est,
            output_tokens_est=telemetry.tokens.output_tokens_est,
            total_tokens_est=telemetry.tokens.total_tokens_est,
            policy_reason=policy.reason,
            routing_flags={
                "explicit_deep_intent": policy.explicit_deep_intent,
                "artifact_request": policy.artifact_request,
                "multi_source_requirement": policy.multi_source_requirement,
                "high_confidence_required": policy.high_confidence_required,
                "deep_mode_requested": policy.deep_mode_requested,
                "escalated_after_retrieval": _escalated_after_retrieval,
                "artifact_funnel": _artifact_funnel,
            },
            failure_mode=effective_failure_mode,
            completed_successfully=effective_completed_successfully,
            artifact_refs=_artifact_refs,
            source_count=len(_sources_used),
            background_jobs_spawned=_background_jobs_spawned,
            stage_events=_trace.stages,
            cache_key_context=_cache_context_for_trace,
        )
        if persisted:
            _request_trace_saved = True
        else:
            logger.error("Request trace %s was not persisted on the active database path", request_id)

    # ── 0. Initialise tool registry & config ─────────────────────────────
    registry = await _ensure_tool_registry()
    workflows_config = _load_workflows_config()

    # ── 0b. Interaction memory ───────────────────────────────────────────
    memory: Optional[InteractionMemory] = None
    concern_context = ""
    workspace_context = ""
    memory_context = ""
    preference_context = ""
    proactive_context = ""
    _proactive_nudges: List[Dict[str, str]] = []
    conv_store: Optional[ConversationStore] = None
    session_id: Optional[str] = None
    filtered: List[Any] = []
    try:
        from admin.dependencies import get_db_optional
        db_inst = get_db_optional()
        if db_inst:
            request_db = db_inst
            memory = InteractionMemory(db_inst)

            # ── Parallel context fetches ─────────────────────────────
            # These DB/service calls are independent — run concurrently
            # instead of sequentially to shave significant latency.

            async def _concern_ctx():
                try:
                    return await memory.build_concern_context(payload.message)
                except Exception:
                    return ""

            async def _workspace_ctx():
                try:
                    from services.workspace_context import get_workspace_context_builder
                    wb = get_workspace_context_builder(db_inst)
                    return await wb.get_context()
                except Exception as exc:
                    logger.debug("Workspace context unavailable: %s", exc)
                    return ""

            async def _preference_ctx():
                try:
                    from services.user_preferences import UserPreferenceService
                    ps = UserPreferenceService(db_inst)
                    return await ps.build_preference_prompt("admin")
                except Exception as exc:
                    logger.debug("Preference context unavailable: %s", exc)
                    return ""

            async def _memory_ctx():
                try:
                    return await ConversationStore(db_inst).build_memory_context(payload.message)
                except Exception:
                    return ""

            async def _proactive_ctx():
                try:
                    _ps_enabled = True
                    if workflows_config:
                        _ps_cfg = (workflows_config.get("memory") or {}).get("proactive_suggestions") or {}
                        _ps_enabled = _ps_cfg.get("enabled", True)
                    if not _ps_enabled:
                        return "", []
                    from services.proactive_suggestions import get_proactive_engine
                    eng = get_proactive_engine(db_inst)
                    ctx, nudges = await asyncio.gather(
                        eng.build_nudge_context(query=payload.message, max_results=2),
                        eng.get_suggestions(query=payload.message, max_results=2),
                    )
                    fmtd = eng.format_nudges_for_display(nudges) if nudges else []
                    return ctx, fmtd
                except Exception as exc:
                    logger.debug("Proactive suggestions unavailable: %s", exc)
                    return "", []

            # Fire all context fetches at once
            (
                concern_context,
                workspace_context,
                preference_context,
                memory_context,
                _proactive_tuple,
            ) = await asyncio.gather(
                _concern_ctx(),
                _workspace_ctx(),
                _preference_ctx(),
                _memory_ctx(),
                _proactive_ctx(),
            )
            proactive_context, _proactive_nudges = _proactive_tuple

            # Conversation store session (sequential — add_turn depends on session_id)
            try:
                conv_store = ConversationStore(db_inst)
                session_id = await conv_store.get_or_create_session(payload.conversation_id)
                await conv_store.add_turn(session_id, "user", payload.message)
            except Exception as exc:
                logger.debug("Conversation store unavailable: %s", exc)
    except Exception as exc:
        logger.debug("Interaction memory unavailable: %s", exc)

    # ── 1. HyDE retrieval ────────────────────────────────────────────────
    yield _sse("status", "Searching knowledge base…")
    retriever = await _ensure_retriever()
    generate_fn = None
    if retriever and _vectorstore is not None:
        # Build hypothesis generator from pipeline router
        hyde_generate_fn = None
        try:
            pipeline_router = await _get_pipeline_router()
            if pipeline_router:
                generate_fn = make_generate_fn_from_router(pipeline_router)
                if generate_fn and aux_budget.try_consume("hyde_hypothesis"):
                    hyde_generate_fn = generate_fn
                    telemetry.llm_calls += 1
                else:
                    _trace.log_stage("hyde_hypothesis", detail={"skipped": True, "reason": "aux_budget_exhausted"})
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        # ── Query decomposition (multi-hop planning) ─────────────
        sub_queries = None
        if policy.use_query_decomposition and generate_fn and aux_budget.try_consume("query_decomposition"):
            telemetry.llm_calls += 1
            try:
                from services.query_planner import decompose_query
                _trace.begin_stage("query_decomposition")
                sub_queries = await decompose_query(payload.message, generate_fn=generate_fn)
                _trace.log_stage("query_decomposition", detail={
                    "original_query": payload.message,
                    "sub_queries": sub_queries or [],
                    "decomposed": bool(sub_queries and len(sub_queries) > 1),
                })
                yield _sse("thinking", {
                    "stage": "query_decomposition", "status": "done",
                    "detail": f"Decomposed into {len(sub_queries)} sub-queries" if sub_queries and len(sub_queries) > 1 else "Single query — no decomposition needed",
                    "data": {"sub_queries": sub_queries or []},
                })
            except Exception as _qp_exc:
                logger.debug("Query decomposition skipped: %s", _qp_exc)
                _trace.log_stage("query_decomposition", detail={"skipped": True, "error": str(_qp_exc)})
                yield _sse("thinking", {"stage": "query_decomposition", "status": "skipped"})
        else:
            _trace.log_stage(
                "query_decomposition",
                detail={
                    "skipped": True,
                    "reason": "policy_or_budget",
                    "policy_enabled": policy.use_query_decomposition,
                },
            )

        _trace.begin_stage("hyde_retrieval")
        raw_docs = await hyde_retrieve(
            _vectorstore,
            payload.message,
            generate_fn=hyde_generate_fn,
            k=20,
            sub_queries=sub_queries,
        )
        if policy.lane == "assistive" and _vectorstore is not None and count_trusted_candidates(raw_docs) < 4:
            _trace.begin_stage("trusted_source_rescue")
            rescue_candidates = []
            try:
                rescue_results = await asyncio.to_thread(
                    _vectorstore.similarity_search_with_score,
                    payload.message,
                    k=60,
                )
                for doc, score in rescue_results:
                    doc.metadata = dict(doc.metadata) if doc.metadata else {}
                    doc.metadata["retrieval_score"] = round(float(score), 4)
                    rescue_candidates.append(doc)
                rescued_docs = promote_trusted_candidates(
                    raw_docs,
                    rescue_candidates,
                    minimum_trusted=6,
                    max_total_docs=30,
                )
                _trace.log_stage(
                    "trusted_source_rescue",
                    docs=rescued_docs,
                    detail={
                        "before_count": len(raw_docs),
                        "after_count": len(rescued_docs),
                        "trusted_before": count_trusted_candidates(raw_docs),
                        "trusted_after": count_trusted_candidates(rescued_docs),
                        "rescue_candidates": len(rescue_candidates),
                    },
                )
                raw_docs = rescued_docs
            except Exception as exc:
                _trace.log_stage(
                    "trusted_source_rescue",
                    detail={"skipped": True, "reason": f"rescue_failed:{exc}"},
                )
        _retrieval_calls += 1
        telemetry.mark_stage(hyde=True)
        _trace.log_stage("hyde_retrieval", docs=raw_docs, detail={
            "k": 20,
            "sub_queries_count": len(sub_queries) if sub_queries else 0,
            "raw_doc_count": len(raw_docs),
            "best_score": round(float(raw_docs[0].metadata.get("retrieval_score", 99)), 4) if raw_docs else None,
            "worst_score": round(float(raw_docs[-1].metadata.get("retrieval_score", 99)), 4) if raw_docs else None,
        })
        _hyde_best = round(float(raw_docs[0].metadata.get("retrieval_score", 99)), 4) if raw_docs else None
        yield _sse("thinking", {
            "stage": "hyde_retrieval", "status": "done",
            "detail": f"Found {len(raw_docs)} chunks (best L2: {_hyde_best})",
            "data": {"doc_count": len(raw_docs), "best_score": _hyde_best},
        })
        # Apply confidence decay so stale docs rank lower
        _trace.begin_stage("confidence_decay")
        apply_confidence_decay_to_results(raw_docs)
        telemetry.mark_stage(rerank=True)
        _trace.log_stage("confidence_decay", docs=raw_docs, doc_count=len(raw_docs))
        _trace.begin_stage("filter_superseded")
        filtered = filter_superseded_docs(raw_docs)
        _trace.log_stage("filter_superseded", docs=filtered, detail={
            "before": len(raw_docs),
            "after": len(filtered),
            "dropped": len(raw_docs) - len(filtered),
        })
        _dropped = len(raw_docs) - len(filtered)

        # ── Cross-encoder reranking ──────────────────────────────
        # Bi-encoder L2 distance is a coarse relevance signal.
        # A cross-encoder scores (query, doc) pairs jointly for
        # much more accurate ordering of which docs the LLM sees.
        try:
            from services.reranker import rerank_documents
            _trace.begin_stage("reranking")
            filtered = await rerank_documents(payload.message, filtered, top_n=12)
            _trace.log_stage("reranking", docs=filtered, doc_count=len(filtered))
        except Exception as _rr_exc:
            logger.debug("Reranking skipped: %s", _rr_exc)

        yield _sse("thinking", {
            "stage": "filter", "status": "done",
            "detail": f"{len(filtered)} chunks after filtering" + (f" ({_dropped} dropped)" if _dropped else ""),
            "data": {"before": len(raw_docs), "after": len(filtered)},
        })

        # ── Self-Correcting Retrieval ────────────────────────────
        # When initial retrieval is sparse, reformulate the query
        # and retry with alternative phrasings for better recall.
        _initial_words = sum(len(d.page_content.split()) for d in filtered) if filtered else 0
        if policy.use_corrective_retrieval and (_initial_words < 80 or len(filtered) < 3) and generate_fn and aux_budget.try_consume("corrective_retrieval"):
            telemetry.llm_calls += 1
            try:
                from services.self_correcting_retrieval import corrective_retrieve
                _trace.begin_stage("corrective_retrieval")
                _pre_count = len(filtered)
                filtered = await corrective_retrieve(
                    _vectorstore,
                    payload.message,
                    initial_docs=filtered,
                    initial_context_words=_initial_words,
                    generate_fn=generate_fn,
                )
                _retrieval_calls += 1
                _trace.log_stage("corrective_retrieval", docs=filtered, detail={
                    "trigger": "sparse" if _initial_words < 80 else "few_docs",
                    "initial_words": _initial_words,
                    "initial_docs": _pre_count,
                    "after_docs": len(filtered),
                    "added": len(filtered) - _pre_count,
                })
                yield _sse("thinking", {
                    "stage": "corrective_retrieval", "status": "done",
                    "detail": f"Reformulated query — added {len(filtered) - _pre_count} extra chunks",
                    "data": {"added": len(filtered) - _pre_count},
                })
            except Exception as _cr_exc:
                logger.debug("Corrective retrieval skipped: %s", _cr_exc)
                _trace.log_stage("corrective_retrieval", detail={"skipped": True, "error": str(_cr_exc)})
        else:
            _trace.log_stage("corrective_retrieval", detail={
                "skipped": True,
                "reason": "policy_budget_or_context",
                "policy_enabled": policy.use_corrective_retrieval,
                "initial_words": _initial_words,
                "doc_count": len(filtered),
            })

        # ── Evidence evaluation + gap retrieval ──────────────────
        _evidence_note = ""
        if policy.use_evidence_evaluation and generate_fn and aux_budget.try_consume("evidence_evaluation"):
            telemetry.llm_calls += 1
            try:
                from services.evidence_evaluator import evaluate_evidence
                _trace.begin_stage("evidence_evaluation")
                eval_result = await evaluate_evidence(
                    payload.message, filtered, generate_fn=generate_fn,
                )
                if eval_result.evaluated:
                    _evidence_note = eval_result.evaluation_note
                    _trace.log_stage("evidence_evaluation", detail={
                        "evaluated": True,
                        "relevant_count": len(eval_result.relevant_indices) if eval_result.relevant_indices else 0,
                        "gap_queries": eval_result.gap_queries or [],
                        "evaluation_note": _evidence_note[:200] if _evidence_note else "",
                    })
                    _ev_rel_count = len(eval_result.relevant_indices) if eval_result.relevant_indices else 0
                    yield _sse("thinking", {
                        "stage": "evidence_evaluation", "status": "done",
                        "detail": f"{_ev_rel_count} relevant chunks identified" + (f", {len(eval_result.gap_queries)} gap queries" if eval_result.gap_queries else ""),
                        "data": {"relevant_count": _ev_rel_count, "gaps": eval_result.gap_queries or []},
                    })
                    if eval_result.gap_queries:
                        from services.hyde_retrieval import gap_retrieve
                        _trace.begin_stage("gap_retrieval")
                        gap_docs = await gap_retrieve(
                            _vectorstore,
                            eval_result.gap_queries,
                            existing_docs=filtered,
                        )
                        _retrieval_calls += 1
                        if gap_docs:
                            filtered = filtered + gap_docs
                            logger.info(
                                "Gap retrieval added %d docs for gaps: %s",
                                len(gap_docs), eval_result.gap_queries,
                            )
                            telemetry.mark_stage(rerank=True)
                        _trace.log_stage("gap_retrieval", docs=gap_docs, detail={
                            "gap_queries": eval_result.gap_queries,
                            "docs_added": len(gap_docs) if gap_docs else 0,
                            "total_after": len(filtered),
                        })
                        if gap_docs:
                            yield _sse("thinking", {
                                "stage": "gap_retrieval", "status": "done",
                                "detail": f"Gap retrieval added {len(gap_docs)} chunks",
                            })
                else:
                    _trace.log_stage("evidence_evaluation", detail={"evaluated": False, "reason": "insufficient_docs"})
            except Exception as _ev_exc:
                _evidence_note = ""
                logger.debug("Evidence evaluation skipped: %s", _ev_exc)
                _trace.log_stage("evidence_evaluation", detail={"skipped": True, "error": str(_ev_exc)})
        else:
            _trace.log_stage(
                "evidence_evaluation",
                detail={
                    "skipped": True,
                    "reason": "policy_or_budget",
                    "policy_enabled": policy.use_evidence_evaluation,
                },
            )
    elif retriever:
        # Fallback: no vectorstore available, use retriever
        raw_docs = await asyncio.to_thread(run_retriever_query, retriever, payload.message)
        apply_confidence_decay_to_results(raw_docs)
        filtered = filter_superseded_docs(raw_docs)
        _evidence_note = ""
    else:
        filtered = []
        _evidence_note = ""

    _trace.begin_stage("context_assembly")
    context = format_docs_for_context(
        filtered,
        max_chars=_ASSISTIVE_RETRIEVAL_CONTEXT_MAX_CHARS if policy.lane == "assistive" else _DEEP_RETRIEVAL_CONTEXT_MAX_CHARS,
    )
    telemetry.retrieved_docs = len(filtered)
    _rel_score = 0.0
    try:
        from cogs.LLM import (
            _build_context_quality_note,
            _build_question_type_hint,
            _build_retrieval_confidence_note,
            _context_relevance_score,
        )
        _rel_score = _context_relevance_score(payload.message, filtered) if filtered else 0.0
        _quality_note = _build_context_quality_note(_rel_score) if filtered else ""
        if _quality_note:
            context = _quality_note + "\n\n" + context if context else _quality_note
            logger.info("Context relevance score: %.2f — injected graded note.", _rel_score)
        # Evidence evaluation note (LLM-assessed relevance & gaps)
        if _evidence_note:
            context = _evidence_note + "\n\n" + context if context else _evidence_note
        # Question-type routing hint
        _qtype_hint = _build_question_type_hint(payload.message)
        if _qtype_hint:
            context = _qtype_hint + "\n\n" + context if context else _qtype_hint
        # Uncertainty admission — borderline retrieval note
        confidence_note = _build_retrieval_confidence_note(filtered)
        if confidence_note:
            context = confidence_note + "\n\n" + context if context else confidence_note
        # Freshness gate — stale docs for recency-sensitive questions
        from services.rag_pipeline import build_freshness_warning
        freshness_note = build_freshness_warning(payload.message, filtered)
        if freshness_note:
            context = freshness_note + "\n\n" + context if context else freshness_note
    except Exception as e:
        logger.warning("operation: suppressed %s", e)
    _context_words = len(context.split()) if context else 0
    telemetry.context_words = _context_words
    sources = extract_source_citations(filtered)

    # Log the scoring / context assembly stage
    _best_score = None
    _avg_score = None
    if filtered:
        _scores = [d.metadata.get("retrieval_score", 99) for d in filtered if d.metadata]
        if _scores:
            _best_score = round(min(_scores), 4)
            _avg_score = round(sum(_scores) / len(_scores), 4)
    _trace.log_stage("context_assembly", docs=filtered, detail={
        "final_doc_count": len(filtered),
        "context_words": _context_words,
        "context_chars": len(context),
        "relevance_score": round(_rel_score, 4),
        "best_retrieval_score": _best_score,
        "avg_retrieval_score": _avg_score,
        "sources": sources,
        "evidence_note": _evidence_note[:200] if _evidence_note else "",
    })
    yield _sse("thinking", {
        "stage": "context_assembly", "status": "done",
        "detail": f"{len(filtered)} docs → {_context_words} words of context (relevance: {round(_rel_score, 2)})",
        "data": {"doc_count": len(filtered), "context_words": _context_words, "relevance_score": round(_rel_score, 2), "best_score": _best_score, "sources": sources},
    })

    # ── Completeness signal for "list all" queries ───────────────────────
    try:
        _all_query_indicators = {"all my", "every ", "list all", "show all", "all open", "all active"}
        _lowered_q = payload.message.lower()
        if any(ind in _lowered_q for ind in _all_query_indicators):
            from admin.dependencies import get_db_optional
            _cdb = get_db_optional()
            if _cdb:
                _record_type_map = {
                    "action": "action", "task": "action",
                    "decision": "decision",
                    "lead": "lead", "opportunit": "lead",
                    "meeting": "meeting",
                }
                _matched_type = None
                for _kw, _rt in _record_type_map.items():
                    if _kw in _lowered_q:
                        _matched_type = _rt
                        break
                if _matched_type:
                    async with _cdb.acquire() as _cconn, _cconn.execute(
                        "SELECT COUNT(*) FROM operational_records WHERE record_type = ? AND state != 'done'",
                        (_matched_type,),
                    ) as _ccur:
                        _total_count = (await _ccur.fetchone())[0]
                    if _total_count and len(filtered) < _total_count:
                        yield _sse("completeness_warning", {
                            "record_type": _matched_type,
                            "retrieved": len(filtered),
                            "total_active": _total_count,
                            "message": (
                                f"Retrieved {len(filtered)} documents, but there are "
                                f"{_total_count} active {_matched_type} records. "
                                f"Check the admin dashboard for a complete list."
                            ),
                        })
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    escalate_after_retrieval, escalation_reason = should_escalate_after_retrieval(
        policy,
        retrieval_doc_count=len(filtered),
        context_words=_context_words,
        relevance_score=_rel_score,
    )
    if escalate_after_retrieval and generate_fn and _vectorstore is not None:
        _escalated_after_retrieval = True
        yield _sse("status", "Initial retrieval was weak for a high-confidence request — switching to deep mode…")
        policy = policy.escalated_to_deep(
            max_aux_retrieval_calls=CHAT_MAX_AUX_RETRIEVAL_CALLS,
            max_generation_stages=CHAT_MAX_GENERATION_STAGES,
            reason=escalation_reason,
        )
        aux_budget = AuxCallBudget(policy.max_aux_retrieval_calls)
        telemetry.policy.complexity = policy.complexity
        telemetry.policy.reason = policy.reason
        telemetry.policy.max_aux_retrieval_calls = policy.max_aux_retrieval_calls
        telemetry.policy.max_generation_stages = policy.max_generation_stages

        deep_sub_queries = None
        if policy.use_query_decomposition and aux_budget.try_consume("query_decomposition"):
            telemetry.llm_calls += 1
            try:
                from services.query_planner import decompose_query

                _trace.begin_stage("query_decomposition_deep")
                deep_sub_queries = await decompose_query(payload.message, generate_fn=generate_fn)
                _trace.log_stage(
                    "query_decomposition_deep",
                    detail={
                        "escalated": True,
                        "sub_queries": deep_sub_queries or [],
                        "reason": escalation_reason,
                    },
                )
            except Exception as exc:
                logger.debug("Deep query decomposition skipped: %s", exc)
                _trace.log_stage("query_decomposition_deep", detail={"skipped": True, "error": str(exc)})

        deep_generate_fn = generate_fn if aux_budget.try_consume("hyde_hypothesis_deep") else None
        if deep_generate_fn:
            telemetry.llm_calls += 1
        _trace.begin_stage("hyde_retrieval_deep")
        deep_raw_docs = await hyde_retrieve(
            _vectorstore,
            payload.message,
            generate_fn=deep_generate_fn,
            k=20,
            sub_queries=deep_sub_queries,
        )
        _retrieval_calls += 1
        apply_confidence_decay_to_results(deep_raw_docs)
        deep_filtered = filter_superseded_docs(deep_raw_docs)
        try:
            from services.reranker import rerank_documents

            _trace.begin_stage("reranking_deep")
            deep_filtered = await rerank_documents(payload.message, deep_filtered, top_n=12)
            _trace.log_stage("reranking_deep", docs=deep_filtered, doc_count=len(deep_filtered))
        except Exception as exc:
            logger.debug("Deep reranking skipped: %s", exc)

        if policy.use_corrective_retrieval and aux_budget.try_consume("corrective_retrieval_deep"):
            telemetry.llm_calls += 1
            try:
                from services.self_correcting_retrieval import corrective_retrieve

                _trace.begin_stage("corrective_retrieval_deep")
                deep_filtered = await corrective_retrieve(
                    _vectorstore,
                    payload.message,
                    initial_docs=deep_filtered,
                    initial_context_words=sum(len(d.page_content.split()) for d in deep_filtered) if deep_filtered else 0,
                    generate_fn=generate_fn,
                )
                _retrieval_calls += 1
                _trace.log_stage("corrective_retrieval_deep", docs=deep_filtered, doc_count=len(deep_filtered))
            except Exception as exc:
                logger.debug("Deep corrective retrieval skipped: %s", exc)
                _trace.log_stage("corrective_retrieval_deep", detail={"skipped": True, "error": str(exc)})

        if policy.use_evidence_evaluation and aux_budget.try_consume("evidence_evaluation_deep"):
            telemetry.llm_calls += 1
            try:
                from services.evidence_evaluator import evaluate_evidence

                _trace.begin_stage("evidence_evaluation_deep")
                eval_result = await evaluate_evidence(payload.message, deep_filtered, generate_fn=generate_fn)
                _trace.log_stage(
                    "evidence_evaluation_deep",
                    detail={
                        "evaluated": eval_result.evaluated,
                        "gap_queries": eval_result.gap_queries or [],
                        "reason": escalation_reason,
                    },
                )
                if eval_result.gap_queries:
                    from services.hyde_retrieval import gap_retrieve

                    _trace.begin_stage("gap_retrieval_deep")
                    gap_docs = await gap_retrieve(
                        _vectorstore,
                        eval_result.gap_queries,
                        existing_docs=deep_filtered,
                    )
                    _retrieval_calls += 1
                    if gap_docs:
                        deep_filtered = deep_filtered + gap_docs
                    _trace.log_stage(
                        "gap_retrieval_deep",
                        docs=gap_docs,
                        detail={"docs_added": len(gap_docs) if gap_docs else 0},
                    )
            except Exception as exc:
                logger.debug("Deep evidence evaluation skipped: %s", exc)
                _trace.log_stage("evidence_evaluation_deep", detail={"skipped": True, "error": str(exc)})

        filtered = deep_filtered
        context = format_docs_for_context(
            filtered,
            max_chars=_DEEP_RETRIEVAL_CONTEXT_MAX_CHARS,
        )
        _context_words = len(context.split()) if context else 0
        telemetry.retrieved_docs = len(filtered)
        telemetry.context_words = _context_words
        sources = extract_source_citations(filtered)
        yield _sse("thinking", {
            "stage": "deep_escalation", "status": "done",
            "detail": f"Deep retrieval produced {len(filtered)} docs and {_context_words} words of context",
            "data": {"reason": escalation_reason, "doc_count": len(filtered), "context_words": _context_words},
        })

    # ── 1a. Proactive web augmentation ───────────────────────────────────
    # When RAG context is sparse, the question signals need for current
    # information, OR retrieved context has poor relevance scores,
    # proactively search the web to supplement the context.
    try:
        from cogs.LLM import _context_is_relevant, _is_outward_question, _needs_web_search

        sparse = _context_words < 80
        intent = _needs_web_search(payload.message)
        weak_relevance = not _context_is_relevant(filtered)
        outward = _is_outward_question(payload.message) and _context_words < 200
        if sparse or intent or weak_relevance or outward:
            wac_enabled = True
            try:
                from core.config_loader import WorkflowConfig
                _wf = WorkflowConfig.load()
                wac_enabled = _wf.cq_web_chat_enabled
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            if wac_enabled:
                import os

                from services.secrets import get_secrets_manager
                from services.tavily_service import TavilyService

                secrets = get_secrets_manager()
                tavily_key = os.getenv("TAVILY_API_KEY") or secrets.get("tavily")
                if tavily_key:
                    tavily = TavilyService(tavily_key)
                    if tavily.is_configured:
                        # ── Status: tell user WHY we're searching the web ──
                        if weak_relevance and not sparse:
                            yield _sse("status", "Knowledge base didn't have a strong match — searching the web…")
                        elif intent:
                            yield _sse("status", "Looking up current information on the web…")
                        else:
                            yield _sse("status", "Limited local knowledge — searching the web…")

                        from services.web_research import chat_web_augment

                        web_block = await chat_web_augment(
                            tavily, payload.message, max_results=4,
                        )
                        if web_block:
                            _web_searched = True
                            # Corpus-first: for inward questions, keep corpus
                            # context authoritative and append web as supplement.
                            if context and not outward:
                                context = (
                                    "[INTERNAL KNOWLEDGE BASE — authoritative for org-specific facts]\n"
                                    + context
                                    + "\n\n[WEB SEARCH RESULTS — supplementary]\n"
                                    + web_block
                                )
                            else:
                                context = context + "\n\n" + web_block if context else web_block
                            trigger = (
                                "intent" if intent
                                else "weak_relevance" if weak_relevance
                                else "sparse"
                            )
                            logger.info(
                                "Web chat augmentation (%s): added %d chars (RAG had %d words)",
                                trigger, len(web_block), _context_words,
                            )
                            telemetry.mark_stage(web=True)

                            yield _sse("status", "Found web sources — generating answer…")

                            # Cache web results into corpus for future reuse
                            try:
                                from services.autonomous_research import cache_web_result
                                await _schedule_durable_background(
                                    coro=cache_web_result(
                                        question=payload.message,
                                        web_block=web_block,
                                    ),
                                    job_name="cache_web_result",
                                    title="Cache web answer into corpus",
                                    objective="Persist useful web augmentation results so later chats can answer locally.",
                                    next_step="Write a memo artifact and ingest it if appropriate.",
                                    start_summary="Queued web-result caching after successful chat web augmentation.",
                                    success_summary_builder=lambda result: "Cached web results into a durable memo artifact." if result else "Web-result caching finished without creating a new memo.",
                                    failure_summary="Web-result caching failed",
                                )
                            except Exception:
                                pass  # Non-blocking
                        else:
                            yield _sse("status", "Web search didn't find anything relevant — using available knowledge…")
    except Exception as exc:
        logger.debug("Web augmentation skipped: %s", exc)

    _trace.log_stage("web_augmentation", detail={
        "triggered": _web_searched,
        "sparse": _context_words < 80,
        "context_words_after": len(context.split()) if context else 0,
    })
    if _web_searched:
        yield _sse("thinking", {
            "stage": "web_augmentation", "status": "done",
            "detail": "Added web search results to context",
        })

    telemetry.finish_retrieval()
    telemetry.policy.aux_retrieval_calls = aux_budget.calls_used
    telemetry.start_generation()
    yield _sse("thinking", {"stage": "generating", "status": "active", "detail": "Generating answer…"})

    # ── 1b. Response cache check ─────────────────────────────────────────
    # Skip cache when a topic reset was detected — the user is correcting
    # context, so returning a stale cached answer would be harmful.
    history = _build_history_string(payload.history, topic_reset=topic_reset)
    full_context = context
    if history:
        full_context = f"[Conversation History]\n{history}\n\n{full_context}"
    full_context = _trim_generation_context(full_context, policy.lane)
    _cache_context_for_trace = full_context

    response_cache = get_response_cache()
    cache_hit = None if topic_reset or policy.artifact_request else response_cache.get(payload.message, full_context)
    if cache_hit:
        telemetry.cache_hit = True
        telemetry.route_mode = "cache"
        logger.debug("Response cache HIT — skipping pipeline")
        yield _sse("status", "")
        cached_reply = cache_hit.result.get("final", "")
        _response_summary = cached_reply[:200]
        _full_reply = cached_reply
        _context_words = cache_hit.context_words
        _pipeline_used = True

        words = cached_reply.split(" ")
        for i in range(0, len(words), 3):
            chunk = " ".join(words[i : i + 3])
            if i + 3 < len(words):
                chunk += " "
            telemetry.note_first_token()
            yield _sse("token", chunk)
            await asyncio.sleep(0.01)

        yield _sse("sources", cache_hit.sources)
        _sources_used = cache_hit.sources
        yield _sse("chunk_sources", cache_hit.chunk_sources)

        # Still log interaction + session even on cache hit
        if conv_store and session_id:
            try:
                await conv_store.add_turn(session_id, "assistant", cached_reply[:2000])
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        if memory:
            try:
                await memory.log_interaction(
                    query=payload.message,
                    response_summary=_response_summary,
                    sources_used=_sources_used,
                )
            except Exception as e:
                logger.warning("operation: suppressed %s", e)
        telemetry.estimate_tokens(
            model="cache-hit",
            context=full_context,
            user_prompt=payload.message,
            reply_text=cached_reply,
        )
        _log_alpha()
        _trace.finish(**telemetry.to_trace_summary(), reply_length=len(_full_reply) if _full_reply else 0)
        await _persist_request_trace_once()
        yield _sse("retrieval_trace_id", _trace.trace_id)
        yield _sse("request_trace_id", request_id)
        yield _sse("done", "")
        return

    # ── 2. Build history string ──────────────────────────────────────────
    if topic_reset:
        memory_context = ""

    # ── 3. Try pipeline with tool-calling ────────────────────────────────
    pipeline_used = False
    local_only_block_reason: Optional[str] = None
    try:
        pipeline_router = await _get_pipeline_router()
        if pipeline_router and pipeline_router.pipeline:
            yield _sse("status", "Analysing…")
            from services.model_router import BackendType, PipelineRole

            # Build system prompt with tool definitions if registry available
            sys_prompt = (
                system_template
                .replace("{context}", "")
                .replace("{question}", "")
                .replace("{history}", "")
                .replace("{user}", "web-user")
                .replace("{channel}", "web-chat")
            )
            if registry:
                sys_prompt = build_tool_system_prompt(
                    sys_prompt, registry, workflows_config
                )

            prompt_fragment_limit = (
                _ASSISTIVE_PROMPT_FRAGMENT_MAX_CHARS
                if policy.lane == "assistive"
                else _DEEP_PROMPT_FRAGMENT_MAX_CHARS
            )
            prompt_fragments: List[str] = []
            if concern_context:
                prompt_fragments.append(_truncate_prompt_fragment(concern_context, min(700, prompt_fragment_limit)))
            if preference_context:
                prompt_fragments.append(_truncate_prompt_fragment(preference_context, min(700, prompt_fragment_limit)))
            if memory_context and (policy.lane == "deep" or payload.history):
                prompt_fragments.append(_truncate_prompt_fragment(memory_context, min(900, prompt_fragment_limit)))
            if workspace_context and policy.lane == "deep":
                prompt_fragments.append(_truncate_prompt_fragment(workspace_context, prompt_fragment_limit))
            if proactive_context and policy.lane == "deep":
                prompt_fragments.append(_truncate_prompt_fragment(proactive_context, min(900, prompt_fragment_limit)))
            for fragment in prompt_fragments:
                if fragment:
                    sys_prompt += "\n" + fragment

            # ── Feedback-driven prompt suffix ────────────────────
            # Append behavioural refinements generated by the feedback
            # learning loop (e.g. "cite sources more" after factual_error
            # feedback patterns).  Best-effort — ignored if unavailable.
            try:
                from services.feedback_learning_loop import FeedbackLearningLoop
                _fbl = FeedbackLearningLoop(request_db)
                _suffix = await _fbl.get_active_prompt_suffix()
                if _suffix:
                    sys_prompt += "\n\n--- BEHAVIOURAL REFINEMENTS (from user feedback) ---\n" + _suffix
            except Exception:
                pass

            # Enforce strict local-only mode by bypassing cloud-configured pipelines.
            if LOCAL_LLM_ONLY:
                has_cloud_role = False
                for role_cfg in pipeline_router.pipeline.roles.values():
                    backend_cfg = pipeline_router.backends.get(role_cfg.backend_name)
                    if not backend_cfg:
                        continue
                    if backend_cfg.backend_type in (BackendType.OPENAI, BackendType.ANTHROPIC, BackendType.OPENROUTER):
                        has_cloud_role = True
                        break
                if has_cloud_role:
                    raise RuntimeError("LOCAL_LLM_ONLY is enabled and pipeline has cloud backends")

            initial_cfg = pipeline_router.pipeline.roles.get(PipelineRole.INITIAL)
            assistive_single_stage = (
                policy.lane == "assistive"
                and policy.max_generation_stages <= 1
                and initial_cfg is not None
                and initial_cfg.enabled
            )

            telemetry.llm_calls += 1
            if assistive_single_stage:
                initial_user = ""
                if full_context:
                    initial_user += f"=== RETRIEVED CONTEXT ===\n{full_context}\n\n"
                initial_user += f"=== USER QUESTION ===\n{payload.message}"
                assistive_backend_name, assistive_model = _resolve_assistive_backend_and_model(initial_cfg)

                assistive_ollama_options = initial_cfg.ollama_options
                if assistive_ollama_options:
                    assistive_ollama_options = dict(assistive_ollama_options)
                    assistive_ollama_options["num_ctx"] = min(
                        int(assistive_ollama_options.get("num_ctx", 16384)),
                        8192,
                    )

                reply_text = await pipeline_router.generate_single(
                    backend_name=assistive_backend_name,
                    model=assistive_model,
                    prompt=initial_user,
                    system_prompt=sys_prompt,
                    temperature=initial_cfg.temperature,
                    max_tokens=min(initial_cfg.max_tokens, _ASSISTIVE_GENERATION_MAX_TOKENS),
                    ollama_options=assistive_ollama_options,
                )
                result = {
                    "final": reply_text,
                    "stages": {"initial": reply_text},
                    "models_used": {"initial": f"{assistive_backend_name}/{assistive_model}"},
                    "stage_limit_applied": 1,
                }
            else:
                result = await pipeline_router.generate_pipeline(
                    user_prompt=payload.message,
                    context=full_context,
                    system_prompt=sys_prompt,
                    max_generation_stages=policy.max_generation_stages,
                )
            reply_text = result["final"]
            pipeline_used = True
            _pipeline_used = True
            _models_used = result.get("models_used")
            _pipeline_stages = result.get("stages") or {}
            telemetry.route_mode = "assistive_single_stage" if assistive_single_stage else "pipeline"
            telemetry.update_from_pipeline_result(result)
            try:
                telemetry.llm_calls += len(result.get("stages", {})) - 1
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            # ── Check for tool call in the response ──────────────────
            tool_call = extract_tool_call(reply_text) if registry else None

            if tool_call and registry:
                # Use the multi-step planning loop for all tool calls
                # It handles: mutating → confirmation, read-only → execute + replan

                # Build a generate_fn for the planning loop
                initial_cfg = pipeline_router.pipeline.roles.get(PipelineRole.INITIAL)

                async def _planning_generate(prompt: str, system_prompt: str) -> str:
                    nonlocal telemetry
                    if initial_cfg:
                        telemetry.llm_calls += 1
                        return await pipeline_router.generate_single(
                            backend_name=initial_cfg.backend_name,
                            model=initial_cfg.model,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            temperature=0.3,
                            ollama_options=initial_cfg.ollama_options,
                        )
                    else:
                        telemetry.llm_calls += 1
                        r = await pipeline_router.generate_pipeline(
                            user_prompt=prompt,
                            context="",
                            system_prompt=system_prompt,
                        )
                        return r["final"]

                # Get db for tools that need it
                tool_db = None
                try:
                    from admin.dependencies import get_db_optional
                    tool_db = get_db_optional()
                except Exception as e:
                    logger.warning("_planning_generate: suppressed %s", e)

                # SSE callbacks for streaming tool progress
                _sse_events = []

                def _on_tool_call(name, args):
                    _tools_invoked.append(name)
                    _mark_artifact_tool_selected(name)
                    _sse_events.append(_sse("tool_call", {"name": name, "arguments": args}))

                def _on_tool_result(name, result):
                    _artifact_refs.extend(result.artifact_refs or [])
                    _mark_artifact_write_attempt(
                        success=result.success,
                        artifact_refs=result.artifact_refs,
                    )
                    _sse_events.append(_sse("tool_result", {
                        "name": name,
                        "success": result.success,
                        "message": result.message,
                        "data": result.data,
                        "artifact_refs": result.artifact_refs,
                    }))

                def _on_status(msg):
                    _sse_events.append(_sse("status", msg))

                planning_result = await run_planning_loop(
                    registry=registry,
                    generate_fn=_planning_generate,
                    initial_response=reply_text,
                    system_prompt=sys_prompt,
                    user_prompt=payload.message,
                    context=full_context,
                    db=tool_db,
                    on_tool_call=_on_tool_call,
                    on_tool_result=_on_tool_result,
                    on_status=_on_status,
                )

                # Emit accumulated SSE events
                for event in _sse_events:
                    yield event

                if planning_result["needs_confirmation"]:
                    # Mutating tool → send confirmation, end stream
                    conf_event = planning_result["confirmation_event"]
                    _tools_invoked.append(f"{conf_event['tool_name']}(pending)")
                    _mark_artifact_tool_selected(
                        conf_event["tool_name"],
                        confirmation_requested=True,
                    )
                    _mark_artifact_terminal("confirmation_requested")
                    yield _sse("tool_confirmation", conf_event)
                    prose = planning_result["final_text"]
                    if prose:
                        _response_summary = prose[:200]
                        telemetry.note_first_token()
                        yield _sse("token", prose)
                    yield _sse("sources", sources)
                    _sources_used = sources
                    if memory:
                        try:
                            await memory.log_interaction(
                                query=payload.message,
                                response_summary=_response_summary,
                                tools_invoked=_tools_invoked,
                                artifact_refs=_artifact_refs,
                                sources_used=_sources_used,
                            )
                        except Exception as e:
                            logger.warning("operation: suppressed %s", e)
                    telemetry.estimate_tokens(
                        model=next(iter((_models_used or {}).values()), "pipeline"),
                        system_prompt=sys_prompt,
                        context=full_context,
                        user_prompt=payload.message,
                        reply_text=prose or "",
                    )
                    _log_alpha()
                    _trace.finish(
                        tool_confirmation=True,
                        pipeline_used=_pipeline_used,
                        models_used=_models_used,
                        **telemetry.to_trace_summary(),
                    )
                    await _persist_request_trace_once()
                    yield _sse("retrieval_trace_id", _trace.trace_id)
                    yield _sse("request_trace_id", request_id)
                    yield _sse("done", "")
                    return

                else:
                    # Planning complete — stream the final text
                    final_text = planning_result["final_text"]
                    if policy.artifact_request and not _artifact_refs and not any(name.endswith("(pending)") for name in _tools_invoked):
                        fallback_tool_call = _build_artifact_tool_fallback(payload.message, final_text)
                        if fallback_tool_call and registry:
                            tool_obj = registry.get(fallback_tool_call["name"])
                            if tool_obj and tool_obj.mutates:
                                _tools_invoked.append(f"{fallback_tool_call['name']}(pending)")
                                _tool_confirmation_pending = True
                                _mark_artifact_tool_selected(
                                    fallback_tool_call["name"],
                                    confirmation_requested=True,
                                )
                                _mark_artifact_terminal("confirmation_requested")
                                yield _sse(
                                    "tool_confirmation",
                                    build_confirmation_event(fallback_tool_call, tool_obj.description),
                                )
                                prose = final_text
                                if prose:
                                    _response_summary = prose[:200]
                                    _full_reply = prose
                                    telemetry.note_first_token()
                                    yield _sse("token", prose)
                                yield _sse("sources", sources)
                                _sources_used = sources
                                if memory:
                                    try:
                                        await memory.log_interaction(
                                            query=payload.message,
                                            response_summary=_response_summary,
                                            tools_invoked=_tools_invoked,
                                            artifact_refs=_artifact_refs,
                                            sources_used=_sources_used,
                                        )
                                    except Exception as e:
                                        logger.warning("operation: suppressed %s", e)
                                telemetry.estimate_tokens(
                                    model=next(iter((_models_used or {}).values()), "pipeline"),
                                    system_prompt=sys_prompt,
                                    context=full_context,
                                    user_prompt=payload.message,
                                    reply_text=prose or "",
                                )
                                _log_alpha()
                                _trace.finish(
                                    tool_confirmation=True,
                                    pipeline_used=_pipeline_used,
                                    models_used=_models_used,
                                    **telemetry.to_trace_summary(),
                                )
                                await _persist_request_trace_once()
                                yield _sse("retrieval_trace_id", _trace.trace_id)
                                yield _sse("request_trace_id", request_id)
                                yield _sse("done", "")
                                return
                    _response_summary = final_text[:200]
                    _full_reply = final_text
                    words = final_text.split(" ")
                    for i in range(0, len(words), 3):
                        chunk = " ".join(words[i : i + 3])
                        if i + 3 < len(words):
                            chunk += " "
                        telemetry.note_first_token()
                        yield _sse("token", chunk)
                        await asyncio.sleep(0.015)
            else:
                # No tool call -- stream normally
                _response_summary = reply_text[:200]
                _full_reply = reply_text
                words = reply_text.split(" ")
                for i in range(0, len(words), 3):
                    chunk = " ".join(words[i : i + 3])
                    if i + 3 < len(words):
                        chunk += " "
                    telemetry.note_first_token()
                    yield _sse("token", chunk)
                    await asyncio.sleep(0.015)
    except Exception as e:
        if LOCAL_LLM_ONLY and "LOCAL_LLM_ONLY" in str(e):
            local_only_block_reason = str(e)
        logger.warning("Pipeline failed, falling back to single model: %s", e)
        _pipeline_stages = {"pipeline_error": str(e)}

    # ── 4. Fallback: stream single-model tokens ─────────────────────────
    if not pipeline_used:
        telemetry.route_mode = "fallback"
        yield _sse("status", "Generating response…")
        try:
            if LOCAL_LLM_ONLY:
                telemetry.route_mode = "degraded_local_only"
                degraded = build_local_only_blocked_reply(local_only_block_reason or "no local route succeeded")
                _response_summary = degraded[:200]
                _full_reply = degraded
                _pipeline_stages = {"degraded_local_only": True}
                telemetry.note_first_token()
                yield _sse("status", "Local-only mode blocked cloud fallback")
                yield _sse("token", degraded)
            else:
                from langchain_core.output_parsers import StrOutputParser
                from langchain_openai import ChatOpenAI

                model = ChatOpenAI(
                    model="gpt-4o-mini",
                    temperature=0.2,
                    api_key=gpt_key,
                    streaming=True,
                )
                telemetry.llm_calls += 1
                _pipeline_stages = {"fallback_single_model": "gpt-4o-mini"}
                chain = llm_prompt | model | StrOutputParser()
                input_dict = {
                    "context": context,
                    "question": payload.message,
                    "history": history,
                    "user": "web-user",
                    "channel": "web-chat",
                }
                async for chunk in chain.astream(input_dict):
                    if chunk:
                        _full_reply += chunk
                        telemetry.note_first_token()
                        yield _sse("token", chunk)
        except Exception as e:
            logger.error("Streaming LLM call failed: %s", e)
            yield _sse("token", f"⚠️ Error generating response: {e}")
            _log_alpha(error=str(e))

    # ── 5. Sources + done ────────────────────────────────────────────────
    # Mark generating as complete
    yield _sse("thinking", {"stage": "generating", "status": "done", "detail": "Answer complete"})
    # Emit source citations for display
    yield _sse("sources", sources)
    _sources_used = sources

    # Emit raw chunk source paths for feedback loop (invisible to user)
    chunk_source_paths = list({(d.metadata.get("source_relpath") or d.metadata.get("source") or "") for d in filtered if d.metadata})
    chunk_source_paths = [p for p in chunk_source_paths if p]  # drop empties
    yield _sse("chunk_sources", chunk_source_paths)

    if policy.artifact_request and not _artifact_refs and not any(name.endswith("(pending)") for name in _tools_invoked):
        fallback_tool_call = _build_artifact_tool_fallback(payload.message, _full_reply)
        if fallback_tool_call and registry:
            tool_obj = registry.get(fallback_tool_call["name"])
            if tool_obj and tool_obj.mutates:
                _tools_invoked.append(f"{fallback_tool_call['name']}(pending)")
                _tool_confirmation_pending = True
                _mark_artifact_tool_selected(
                    fallback_tool_call["name"],
                    confirmation_requested=True,
                )
                _mark_artifact_terminal("confirmation_requested")
                yield _sse(
                    "tool_confirmation",
                    build_confirmation_event(fallback_tool_call, tool_obj.description),
                )
        if not _artifact_refs and not _tool_confirmation_pending and _artifact_funnel.get("terminal_state") is None:
            _mark_artifact_terminal(
                "tool_plan_not_executed" if _artifact_funnel.get("tool_selected") else "null_outcome",
                failure_mode=(
                    _ARTIFACT_FAILURE_PLAN_NOT_EXECUTED
                    if _artifact_funnel.get("tool_selected")
                    else _ARTIFACT_FAILURE_NULL_OUTCOME
                ),
                completed_successfully=False,
            )

    # ── 5b. Generic answer suppression (#10) ────────────────────────────
    # If the response doesn't address the question's key terms, append
    # a low-confidence note via an extra SSE token.
    if _full_reply:
        try:
            from cogs.LLM import _is_generic_answer
            if _is_generic_answer(payload.message, _full_reply):
                logger.info("Generic answer detected in web chat — appending note")
                generic_note = (
                    "\n\n---\n*Note: This answer may not fully address your specific question. "
                    "Try rephrasing, or ask me to search the web for more targeted information.*"
                )
                yield _sse("token", generic_note)
                _full_reply += generic_note
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

    # ── 5b-ii. Surface fact-check caveats from CRITIQUE stage ────────────
    critique_text = _pipeline_stages.get("critique") or ""
    if critique_text and _full_reply:
        try:
            # Look for verification flags in the critique
            _caveat_indicators = [
                "not supported by", "no evidence in", "not found in the source",
                "sources do not", "no source document", "cannot verify",
                "not mentioned in", "contradicts", "misinterpret",
            ]
            lowered = critique_text.lower()
            flagged = any(ind in lowered for ind in _caveat_indicators)
            if flagged:
                yield _sse("fact_check_caveat", {
                    "message": (
                        "The fact-check stage flagged potential concerns with parts of this answer. "
                        "Some claims may not be fully supported by your documents."
                    ),
                })
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

    # ── 5c. Auto-detect knowledge gaps + autonomous research ──────────────
    if _full_reply:
        try:
            from admin.dependencies import get_db_optional
            gap_db = get_db_optional()
            if gap_db:
                context_word_count = len(context.split()) if context else 0
                gap_id, _assess = await _maybe_log_knowledge_gap(
                    db=gap_db,
                    question=payload.message,
                    reply_text=_full_reply,
                    context_word_count=context_word_count,
                    doc_count=len(filtered),
                    context=context or "",
                )

                # Surface confidence tier to the frontend
                if _assess:
                    conf = _assess.confidence
                    if conf >= 8:
                        conf_tier = "high"
                    elif conf >= 5:
                        conf_tier = "moderate"
                    else:
                        conf_tier = "low"
                    yield _sse("confidence", {
                        "tier": conf_tier,
                        "grounded": _assess.grounded,
                        "gap_detected": _assess.gap_detected,
                    })

                if gap_id:
                    yield _sse("knowledge_gap", {
                        "gap_id": gap_id,
                        "message": "A knowledge gap was detected and logged for follow-up.",
                    })

                    # Fire-and-forget: research the gap immediately
                    try:
                        from services.autonomous_research import (
                            research_gap_immediately,
                        )
                        _topic = (
                            (_assess.suggested_topic if _assess else "")
                            or " ".join(
                                w for w in payload.message.split()[:8] if len(w) > 3
                            )
                        )
                        _missing = _assess.missing_knowledge if _assess else ""
                        await _schedule_durable_background(
                            coro=research_gap_immediately(
                                topic=_topic,
                                question=payload.message,
                                missing_knowledge=_missing,
                                db=gap_db,
                            ),
                            job_name="research_gap_immediately",
                            title="Research detected knowledge gap",
                            objective="Research a detected knowledge gap and persist any resulting memo artifact.",
                            next_step="Search, draft, and ingest a memo if the gap can be filled automatically.",
                            start_summary="Queued immediate research for a knowledge gap detected in chat.",
                            success_summary_builder=lambda result: "Immediate gap research produced a durable memo artifact." if result else "Immediate gap research completed without creating a memo.",
                            failure_summary="Immediate gap research failed",
                        )
                    except Exception:
                        pass  # Non-blocking
                elif _assess:
                    # No gap — answer was good. Auto-close matching gaps.
                    try:
                        from services.autonomous_research import (
                            maybe_auto_close_gap,
                        )
                        await _schedule_durable_background(
                            coro=maybe_auto_close_gap(
                                question=payload.message,
                                confidence=_assess.confidence,
                                grounded=_assess.grounded,
                                db=gap_db,
                            ),
                            job_name="maybe_auto_close_gap",
                            title="Resolve matching knowledge gaps",
                            objective="Auto-close matching open knowledge gaps after a grounded high-confidence answer.",
                            next_step="Resolve matching open gaps if the answer quality justifies it.",
                            start_summary="Queued automatic gap-resolution check after a strong grounded answer.",
                            success_summary_builder=lambda result: "Auto-closed at least one matching knowledge gap." if result else "Gap-resolution check finished with no matching gaps to close.",
                            failure_summary="Automatic gap-resolution check failed",
                        )
                    except Exception:
                        pass  # Non-blocking
        except Exception as exc:
            logger.debug("Gap auto-detection skipped: %s", exc)

    # ── 6. Log interaction to memory ─────────────────────────────────────
    if memory:
        try:
            await memory.log_interaction(
                query=payload.message,
                response_summary=_response_summary,
                tools_invoked=_tools_invoked or None,
                artifact_refs=_artifact_refs or None,
                sources_used=_sources_used or None,
            )
        except Exception as exc:
            logger.debug("Failed to log interaction: %s", exc)

    # ── 6a. Observe interaction for preference learning (moat) ───────────
    try:
        from admin.dependencies import get_db_optional
        db_inst = get_db_optional()
        if db_inst:
            from services.user_preferences import UserPreferenceService
            pref_svc = UserPreferenceService(db_inst)
            await pref_svc.observe_interaction(
                user_id="admin",
                query=payload.message,
                response_length=len(_full_reply) if _full_reply else 0,
                tools_used=_tools_invoked or None,
                source_count=len(_sources_used) if _sources_used else 0,
            )
    except Exception as exc:
        logger.debug("Preference observation skipped: %s", exc)

    # ── 6b. Cache the response for future identical queries ──────────────
    if _full_reply and not _tools_invoked and not policy.artifact_request:
        try:
            response_cache = get_response_cache()
            _ctx_words = len(full_context.split()) if full_context else 0
            _chunk_src = list({
                (d.metadata.get("source_relpath") or d.metadata.get("source") or "")
                for d in filtered if d.metadata
            }) if filtered else []
            _chunk_src = [p for p in _chunk_src if p]
            response_cache.put(
                query=payload.message,
                context=full_context,
                result={"final": _full_reply},
                sources=sources,
                chunk_sources=_chunk_src,
                context_words=_ctx_words,
            )
        except Exception as exc:
            logger.debug("Response cache put failed: %s", exc)

    telemetry.estimate_tokens(
        model=next(iter((_models_used or {}).values()), "gpt-4o-mini" if telemetry.route_mode == "fallback" else "pipeline"),
        system_prompt=sys_prompt if 'sys_prompt' in locals() else system_template,
        context=full_context if 'full_context' in locals() else context,
        user_prompt=payload.message,
        history=history if 'history' in locals() else "",
        reply_text=_full_reply,
    )

    # ── 6c. Save assistant turn to persistent conversation memory ────────
    if conv_store and session_id and _full_reply:
        try:
            await conv_store.add_turn(session_id, "assistant", _full_reply[:4000])
        except Exception as exc:
            logger.debug("Conversation store save failed: %s", exc)

    # ── 6d. Emit proactive nudges (agentic suggestions) ──────────────────
    if _proactive_nudges:
        yield _sse("proactive_nudge", _proactive_nudges)

    _log_alpha()
    if policy.artifact_request and _artifact_refs:
        _mark_artifact_terminal("artifact_created", completed_successfully=True)
    _trace.finish(
        pipeline_used=_pipeline_used,
        models_used=_models_used,
        web_searched=_web_searched,
        tool_confirmation=_tool_confirmation_pending,
        sources=_sources_used,
        **telemetry.to_trace_summary(),
        reply_length=len(_full_reply) if _full_reply else 0,
    )
    await _persist_request_trace_once()
    yield _sse("retrieval_trace_id", _trace.trace_id)
    yield _sse("request_trace_id", request_id)
    _done_payload = {"conversation_id": session_id} if session_id else ""
    if isinstance(_done_payload, dict):
        _done_payload["trace_id"] = _trace.trace_id
        _done_payload["request_id"] = request_id
    yield _sse("done", _done_payload)


# ── Legacy non-streaming endpoint (kept for backward compat) ─────────────────


@router.post("/api/v1/chat", response_model=ChatResponse, dependencies=[Depends(require_admin)])
async def api_chat(payload: ChatRequest):
    """Non-streaming fallback."""
    from cogs.LLM import AskQuestion

    retriever = await _ensure_retriever()

    topic_reset = _detect_topic_reset(payload.message)
    history_text = _build_history_string(payload.history, topic_reset=topic_reset)
    message_chain: list[str] = ["__placeholder__"]
    if history_text:
        message_chain.extend(history_text.split("\n"))

    try:
        reply = await AskQuestion(
            q=payload.message,
            message_chain=message_chain if len(message_chain) > 1 else None,
            user="web-user",
            retriever=retriever,
        )
    except Exception as exc:
        logger.error("Chat pipeline error: %s", exc, exc_info=True)
        reply = f"Sorry, something went wrong: {exc}"

    return ChatResponse(
        reply=reply or "No response generated.",
        conversation_id=payload.conversation_id,
    )


# ── Feedback endpoint ────────────────────────────────────────────────────────


@router.post("/api/v1/chat/feedback", dependencies=[Depends(require_admin)])
async def api_chat_feedback(payload: FeedbackRequest, current_actor=Depends(get_current_actor), db=Depends(get_db)):
    """Record thumbs-up / thumbs-down feedback on a chat response.

    Also feeds into the learning loop for chunk quality tracking,
    improvement signal generation, and auto-gap creation — mirroring
    the inbox feedback endpoint so web chat feedback actually drives
    corpus improvement.
    """
    try:
        current_actor = _resolve_actor(current_actor)
        chunk_sources_json = json.dumps(payload.chunk_sources) if payload.chunk_sources else None
        await db.execute(
            """INSERT INTO response_feedback
            (user_id, username, question, answer, feedback, channel_id, message_id, chunk_sources)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (current_actor.account_id or 0, _actor_display_name(current_actor), payload.question, payload.answer[:2000],
            payload.feedback, None, None, chunk_sources_json),
            )
        # Feed into learning loop — chunk quality, improvement signals, auto-gaps
        try:
            from services.feedback_learning_loop import FeedbackLearningLoop
            fll = FeedbackLearningLoop(db)
            await fll.ensure_tables()
            await fll.process_feedback(
                query=payload.question,
                response=payload.answer[:2000],
                feedback=payload.feedback,
                chunk_sources=payload.chunk_sources or [],
                user_id=current_actor.stable_id,
            )
        except Exception as fll_err:
            logger.warning("Feedback learning loop error (non-fatal): %s", fll_err)

        return {"ok": True}
    except Exception as e:
        logger.error("Failed to record chat feedback: %s", e)
        raise HTTPException(status_code=500, detail="Failed to record feedback")


# ── Tool confirmation endpoint ───────────────────────────────────────────────


@router.post("/api/v1/chat/tool-confirm", dependencies=[Depends(require_admin)])
async def api_tool_confirm(payload: ToolConfirmRequest, db=Depends(get_db)):
    """
    Execute a previously proposed tool after user confirmation.

    The frontend sends this when the user clicks 'Confirm' on a
    tool_confirmation SSE event.  Returns the tool result directly
    (not streamed).
    """
    from services.agentic_chat import execute_tool_call
    from services.request_tracing import update_request_trace_after_confirmation

    registry = await _ensure_tool_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Tool registry unavailable")

    if not payload.confirmed:
        if payload.request_id:
            await update_request_trace_after_confirmation(
                db,
                request_id=payload.request_id,
                failure_mode=_ARTIFACT_FAILURE_CONFIRMATION_DECLINED,
                completed_successfully=False,
                artifact_funnel={
                    "confirmation_requested": True,
                    "terminal_state": "confirmation_declined",
                },
                stage_name="artifact_confirmation_declined",
            )
        return {"ok": True, "result": {"success": False, "message": "User declined"}}

    tool_call = {"name": payload.tool_name, "arguments": payload.arguments}
    result = await execute_tool_call(
        registry, tool_call, db=db, confirmed=True,
    )
    if payload.request_id:
        artifact_failure_mode = None
        terminal_state = "artifact_created"
        completed_successfully = True
        if result.success and not result.artifact_refs:
            artifact_failure_mode = _ARTIFACT_FAILURE_REF_MISSING
            terminal_state = "artifact_created_ref_missing"
            completed_successfully = False
        elif not result.success:
            artifact_failure_mode = _ARTIFACT_FAILURE_WRITE_FAILED
            terminal_state = "write_failed"
            completed_successfully = False

        await update_request_trace_after_confirmation(
            db,
            request_id=payload.request_id,
            artifact_refs=result.artifact_refs,
            failure_mode=artifact_failure_mode,
            completed_successfully=completed_successfully,
            artifact_funnel={
                "write_attempted": True,
                "artifact_row_created": bool(result.success),
                "artifact_ref_emitted": bool(result.artifact_refs),
                "terminal_state": terminal_state,
            },
            stage_name="artifact_confirmation_result",
        )

    return {
        "ok": True,
        "result": {
            "success": result.success,
            "message": result.message,
            "data": result.data,
            "artifact_refs": result.artifact_refs,
        },
    }


# ── Tool registry info endpoint ──────────────────────────────────────────────


@router.get("/api/v1/tools", dependencies=[Depends(require_admin)])
async def api_list_tools():
    """List available tools and their schemas (for admin/debug)."""
    registry = await _ensure_tool_registry()
    if not registry:
        return {"tools": [], "count": 0}

    workflows_config = _load_workflows_config()
    tools = registry.list_tools(config=workflows_config)
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "category": t.category.value,
                "mutates": t.mutates,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        "enum": p.enum,
                    }
                    for p in t.parameters
                ],
            }
            for t in tools
        ],
        "count": len(tools),
        "summary": registry.summary(),
    }


# ── Concern threads endpoints ────────────────────────────────────────────────


@router.get("/api/v1/concerns", dependencies=[Depends(require_admin)])
async def api_list_concerns(db=Depends(get_db)):
    """List active concern threads for dashboard and chat UI."""
    from services.interaction_memory import InteractionMemory

    memory = InteractionMemory(db)
    concerns = await memory.get_active_concerns(limit=20)
    return {"success": True, "concerns": concerns}


@router.post("/api/v1/concerns/{concern_id}/resolve", dependencies=[Depends(require_admin)])
async def api_resolve_concern(concern_id: int, db=Depends(get_db)):
    """Mark a concern thread as resolved."""
    from services.interaction_memory import InteractionMemory

    memory = InteractionMemory(db)
    ok = await memory.mark_concern_resolved(concern_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Concern not found or already resolved")
    return {"success": True}


# ── Tool activity endpoint (for dashboard stat + activity feed) ──────────────


@router.get("/api/v1/tool-activity", dependencies=[Depends(require_admin)])
async def api_tool_activity(limit: int = 50, db=Depends(get_db)):
    """Return recent tool executions and summary stats."""
    try:
        async with db.acquire() as conn:
            # Count this week's executions
            async with conn.execute(
                "SELECT COUNT(*) FROM tool_executions WHERE executed_at >= datetime('now', '-7 days')"
            ) as cur:
                row = await cur.fetchone()
                week_count = row[0] if row else 0

            # Recent executions
            async with conn.execute(
                """SELECT tool_name, arguments, success, message,
                          artifact_refs, source, confirmed_by_user, executed_at
                   FROM tool_executions
                   ORDER BY executed_at DESC LIMIT ?""",
                (min(limit, 200),),
            ) as cur:
                rows = await cur.fetchall()

        executions = [
            {
                "tool_name": r[0],
                "arguments": r[1],
                "success": bool(r[2]),
                "message": r[3],
                "artifact_refs": r[4],
                "source": r[5],
                "confirmed_by_user": r[6],
                "executed_at": r[7],
            }
            for r in rows
        ]
        return {
            "success": True,
            "week_count": week_count,
            "executions": executions,
            "total": len(executions),
        }
    except Exception as exc:
        logger.debug("tool_executions table not available: %s", exc)
        return {"success": True, "week_count": 0, "executions": [], "total": 0}
