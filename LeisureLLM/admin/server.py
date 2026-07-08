"""
Admin GUI server â€” Application factory and router orchestration.

All endpoints live in ``admin.routers.*``.  This module wires them together,
handles startup/shutdown lifecycle, and exposes the ``app`` + ``set_bot_instance``
entry-points consumed by ``leisureLLM.py``.
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import markdown as _markdown
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# â”€â”€ Path boot-strap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
BASE_DIR = Path(__file__).parent.resolve()
LEISURELLM_DIR = BASE_DIR.parent
sys.path.insert(0, str(LEISURELLM_DIR))

from services.audit_context import clear_audit_context, set_audit_context
from services.model_router import (
    BackendConfig,
    BackendType,
    ModelRouter,
    PipelineConfig,
    PipelineRole,
    RoleConfig,
)
from services.request_tracing import new_request_id

from admin.dependencies import (
    CONFIG_DIR,
    ROUTER_CONFIG_PATH,
    STATIC_DIR,
    _ensure_admin_token,
    admin_auth_enabled,
    get_bot,
    get_current_actor_optional,
    get_db,
    get_model_router,
    get_web_identity_service,
    is_first_run,
    require_admin,
    set_model_router,
    templates,
)
from admin.performance import describe_cache, peek_cache, record_timing, snapshot_metrics

# Only add a basic handler when the root logger has none (i.e. standalone
# invocation).  When launched via leisureLLM.py the rotating file handler
# is already configured and we must not clobber it.
if not logging.root.handlers:
    logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AdminServer")

# â”€â”€ App creation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app = FastAPI(title="Leisure Center Admin")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def _root_favicon():
    """Serve favicon at root so Edge/Chrome --app= mode finds it."""
    from fastapi.responses import FileResponse
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    from fastapi.responses import Response
    return Response(status_code=204)


# ── CSRF Middleware ────────────────────────────────────────────────────────────

_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_HEADER = "x-csrf-protection"  # any non-empty value


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests from cross-origin contexts.

    Requires a custom header (``X-CSRF-Protection: 1``) on all
    non-safe requests.  Browsers enforce CORS preflight for custom
    headers, making this equivalent to a synchroniser token without
    the server-side state.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method not in _CSRF_SAFE_METHODS:
            # Allow unauthenticated endpoints (login, setup)
            if request.url.path not in ("/api/v1/auth/login", "/api/v1/auth/bootstrap", "/api/v1/auth/logout"):
                if not request.headers.get(_CSRF_HEADER):
                    return JSONResponse(
                        {"success": False, "error": "Missing X-CSRF-Protection header"},
                        status_code=403,
                    )
        return await call_next(request)


app.add_middleware(CSRFMiddleware)


# ── Security Headers Middleware ───────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data:; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self'; frame-ancestors 'none';",
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


class RequestAuditContextMiddleware(BaseHTTPMiddleware):
    """Attach correlation metadata to each web request for durable audit logging."""

    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("X-Request-ID", "").strip() or new_request_id()
        request.state.request_id = correlation_id
        token = set_audit_context(surface="web", correlation_id=correlation_id)
        try:
            response: Response = await call_next(request)
        finally:
            clear_audit_context(token)
        response.headers.setdefault("X-Request-ID", correlation_id)
        return response


app.add_middleware(RequestAuditContextMiddleware)


def _dashboard_ollama_placeholder() -> dict:
    return {
        "pending": True,
        "installed": None,
        "running": None,
        "version": None,
        "model_count": None,
        "models": [],
    }


def _dashboard_router_backends() -> dict:
    mr = get_model_router()
    if not mr:
        return {}
    backends = {}
    for name, cfg in mr.backends.items():
        backends[name] = {
            "type": cfg.backend_type.value,
            "name": cfg.name,
            "models": cfg.available_models,
            "default_model": cfg.default_model,
            "endpoint": cfg.endpoint_url,
        }
    return backends






# ── Rate Limiting (login endpoint — stricter) ─────────────────────────────────

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_RATE_LIMIT = 5       # max attempts
_LOGIN_RATE_WINDOW = 300    # per 5-minute window

# â”€â”€ Include domain routers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from admin.router_registry import iter_router_modules  # noqa: E402

for _, _module in iter_router_modules():
    app.include_router(_module.router)


# â”€â”€ Cloud backend (re-)registration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def _register_cloud_backends_from_secrets() -> None:
    """(Re)register cloud backends based on stored secrets."""
    mr = get_model_router()
    if not mr:
        return

    from services.secrets import get_secrets_manager
    secrets = get_secrets_manager()

    openai_key = secrets.get("openai") or os.environ.get("OPENAI_API_KEY")
    anthropic_key = secrets.get("anthropic")
    openrouter_key = secrets.get("openrouter")

    async def replace_backend(config: BackendConfig) -> None:
        existing = mr.clients.get(config.name)
        if existing and hasattr(existing, "close"):
            try:
                await existing.close()
            except Exception as e:
                logger.warning("replace_backend: suppressed %s", e)
        mr.backends.pop(config.name, None)
        mr.clients.pop(config.name, None)
        await mr.register_backend(config)

    for key_val, btype, name in [
        (openai_key, BackendType.OPENAI, "openai"),
        (anthropic_key, BackendType.ANTHROPIC, "anthropic"),
        (openrouter_key, BackendType.OPENROUTER, "openrouter"),
    ]:
        if key_val:
            try:
                await replace_backend(BackendConfig(backend_type=btype, name=name, api_key=key_val))
                logger.info("%s backend registered (from secrets/config)", name)
            except Exception as e:
                logger.warning("Could not register %s: %s", name, e)


# â”€â”€ Pipeline helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _set_default_pipeline() -> None:
    mr = get_model_router()
    if not mr:
        return
    local_model = None
    if "ollama" in mr.backends:
        models = mr.backends["ollama"].available_models
        local_model = "qwen2.5:32b" if "qwen2.5:32b" in models else (models[0] if models else None)
    roles = {}
    if local_model:
        roles[PipelineRole.INITIAL] = RoleConfig(role=PipelineRole.INITIAL, backend_name="ollama", model=local_model, temperature=0.4)
        roles[PipelineRole.CRITIQUE] = RoleConfig(role=PipelineRole.CRITIQUE, backend_name="ollama", model=local_model, temperature=0.2)
        roles[PipelineRole.SYNTHESIZE] = RoleConfig(role=PipelineRole.SYNTHESIZE, backend_name="ollama", model=local_model, temperature=0.3)
    elif "openai" in mr.backends:
        roles[PipelineRole.INITIAL] = RoleConfig(role=PipelineRole.INITIAL, backend_name="openai", model="gpt-4o-mini", temperature=0.4)
        roles[PipelineRole.CRITIQUE] = RoleConfig(role=PipelineRole.CRITIQUE, backend_name="openai", model="gpt-4o-mini", temperature=0.2)
        roles[PipelineRole.SYNTHESIZE] = RoleConfig(role=PipelineRole.SYNTHESIZE, backend_name="openai", model="gpt-4o-mini", temperature=0.3)
    if roles:
        mr.configure_pipeline(PipelineConfig(name="default", roles=roles))


async def _load_pipeline_from_file() -> None:
    mr = get_model_router()
    if not mr or not ROUTER_CONFIG_PATH.exists():
        return
    with open(ROUTER_CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    pipeline_data = config.get("pipeline", config)
    roles = {}
    for role_str, rd in pipeline_data.get("roles", {}).items():
        if not rd.get("enabled", True):
            continue
        bn = rd.get("backend_name")
        if bn and bn not in mr.backends:
            logger.warning("Skipping role '%s' â€” backend '%s' not registered", role_str, bn)
            continue
        backend = mr.backends.get(bn) if bn else None
        model_name = rd.get("model")
        if backend and backend.available_models and model_name not in backend.available_models:
            logger.warning(
                "Skipping role '%s' — model '%s' not available on '%s'",
                role_str,
                model_name,
                bn,
            )
            continue
        role = PipelineRole(role_str)
        roles[role] = RoleConfig(
            role=role, backend_name=rd["backend_name"], model=rd["model"],
            temperature=rd.get("temperature", 0.3), max_tokens=rd.get("max_tokens", 4000),
            system_prompt_override=rd.get("system_prompt_override"), enabled=rd.get("enabled", True),
        )
    if roles:
        mr.configure_pipeline(PipelineConfig(name=pipeline_data.get("name", "loaded"), roles=roles))
    else:
        logger.warning("No valid pipeline roles found in config; will use defaults")


# â”€â”€ Lifecycle events â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.on_event("startup")
async def startup_event():
    # Surface admin token so Docker users can find it via `docker compose logs admin`
    if admin_auth_enabled():
        _ensure_admin_token()
        logger.info("Web auth is ON.")
        logger.info("  Bootstrap token file : %s", CONFIG_DIR / ".admin_token")
        logger.info("  Docker hint: docker exec magickey-admin cat /app/LeisureLLM/config/.admin_token")
    else:
        logger.info("Web auth is DISABLED (ADMIN_AUTH_DISABLED is set).")

    # Ensure the standalone DB is connected (needed when running
    # 'python -m admin.server' without leisureLLM.py, e.g. first-run setup).
    from admin.dependencies import connect_standalone_db
    await connect_standalone_db()

    mr = ModelRouter()
    set_model_router(mr)

    # Share this same router with the RAG pipeline so that the web chat,
    # Discord cog, and admin API all use the single authoritative instance.
    from services.rag_pipeline import set_pipeline_router
    set_pipeline_router(mr)

    try:
        ok = await mr.register_backend(BackendConfig(
            backend_type=BackendType.OLLAMA, name="ollama",
            endpoint_url="http://localhost:11434",
        ))
        if ok:
            logger.info("Ollama backend registered with models: %s", mr.backends["ollama"].available_models)

            # ── Evergreen: catalog update + auto-pull missing models ─────
            import asyncio

            from services.model_discovery import startup_model_refresh

            async def _run_model_refresh():
                try:
                    result = await startup_model_refresh()
                    cat = result.get("catalog_update", {})
                    pull = result.get("auto_pull", {})
                    if cat.get("added"):
                        logger.info(
                            "Startup catalog: %d new model(s) discovered",
                            len(cat["added"]),
                        )
                    if pull.get("pulled"):
                        logger.info(
                            "Startup auto-pull: %s",
                            ", ".join(pull["pulled"]),
                        )
                except Exception as exc:
                    logger.warning("Startup model refresh failed: %s", exc)

            asyncio.create_task(_run_model_refresh())

    except Exception as e:
        logger.warning("Could not connect to Ollama: %s", e)
    await _register_cloud_backends_from_secrets()

    # Wire cost tracker into the model router (moat: cost counter-positioning)
    try:
        from services.inference_cost_tracker import InferenceCostTracker

        from admin.dependencies import get_db
        cost_tracker = InferenceCostTracker(get_db())
        await cost_tracker.ensure_tables()
        mr.set_cost_tracker(cost_tracker)
        logger.info("Cost tracker attached to ModelRouter")
    except Exception as e:
        logger.warning("Could not attach cost tracker: %s", e)

    if ROUTER_CONFIG_PATH.exists():
        try:
            await _load_pipeline_from_file()
            logger.info("Loaded pipeline config from file")
        except Exception as e:
            logger.warning("Could not load pipeline config: %s", e)
    if not mr.pipeline or not mr.pipeline.roles:
        _set_default_pipeline()

    try:
        from core.feedback_learning_runner import FeedbackLearningScheduler
        from core.inbox_recovery_runner import InboxRecoveryScheduler
        from core.operational_continuity_runner import OperationalContinuityScheduler

        from admin.dependencies import get_bot, get_db_optional

        db = get_db_optional()
        if db and get_bot() is None:
            continuity_scheduler = OperationalContinuityScheduler(db)
            continuity_scheduler.start()
            app.state.operational_continuity_scheduler = continuity_scheduler
            logger.info("Started web-only operational continuity scheduler")

            inbox_recovery_scheduler = InboxRecoveryScheduler(db)
            inbox_recovery_scheduler.start()
            app.state.inbox_recovery_scheduler = inbox_recovery_scheduler
            logger.info("Started web-only inbox recovery scheduler")

            feedback_scheduler = FeedbackLearningScheduler(db)
            feedback_scheduler.start()
            app.state.feedback_learning_scheduler = feedback_scheduler
            logger.info("Started web-only feedback learning scheduler")
    except Exception as exc:
        logger.warning("Could not start web-only schedulers: %s", exc)


@app.on_event("shutdown")
async def shutdown_event():
    mr = get_model_router()
    if mr:
        await mr.close()
    scheduler = getattr(app.state, "operational_continuity_scheduler", None)
    if scheduler is not None:
        await scheduler.stop()
    inbox_scheduler = getattr(app.state, "inbox_recovery_scheduler", None)
    if inbox_scheduler is not None:
        await inbox_scheduler.stop()
    feedback_scheduler = getattr(app.state, "feedback_learning_scheduler", None)
    if feedback_scheduler is not None:
        await feedback_scheduler.stop()


# â”€â”€ Dashboard (root page) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# ── Root redirect → Pulse (workspace-first) ──────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root_redirect(request: Request):
    from fastapi.responses import RedirectResponse

    from admin.routers.settings import _build_onboarding_state

    onboarding_state = await _build_onboarding_state(prefer_cached_provider_state=True)
    if is_first_run() and not (onboarding_state.get("setup_complete") or onboarding_state.get("phase1_saved")):
        return RedirectResponse(url="/setup", status_code=302)

    if admin_auth_enabled():
        actor = await get_current_actor_optional(request)
        if actor is None:
            return RedirectResponse(url="/login", status_code=303)

    return RedirectResponse(url="/pulse", status_code=302)


# ── Dashboard (explicit route) ───────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    from fastapi.responses import RedirectResponse

    from admin.routers.settings import _build_model_setup_guidance, _build_onboarding_state

    route_started = time.perf_counter()

    onboarding_state = await _build_onboarding_state(prefer_cached_provider_state=True)
    model_setup_guidance = _build_model_setup_guidance(onboarding_state)
    if is_first_run() and not (onboarding_state.get("setup_complete") or onboarding_state.get("phase1_saved")):
        return RedirectResponse(url="/setup", status_code=302)

    # Dashboard requires auth like everything else
    if admin_auth_enabled():
        actor = await get_current_actor_optional(request)
        if actor is None:
            return RedirectResponse(url="/login", status_code=303)

    # llama.cpp status (optional — gracefully handle if not available)
    llamacpp_status = {"installed": False, "running": False}
    try:
        from services.llamacpp_manager import get_llamacpp_manager
        lcpp = get_llamacpp_manager()
        s = lcpp.get_status()
        llamacpp_status = {"installed": s.installed, "running": s.running,
                           "port": s.port, "model_count": len(s.available_models),
                           "pid": s.pid, "binary_path": s.binary_path}
    except Exception as e:
        logger.warning("dashboard: suppressed %s", e)

    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "ollama_status": peek_cache("ollama_status") or _dashboard_ollama_placeholder(),
            "llamacpp_status": llamacpp_status,
            "onboarding_state": onboarding_state,
            "model_setup_guidance": model_setup_guidance,
            "active_page": "dashboard",
        },
    )
    record_timing("dashboard.render", (time.perf_counter() - route_started) * 1000.0)
    return response


@app.get("/api/v1/dashboard/summary", dependencies=[Depends(require_admin)])
async def api_dashboard_summary(db=Depends(get_db)):
    from services.interaction_memory import InteractionMemory
    from services.secrets import get_secrets_manager

    from admin.routers.artifacts import api_analytics_overview
    from admin.routers.knowledge import get_cached_knowledge_stats
    from admin.routers.settings import build_setup_completion

    started = time.perf_counter()

    try:
        analytics = await api_analytics_overview(db=db)
    except Exception as exc:
        logger.warning("Dashboard analytics summary failed: %s", exc, exc_info=True)
        analytics = {"success": False, "analytics": {}, "error": "analytics_unavailable"}

    try:
        setup = await build_setup_completion(db, prefer_cached_provider_state=True)
    except Exception as exc:
        logger.warning("Dashboard setup summary failed: %s", exc, exc_info=True)
        setup = {
            "success": False,
            "completion_pct": 0,
            "is_complete": False,
            "milestones": [],
            "counts": {},
            "onboarding_state": {},
            "model_setup_guidance": {},
            "error": "setup_summary_unavailable",
        }

    try:
        knowledge = get_cached_knowledge_stats()
    except Exception as exc:
        logger.warning("Dashboard knowledge summary failed: %s", exc, exc_info=True)
        knowledge = {}

    unread_count = 0
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM inbox_threads WHERE status = 'ready'"
            )
            row = await cur.fetchone()
            unread_count = int(row[0]) if row else 0
    except Exception:
        unread_count = 0

    concerns = []
    try:
        concerns = await InteractionMemory(db).get_active_concerns(limit=20)
    except Exception:
        concerns = []

    secrets = get_secrets_manager()
    provider_secrets = {item["env_var"]: item["has_value"] for item in secrets.list_keys()}

    savings = {}
    try:
        from services.inference_cost_tracker import InferenceCostTracker
        tracker = InferenceCostTracker(db)
        savings = await tracker.get_savings_summary(days=30)
    except Exception:
        savings = {}

    open_gaps_count = 0
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM knowledge_gaps WHERE status = 'open'"
            )
            row = await cur.fetchone()
            open_gaps_count = int(row[0]) if row else 0
    except Exception:
        open_gaps_count = 0

    nudges = []
    try:
        from services.proactive_suggestions import get_proactive_engine
        engine = get_proactive_engine(db)
        raw_nudges = await engine.get_suggestions(max_results=4)
        nudges = engine.format_nudges_for_display(raw_nudges)
    except Exception:
        nudges = []

    payload = {
        "success": True,
        "unread_count": unread_count,
        "analytics": analytics.get("analytics", {}) if analytics.get("success") else {},
        "knowledge": knowledge,
        "setup": setup,
        "concerns": concerns,
        "savings": savings,
        "open_gaps_count": open_gaps_count,
        "nudges": nudges,
        "provider_summary": {
            "backends": _dashboard_router_backends(),
            "secrets": provider_secrets,
            "model_setup_guidance": setup.get("model_setup_guidance", {}),
            "onboarding_state": setup.get("onboarding_state", {}),
        },
    }
    record_timing("dashboard.summary", (time.perf_counter() - started) * 1000.0)
    return payload


@app.get("/api/v1/internal/performance", dependencies=[Depends(require_admin)])
async def api_internal_performance():
    return {
        "success": True,
        "timings": snapshot_metrics([
            "dashboard.render",
            "dashboard.summary",
            "ollama.status_probe",
            "knowledge.stats",
            "knowledge.storage",
        ]),
        "cache": describe_cache("ollama_status", "knowledge_stats", "knowledge_storage"),
    }


# ── Auth routes (unauthenticated) ────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the login page — no auth required."""
    next_url = request.query_params.get("next", "")
    bootstrap_required = False
    if admin_auth_enabled():
        try:
            bootstrap_required = not await get_web_identity_service().has_any_accounts()
        except Exception:
            bootstrap_required = True
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_url, "bootstrap_required": bootstrap_required},
    )


