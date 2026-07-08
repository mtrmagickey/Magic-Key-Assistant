"""Retrieval & Inference Debug router — viewer for RAG pipeline traces,
inference call log, llama.cpp engine logs, and model playground."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from admin.dependencies import require_admin, templates

logger = logging.getLogger("AdminServer.retrieval_log")
router = APIRouter(tags=["retrieval_log"], dependencies=[Depends(require_admin)])


# ── Page route ───────────────────────────────────────────────────────────────

@router.get("/retrieval-log", response_class=HTMLResponse)
async def retrieval_log_page(request: Request):
    return templates.TemplateResponse(
        request, "retrieval_log.html", {"active_page": "retrieval_log"},
    )


# ── API: Retrieval traces ────────────────────────────────────────────────────

@router.get("/api/v1/retrieval/traces")
async def api_retrieval_traces(limit: int = Query(20, ge=1, le=100)):
    """Return recent retrieval traces (newest first)."""
    from services.retrieval_debug_log import retrieval_logger
    return {"success": True, "traces": retrieval_logger.get_traces(limit=limit)}


@router.get("/api/v1/retrieval/traces/{trace_id}")
async def api_retrieval_trace_detail(trace_id: str):
    """Return a single retrieval trace by ID."""
    from services.retrieval_debug_log import retrieval_logger
    trace = retrieval_logger.get_trace(trace_id)
    if trace is None:
        return {"success": False, "error": "Trace not found"}
    return {"success": True, "trace": trace}


@router.delete("/api/v1/retrieval/traces")
async def api_clear_traces():
    """Clear all stored traces."""
    from services.retrieval_debug_log import retrieval_logger
    retrieval_logger.clear()
    return {"success": True, "message": "All traces cleared"}


# ── API: Inference call log ──────────────────────────────────────────────────

@router.get("/api/v1/inference/recent")
async def api_inference_recent(limit: int = Query(50, ge=1, le=200)):
    """Return recent individual LLM inference calls with model/latency/tokens."""
    try:
        from admin.dependencies import get_db
        db = get_db()
        from services.inference_cost_tracker import InferenceCostTracker
        tracker = InferenceCostTracker(db)
        entries = await tracker.get_recent_entries(limit)
        return {"success": True, "entries": entries}
    except Exception as e:
        logger.warning("Failed to get recent inference entries: %s", e)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# ── API: llama.cpp engine logs ───────────────────────────────────────────────

@router.get("/api/v1/inference/engine-logs")
async def api_engine_logs(tail: int = Query(80, ge=10, le=500)):
    """Return the last N lines from the llama-server log file."""
    try:
        from services.llamacpp_manager import get_llamacpp_manager
        manager = get_llamacpp_manager()
        log_path = manager._base_dir / "llama-server.log"
        if not log_path.exists():
            return {"success": True, "lines": [], "message": "No llama-server log file found"}
        text = log_path.read_text(errors="replace")
        all_lines = text.splitlines()
        lines = all_lines[-tail:]
        return {
            "success": True,
            "lines": lines,
            "total_lines": len(all_lines),
            "showing": len(lines),
        }
    except Exception as e:
        logger.warning("Failed to read engine logs: %s", e)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# ── API: Model playground ────────────────────────────────────────────────────

@router.post("/api/v1/inference/playground")
async def api_playground(request: Request):
    """Send a freeform prompt to a specific backend/model and return the raw
    response with latency and token estimates.

    Body: {"backend": "ollama", "model": "qwen2.5:32b", "prompt": "...",
           "system_prompt": "...", "temperature": 0.5, "max_tokens": 2000}
    """
    import time as _time

    try:
        body = await request.json()
    except Exception:
        return {"success": False, "error": "Invalid JSON body"}

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "No prompt provided"}
    if len(prompt) > 20000:
        return {"success": False, "error": "Prompt too long (max 20 000 chars)"}

    backend_name = body.get("backend", "").strip()
    model = body.get("model", "").strip()
    system_prompt = (body.get("system_prompt") or "").strip() or None
    temperature = float(body.get("temperature", 0.5))
    max_tokens = int(body.get("max_tokens", 2000))

    # Clamp values
    temperature = max(0.0, min(2.0, temperature))
    max_tokens = max(64, min(8000, max_tokens))

    from admin.dependencies import get_model_router
    mr = get_model_router()
    if not mr:
        return {"success": False, "error": "Model router not initialized"}

    # Resolve backend/model if not specified
    if not backend_name:
        if mr.pipeline and mr.pipeline.roles:
            from services.model_router import PipelineRole
            initial = mr.pipeline.roles.get(PipelineRole.INITIAL)
            if initial:
                backend_name = initial.backend_name
                model = model or initial.model
        if not backend_name:
            # First available backend
            if mr.backends:
                backend_name = next(iter(mr.backends))

    if not backend_name or backend_name not in mr.backends:
        return {"success": False, "error": f"Unknown backend: {backend_name!r}"}

    if not model:
        cfg = mr.backends[backend_name]
        model = cfg.default_model or (cfg.available_models[0] if cfg.available_models else "")
    if not model:
        return {"success": False, "error": "No model specified or available on backend"}

    t0 = _time.monotonic()
    try:
        result = await mr.generate_single(
            backend_name=backend_name,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}
    latency_ms = int((_time.monotonic() - t0) * 1000)

    # Rough token estimates
    input_tokens = max(1, len((system_prompt or "") + prompt) // 4)
    output_tokens = max(1, len(result) // 4)

    return {
        "success": True,
        "response": result,
        "backend": backend_name,
        "model": model,
        "temperature": temperature,
        "latency_ms": latency_ms,
        "est_input_tokens": input_tokens,
        "est_output_tokens": output_tokens,
    }