class _LoginPayload(BaseModel):
    username: str
    password: str


class _BootstrapPayload(BaseModel):
    bootstrap_token: str
    username: str
    password: str
    display_name: Optional[str] = None


def _set_session_cookie(response: JSONResponse, session_token: str) -> None:
    _is_localhost = os.environ.get("ADMIN_GUI_HOST", "127.0.0.1") in ("127.0.0.1", "localhost")
    response.set_cookie(
        key=get_web_identity_service().session_cookie_name,
        value=session_token,
        httponly=True,
        secure=not _is_localhost,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        path="/",
    )


def _record_login_attempt(client_ip: str) -> Optional[JSONResponse]:
    now = time.time()
    _login_attempts[client_ip] = [
        t for t in _login_attempts[client_ip] if now - t < _LOGIN_RATE_WINDOW
    ]
    if len(_login_attempts) > 1000:
        stale = [ip for ip, ts in _login_attempts.items() if not ts]
        for ip in stale:
            del _login_attempts[ip]
    if len(_login_attempts[client_ip]) >= _LOGIN_RATE_LIMIT:
        return JSONResponse(
            {"success": False, "error": "Too many login attempts. Try again later."},
            status_code=429,
        )
    _login_attempts[client_ip].append(now)
    return None


@app.post("/api/v1/auth/bootstrap")
async def api_auth_bootstrap(payload: _BootstrapPayload, request: Request):
    """Bootstrap the first durable admin account using the local bootstrap token."""
    if not admin_auth_enabled():
        return JSONResponse({"success": True, "auth_enabled": False})

    client_ip = request.client.host if request.client else "unknown"
    rate_limit = _record_login_attempt(client_ip)
    if rate_limit is not None:
        return rate_limit

    service = get_web_identity_service()
    try:
        account = await service.bootstrap_admin(
            bootstrap_token=payload.bootstrap_token,
            expected_bootstrap_token=_ensure_admin_token(),
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
        )
        _, session_token = await service.authenticate(
            username=payload.username,
            password=payload.password,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:
        status_code = 409 if "bootstrap" in str(exc).lower() else 401
        return JSONResponse({"success": False, "error": str(exc)}, status_code=status_code)

    response = JSONResponse(
        {
            "success": True,
            "user": {
                "username": account["username"],
                "display_name": account["display_name"],
                "role": account["role"],
            },
        }
    )
    _set_session_cookie(response, session_token)
    return response


@app.post("/api/v1/auth/login")
async def api_auth_login(payload: _LoginPayload, request: Request):
    """Validate username/password and issue a durable session cookie."""
    if not admin_auth_enabled():
        return JSONResponse({"success": True, "auth_enabled": False})

    client_ip = request.client.host if request.client else "unknown"
    rate_limit = _record_login_attempt(client_ip)
    if rate_limit is not None:
        return rate_limit

    service = get_web_identity_service()
    if not await service.has_any_accounts():
        return JSONResponse(
            {"success": False, "error": "Bootstrap admin required before login.", "bootstrap_required": True},
            status_code=409,
        )

    try:
        account, session_token = await service.authenticate(
            username=payload.username,
            password=payload.password,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=401)

    response = JSONResponse(
        {
            "success": True,
            "user": {
                "username": account["username"],
                "display_name": account["display_name"],
                "role": account["role"],
            },
        }
    )
    _set_session_cookie(response, session_token)
    return response


@app.post("/api/v1/auth/logout")
async def api_auth_logout(request: Request):
    """Revoke the current session cookie."""
    response = JSONResponse({"success": True})
    token = request.cookies.get(get_web_identity_service().session_cookie_name, "")
    try:
        await get_web_identity_service().revoke_session(token)
    except Exception as e:
        logger.warning("api_auth_logout: suppressed %s", e)
    response.delete_cookie(key=get_web_identity_service().session_cookie_name, path="/")
    return response


_LOCALHOST_IPS = frozenset({"127.0.0.1", "::1", "localhost"})


@app.get("/api/v1/auth/reveal-token")
async def api_reveal_token(request: Request):
    """Reveal the bootstrap token for first-admin setup from localhost only.

    Checks both the direct socket address **and** X-Forwarded-For to
    prevent bypass behind a reverse proxy.
    """
    client_ip = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    # If there's a forwarded header, the *real* client IP is the forwarded
    # value — the socket IP is just the proxy.  Both must be localhost.
    if forwarded and forwarded not in _LOCALHOST_IPS:
        return JSONResponse(
            {"success": False, "error": "Only available from this computer."},
            status_code=403,
        )
    if client_ip not in _LOCALHOST_IPS:
        return JSONResponse(
            {"success": False, "error": "Only available from this computer."},
            status_code=403,
        )
    service = get_web_identity_service()
    if await service.has_any_accounts():
        return JSONResponse(
            {"success": False, "error": "Bootstrap is already complete."},
            status_code=409,
        )
    return {"success": True, "token": _ensure_admin_token()}


@app.get("/api/v1/auth/status")
async def api_auth_status(request: Request):
    """Check whether the current session is authenticated."""
    if not admin_auth_enabled():
        return {
            "authenticated": True,
            "auth_enabled": False,
            "bootstrap_required": False,
            "user": {"display_name": "Auth Disabled", "role": "admin", "username": "auth-disabled"},
        }

    service = get_web_identity_service()
    actor = await get_current_actor_optional(request)
    return {
        "authenticated": actor is not None,
        "auth_enabled": True,
        "bootstrap_required": not await service.has_any_accounts(),
        "user": (
            {
                "display_name": actor.display_name,
                "role": actor.role,
                "username": actor.username,
                "actor_kind": actor.actor_kind,
                "actor_stable_id": actor.stable_id,
            }
            if actor is not None
            else None
        ),
    }


# ── Inbox page (replaces Chat) ────────────────────────────────────────────────

@app.get("/inbox", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def inbox_page(request: Request):
    return templates.TemplateResponse(request, "inbox.html", {"active_page": "inbox"})


@app.get("/chat", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def chat_page(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/inbox", status_code=302)



# ── Activity page (requires auth) ────────────────────────────────────────────

@app.get("/activity", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def activity_page(request: Request):
    return templates.TemplateResponse(request, "activity.html", {"active_page": "activity"})


# ── Jobs page (requires auth) ────────────────────────────────────────────────

@app.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def jobs_page(request: Request):
    return templates.TemplateResponse(request, "jobs.html", {"active_page": "jobs"})


@app.get("/system", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def system_page(request: Request):
    return templates.TemplateResponse(request, "system.html", {"active_page": "system"})


@app.get("/api/v1/jobs", dependencies=[Depends(require_admin)])
async def api_list_jobs():
    """Return all registered jobs from the job registry, enriched with last_run data."""
    from core.job_registry import JOB_REGISTRY

    # Try to fetch last_run timestamps from the DB
    last_runs: dict[str, dict] = {}
    db = get_db()
    if db:
        try:
            async with db.acquire() as conn, conn.execute(
                """
                    SELECT job_name,
                           MAX(started_at) AS last_started,
                           status
                    FROM job_runs
                    GROUP BY job_name
                    """
            ) as cur:
                for row in await cur.fetchall():
                    last_runs[row[0]] = {"last_run": row[1], "last_status": row[2]}
        except Exception:
            pass  # table may not exist yet

    jobs = []
    for name, meta in JOB_REGISTRY.items():
        entry = {
            "name": name,
            "schedule": meta.schedule,
            "module": meta.module,
            "cog": meta.cog,
            "manual_trigger": meta.manual_trigger,
            "description": meta.description,
            "requires_accelerators": meta.requires_accelerators,
        }
        lr = last_runs.get(name)
        if lr:
            entry["last_run"] = lr["last_run"]
            entry["last_status"] = lr["last_status"]
        jobs.append(entry)
    return {"success": True, "jobs": jobs, "total": len(jobs)}


@app.post("/api/v1/jobs/{job_name}/run", dependencies=[Depends(require_admin)])
async def api_run_job(job_name: str):
    """Manually trigger a registered job (requires bot in team mode, or standalone for memory jobs)."""
    from core.job_registry import JOB_REGISTRY

    if job_name not in JOB_REGISTRY:
        return JSONResponse({"success": False, "error": f"Unknown job: {job_name}"}, status_code=404)

    meta = JOB_REGISTRY[job_name]
    if not meta.manual_trigger:
        return JSONResponse({"success": False, "error": f"Job '{job_name}' is not manually triggerable."}, status_code=400)

    if job_name == "operational_continuity_sweep":
        try:
            from core.operational_continuity_runner import OperationalContinuityScheduler

            db = get_db()
            result = await OperationalContinuityScheduler(db).run_once(triggered_by="manual")
            summary = result.get("result", {}) if isinstance(result, dict) else {}
            return {
                "success": True,
                "message": "Operational continuity sweep completed",
                "job": result,
                "summary": summary,
            }
        except Exception as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    if job_name == "inbox_stalled_thread_sweep":
        try:
            from core.inbox_recovery_runner import InboxRecoveryScheduler

            db = get_db()
            result = await InboxRecoveryScheduler(db).run_once(triggered_by="manual")
            summary = result.get("result", {}) if isinstance(result, dict) else {}
            return {
                "success": True,
                "message": "Inbox stalled-thread sweep completed",
                "job": result,
                "summary": summary,
            }
        except Exception as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    # Special case: memory sync jobs can run without the bot
    if job_name == "daily_knowledge_refresh":
        try:
            import asyncio

            from cogs.ingest_metadata import run_ingest

            _, stats, _ = await asyncio.to_thread(run_ingest)
            # Record in job_runs
            db = get_db()
            if db:
                try:
                    async with db.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO job_runs (job_name, run_date, started_at, completed_at, status, triggered_by) VALUES (?, date('now'), datetime('now'), datetime('now'), 'completed', 'manual')",
                            (job_name,),
                        )
                        await conn.commit()
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)
            return {"success": True, "message": f"Knowledge refresh complete: {stats.get('added_files', 0)} added, {stats.get('updated_files', 0)} updated"}
        except Exception as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    # For all other jobs: need the bot instance with the cog loaded
    bot = get_bot()
    if not bot:
        return JSONResponse(
            {"success": False, "error": "Bot is not running. Manual job trigger requires team mode with Discord connected."},
            status_code=503,
        )

    cog = bot.get_cog(meta.cog)
    if not cog:
        return JSONResponse({"success": False, "error": f"Cog '{meta.cog}' is not loaded."}, status_code=503)

    method = getattr(cog, job_name, None)
    if not method:
        return JSONResponse({"success": False, "error": f"Method '{job_name}' not found on cog '{meta.cog}'."}, status_code=500)

    # Fire the job in the background
    import asyncio
    asyncio.create_task(_run_job_task(method, job_name))
    return {"success": True, "message": f"Job '{job_name}' triggered. Check Activity for results."}


async def _run_job_task(method, job_name: str):
    """Background wrapper to run a cog job method and record result."""
    try:
        await method()
        logger.info("Manual job '%s' completed", job_name)
    except Exception as exc:
        logger.error("Manual job '%s' failed: %s", job_name, exc)


# ── Data Explorer page (requires auth) ───────────────────────────────────────

@app.get("/explorer", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def explorer_page(request: Request):
    return templates.TemplateResponse(request, "explorer.html", {"active_page": "explorer"})


# ── Guide page (in-app documentation) ────────────────────────────────────────

# Project root is two levels above LeisureLLM/admin/
_PROJECT_ROOT = LEISURELLM_DIR.parent

# Available documentation files, keyed by slug
_GUIDE_DOCS = {
    "readme":        ("Overview",               "README.md"),
    "get-started":   ("Getting Started",     "GET_STARTED.md"),
    "installation":  ("Installation",        "INSTALLATION.md"),
    "architecture":  ("Architecture",        "docs/architecture/system.md"),
    "local-llm":     ("Local LLM",           "docs/architecture/local-llm.md"),
    "security":      ("Security",            "SECURITY.md"),
}


def _render_guide_markdown(md_path: Path) -> str:
    """Read a Markdown file and convert to HTML."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "<p>Documentation file not found.</p>"
    # Convert Markdown → HTML with tables and fenced code support
    return _markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "nl2br"],
    )


@app.get("/guide", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def guide_page(request: Request, doc: str | None = None):
    """Render in-app documentation.  ?doc=readme (default), get-started, etc."""
    from admin.routers.settings import _build_model_setup_guidance, _build_onboarding_state

    onboarding_state = await _build_onboarding_state()
    model_setup_guidance = _build_model_setup_guidance(onboarding_state)

    # Default to getting-started for users who haven't finished setup
    if doc is None:
        if model_setup_guidance.get("state_key") != "phase1_saved":
            doc = "get-started"
        else:
            doc = "readme"
    if doc not in _GUIDE_DOCS:
        doc = "readme"
    title, filename = _GUIDE_DOCS[doc]
    md_path = _PROJECT_ROOT / filename
    guide_html = _render_guide_markdown(md_path)

    # Build list for the doc switcher
    doc_list = [
        {"slug": slug, "title": t, "active": slug == doc}
        for slug, (t, _) in _GUIDE_DOCS.items()
        if (_PROJECT_ROOT / _).exists()
    ]

    return templates.TemplateResponse(
        request,
        "guide.html",
        {
            "active_page": "guide",
            "guide_html": guide_html,
            "doc_list": doc_list,
            "current_doc_title": title,
            "onboarding_state": onboarding_state,
            "model_setup_guidance": model_setup_guidance,
        },
    )


@app.get("/api/v1/explorer/tables", dependencies=[Depends(require_admin)])
async def api_explorer_tables():
    """List all tables and their row counts."""
    db = get_db()
    async with db.acquire() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        result = []
        for tbl in tables:
            try:
                cnt = await conn.execute(f"SELECT COUNT(*) FROM [{tbl}]")  # noqa: S608
                count = (await cnt.fetchone())[0]
            except Exception:
                count = -1
            result.append({"name": tbl, "row_count": count})
    return {"success": True, "tables": result}


@app.get("/api/v1/explorer/{table}", dependencies=[Depends(require_admin)])
async def api_explorer_query(
    table: str,
    request: Request,
    limit: int = 50,
    offset: int = 0,
    sort: str = "rowid",
    order: str = "desc",
):
    """Read-only paginated query on a table with optional column filters."""
    import re

    # Sanitise table/sort names to prevent injection
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table):
        return JSONResponse({"success": False, "error": "Invalid table name"}, status_code=400)
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", sort):
        sort = "rowid"
    if order.lower() not in ("asc", "desc"):
        order = "desc"
    limit = min(max(1, limit), 500)

    db = get_db()
    async with db.acquire() as conn:
        # Verify table exists
        chk = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if not await chk.fetchone():
            return JSONResponse({"success": False, "error": f"Table '{table}' not found"}, status_code=404)

        # Get column info
        info = await conn.execute(f"PRAGMA table_info([{table}])")
        columns = [{"name": row[1], "type": row[2]} for row in await info.fetchall()]
        col_names = [c["name"] for c in columns]

        if sort not in col_names and sort != "rowid":
            sort = "rowid"

        # Build WHERE clause from query params (?col=value)
        where_parts = []
        params = []
        for col in col_names:
            val = request.query_params.get(col)
            if val is not None:
                where_parts.append(f"[{col}] LIKE ?")
                params.append(f"%{val}%")

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        count_cursor = await conn.execute(
            f"SELECT COUNT(*) FROM [{table}]{where_sql}", params  # noqa: S608
        )
        total = (await count_cursor.fetchone())[0]

        query = (
            f"SELECT * FROM [{table}]{where_sql} ORDER BY [{sort}] {order} LIMIT ? OFFSET ?"  # noqa: S608
        )
        cursor = await conn.execute(query, params + [limit, offset])
        rows = []
        for row in await cursor.fetchall():
            rows.append({col_names[i]: row[i] for i in range(len(col_names))})

    return {
        "success": True,
        "table": table,
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }



if __name__ == "__main__":
    uvicorn.run("admin.server:app", host="127.0.0.1", port=8000, reload=True)

