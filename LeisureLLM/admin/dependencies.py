"""
Shared dependencies for the admin GUI routers.

Provides accessor functions for:
- Bot instance (injected by leisureLLM.py at startup)
- Jinja2 templates
- Model router instance
- Common paths
- Admin auth gate
"""

import logging
import os
import secrets as _secrets_mod
from pathlib import Path
from typing import Optional

from core.actors import ActorContext
from core.app_metadata import get_app_version
from core.services.web_identity_service import WebIdentityService
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from services.audit_context import update_audit_context

logger = logging.getLogger("AdminServer")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
LEISURELLM_DIR = BASE_DIR.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
CONFIG_DIR = LEISURELLM_DIR / "config"
ROUTER_CONFIG_PATH = CONFIG_DIR / "model_router.json"
PROMPTS_DIR = LEISURELLM_DIR / "prompts"

# ── Singleton instances ───────────────────────────────────────────────────────
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Make operation_mode available in every template automatically
from config import OPERATION_MODE as _OP_MODE  # noqa: E402

templates.env.globals["operation_mode"] = _OP_MODE

# Make bot_name available in every template (from org_profile.yaml)
try:
    from core.config_loader import OrgProfile as _OrgProfile
    _bot_name = _OrgProfile.load().bot_name
except Exception:
    _bot_name = "Magic Key Assistant"
templates.env.globals["bot_name"] = _bot_name

_app_version = get_app_version()
templates.env.globals["app_version"] = _app_version

_bot_instance = None
_model_router = None  # services.model_router.ModelRouter


def set_bot_instance(bot) -> None:
    """Called by leisureLLM.py to provide access to the bot instance."""
    global _bot_instance
    _bot_instance = bot


def get_bot():
    """Return the bot instance, or None if not connected yet."""
    return _bot_instance


def get_db():
    """Return us a Database wrapper for admin routes.

    Resolution order:
    1. The bot's connected ``bot.db`` (team or solo mode via leisureLLM.py).
    2. A standalone Database instance created lazily (first-run setup when
       only ``admin.server`` is running, without the full bot).
    """
    bot = get_bot()
    if bot and hasattr(bot, "db") and bot.db:
        return bot.db

    # Fallback: standalone DB for first-run / admin-only mode
    return _get_standalone_db()


# ── Standalone DB (lazy, created on first access) ─────────────────────────────
_standalone_db = None


def _standalone_db_path() -> str:
    configured = os.getenv("DATABASE_PATH")
    if configured:
        return configured
    return str(LEISURELLM_DIR / "assistant.db")


async def _init_standalone_db():
    """Create and connect a standalone Database instance."""
    from database import Database
    db_path = _standalone_db_path()
    db = Database(db_path)
    await db.connect()
    return db


def _get_standalone_db():
    """Return the standalone DB.

    On first call the DB has not been connected yet — we register a
    FastAPI startup hook so it gets connected once the event loop is
    running.  Until that hook fires we return the unconnected instance
    (callers who ``await db.acquire()`` will get a clear error instead
    of a 503 "not available").
    """
    global _standalone_db
    if _standalone_db is None:
        from database import Database
        db_path = _standalone_db_path()
        _standalone_db = Database(db_path)
    return _standalone_db


async def connect_standalone_db():
    """Connect the standalone DB.  Called from server.py's lifespan hook."""
    global _standalone_db
    if _standalone_db is None:
        _standalone_db = _get_standalone_db()
    if _standalone_db is not None and not _standalone_db._is_healthy:
        try:
            await _standalone_db.connect()
            logger.info("Standalone admin DB connected: %s", _standalone_db.database_path)
        except Exception as exc:
            logger.error("Standalone admin DB failed: %s", exc)


def get_db_optional():
    """Return a Database instance if available, else None.

    Tries the bot's DB first, then the standalone fallback.
    """
    bot = get_bot()
    if bot and hasattr(bot, "db") and bot.db:
        return bot.db
    # Standalone fallback (may or may not be connected)
    if _standalone_db and _standalone_db._is_healthy:
        return _standalone_db
    return None


def set_model_router(router) -> None:
    global _model_router
    _model_router = router


def get_model_router():
    return _model_router


def get_web_identity_service(db=None) -> WebIdentityService:
    return WebIdentityService(db or get_db())


# ── Admin auth ────────────────────────────────────────────────────────────────
_ADMIN_TOKEN_PATH = CONFIG_DIR / ".admin_token"


def _ensure_admin_token() -> str:
    """Return the admin token, generating one on first access."""
    if _ADMIN_TOKEN_PATH.exists():
        return _ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = _secrets_mod.token_urlsafe(32)
    _ADMIN_TOKEN_PATH.write_text(token, encoding="utf-8")
    logger.info(
        "Generated admin token → %s  (store this somewhere safe)",
        _ADMIN_TOKEN_PATH,
    )
    return token


def admin_auth_enabled() -> bool:
    """Whether the web console requires login.

    Auth is OFF by default when the GUI is bound to localhost (127.0.0.1
    or ``localhost``).  This covers both solo and team mode — the admin
    dashboard is accessed by one person at the keyboard.

    Auth turns ON automatically when ``ADMIN_GUI_HOST`` is set to
    something other than localhost (e.g. ``0.0.0.0``), because the
    dashboard is then reachable from the network.

    Explicit overrides:
      ``ADMIN_AUTH_DISABLED=1`` → force OFF in any mode
      ``ADMIN_AUTH_ENABLED=1``  → force ON in any mode
    """
    explicit_disabled = os.environ.get("ADMIN_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    if explicit_disabled:
        return False
    explicit_enabled = os.environ.get("ADMIN_AUTH_ENABLED", "").lower() in ("1", "true", "yes")
    if explicit_enabled:
        return True
    # Only require login when the GUI is exposed beyond localhost
    host = os.environ.get("ADMIN_GUI_HOST", "127.0.0.1").strip()
    if host in ("127.0.0.1", "localhost", "::1"):
        return False
    return True


def _auth_disabled_actor() -> ActorContext:
    return ActorContext(
        actor_id=0,
        stable_id="actor_auth_disabled",
        actor_kind="system",
        external_ref="admin-auth-disabled",
        display_name="Auth Disabled",
        role="admin",
        username="auth-disabled",
        auth_source="auth_disabled",
    )


def _extract_session_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    cookie_token = request.cookies.get(WebIdentityService.session_cookie_name, "").strip()
    if cookie_token:
        return cookie_token
    return None


async def get_current_actor_optional(request: Request) -> Optional[ActorContext]:
    cached = getattr(request.state, "current_actor", None)
    if cached is not None:
        update_audit_context(actor_id=cached.actor_id)
        return cached

    if not admin_auth_enabled():
        actor = _auth_disabled_actor()
        request.state.current_actor = actor
        update_audit_context(actor_id=actor.actor_id)
        return actor

    token = _extract_session_token(request)
    if not token:
        return None

    actor = await get_web_identity_service().get_session_actor(token)
    if actor is not None:
        request.state.current_actor = actor
        update_audit_context(actor_id=actor.actor_id)
        return actor
    return None


async def require_authenticated_actor(request: Request) -> ActorContext:
    actor = await get_current_actor_optional(request)
    if actor is not None:
        return actor

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        import urllib.parse

        next_url = urllib.parse.quote(str(request.url.path), safe="")
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={next_url}"},
        )

    raise HTTPException(status_code=401, detail="Authenticated session required")


async def _require_role(request: Request, minimum_role: str) -> ActorContext:
    actor = await require_authenticated_actor(request)
    if actor.has_role(minimum_role):
        return actor
    raise HTTPException(status_code=403, detail=f"{minimum_role} role required")


async def get_current_actor(request: Request) -> ActorContext:
    return await require_authenticated_actor(request)


async def require_member(request: Request) -> ActorContext:
    return await require_authenticated_actor(request)


async def require_manager(request: Request) -> ActorContext:
    return await _require_role(request, "manager")


async def require_admin(request: Request) -> ActorContext:
    """FastAPI dependency enforcing an authenticated admin actor."""
    return await _require_role(request, "admin")


# ── First-run detection ───────────────────────────────────────────────────────
_SETUP_COMPLETE_FLAG = CONFIG_DIR / ".setup_complete"
_ENV_PATH = LEISURELLM_DIR / ".env"


def is_first_run() -> bool:
    """Return True if the setup wizard hasn't been completed yet."""
    if _SETUP_COMPLETE_FLAG.exists():
        return False
    if not _ENV_PATH.exists():
        return True
    org_path = CONFIG_DIR / "org_profile.yaml"
    if not org_path.exists():
        return True
    return False


# Make first-run flag available in templates for auto-start behaviors.
templates.env.globals["is_first_run"] = is_first_run


# ── Sidebar context (Launch Mode gating) ──────────────────────────────────────

def get_sidebar_context() -> dict:
    """Return sidebar config for base.html.

    When a rail_map is active, returns structured sidebar sections from
    rail_maps.yaml so the template can collapse advanced pages.  Falls back
    to ``None`` (show everything) if no config exists.
    """
    try:
        import yaml

        rail_maps_path = CONFIG_DIR / "rail_maps.yaml"
        org_profile_path = CONFIG_DIR / "org_profile.yaml"

        active_map: Optional[str] = None
        if org_profile_path.exists():
            with open(org_profile_path, encoding="utf-8") as f:
                org = yaml.safe_load(f) or {}
            active_map = org.get("rail_map")

        sidebar: Optional[dict] = None
        if rail_maps_path.exists():
            with open(rail_maps_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            sidebar = data.get("sidebar")

        return {
            "active_rail_map": active_map,
            "sidebar_config": sidebar,
            "launch_mode": active_map is not None and sidebar is not None,
        }
    except Exception:
        logger.debug("Failed to load sidebar context", exc_info=True)
        return {"active_rail_map": None, "sidebar_config": None, "launch_mode": False}


# Register as Jinja2 template global so base.html can always access it.
templates.env.globals["get_sidebar"] = get_sidebar_context


# ── Progressive disclosure (sidebar visibility) ──────────────────────────────

def get_sidebar_visibility() -> dict:
    """Return per-page visibility flags for progressive disclosure.

    New users see only core pages (Dashboard, Conversations, Action Items,
    Teach, Knowledge, Settings).  Advanced pages unlock as the workspace
    matures:

    • Gaps       — visible after foundational gaps are seeded
    • Progress   — visible after any artifacts exist
    • Automations, Model Router, Organization — visible after setup complete
    • Leads, Obligations, Meetings — visible when workflows enable them

    Falls back to all-visible to avoid hiding things from existing users.
    """
    vis = {
        # Core (always visible)
        "dashboard": True,
        "inbox": True,
        "tasks": True,
        "teach": True,
        "knowledge": True,
        "settings": True,
        "guide": True,
        # Progressive (may be hidden for brand-new users)
        "analytics": True,
        "gaps": True,
        "jobs": True,
        "router": True,
        "org": True,
        "leads": True,
        "obligations": True,
        "meetings": True,
        "feedback": True,
    }

    try:
        import yaml

        org_path = CONFIG_DIR / "org_profile.yaml"
        setup_flag = CONFIG_DIR / ".setup_complete"
        gaps_flag = Path(__file__).resolve().parent.parent / ".foundational_gaps_seeded"

        has_org = org_path.exists()
        has_setup = setup_flag.exists()
        _ = gaps_flag.exists()

        if not has_setup:
            # Brand-new install: hide advanced pages (keep gaps visible)
            vis["analytics"] = False
            vis["jobs"] = False
            # Router is always visible — users need it early
            vis["org"] = False
            vis["leads"] = False
            vis["obligations"] = False
            vis["meetings"] = False
            vis["feedback"] = False
        else:
            # Setup done — selectively show based on what's configured
            pass

            # Check workflow toggles for optional sections
            wf_path = CONFIG_DIR / "workflows.yaml"
            if wf_path.exists():
                with open(wf_path, encoding="utf-8") as f:
                    wf = yaml.safe_load(f) or {}
                vis["leads"] = bool(wf.get("pipeline", {}).get("enabled", False))
                vis["meetings"] = bool(wf.get("persona_meetings", {}).get("enabled", False))
            else:
                vis["leads"] = False
                vis["meetings"] = False

        # Allow explicit overrides from org_profile.yaml
        if has_org:
            with open(org_path, encoding="utf-8") as f:
                org = yaml.safe_load(f) or {}
            overrides = org.get("sidebar_visibility", {})
            vis.update(overrides)

    except Exception:
        logger.debug("Failed to compute sidebar visibility", exc_info=True)
        # Fail-open: show everything

    return vis


templates.env.globals["get_sidebar_visibility"] = get_sidebar_visibility


def get_sidebar_lock_reasons() -> dict:
    """Return actionable lock reasons for sidebar items.

    Each reason is a short sentence explaining *exactly* what the user needs
    to do and links to the page that resolves it.
    """
    reasons: dict[str, str] = {}

    setup_flag = CONFIG_DIR / ".setup_complete"
    gaps_flag = Path(__file__).resolve().parent.parent / ".foundational_gaps_seeded"
    setup_done = setup_flag.exists()
    gaps_done = gaps_flag.exists()

    if not setup_done:
        # Analytics — needs at least some artifacts
        reasons["analytics"] = (
            '<a href="/setup" class="underline text-teal-light">Complete setup</a> '
            "to start tracking progress"
        )
        # Jobs, org, feedback — setup required (router is always unlocked)
        for key, label in [
            ("jobs", "Automations"),
            ("org", "Organization"),
            ("feedback", "Feedback"),
        ]:
            reasons[key] = (
                f'<a href="/setup" class="underline text-teal-light">'
                f"Finish setup</a> to unlock {label}"
            )
    else:
        # After setup, give more specific hints
        reasons["analytics"] = ""  # visible already
        reasons["jobs"] = ""
        reasons["org"] = ""
        reasons["feedback"] = ""

    if not gaps_done:
        reasons["gaps"] = (
            '<a href="/teach" class="underline text-teal-light">'
            "Teach something first</a> — gaps appear after the first interview seed"
        )
    else:
        reasons["gaps"] = ""

    # Workflow-gated pages
    try:
        import yaml
        wf_path = CONFIG_DIR / "workflows.yaml"
        if wf_path.exists():
            with open(wf_path, encoding="utf-8") as f:
                wf = yaml.safe_load(f) or {}
            if not wf.get("pipeline", {}).get("enabled", False):
                reasons["leads"] = (
                    '<a href="/settings" class="underline text-teal-light">'
                    "Enable the pipeline module</a> in Settings → Workflows"
                )
            if not wf.get("persona_meetings", {}).get("enabled", False):
                reasons["meetings"] = (
                    '<a href="/settings" class="underline text-teal-light">'
                    "Enable persona meetings</a> in Settings → Workflows"
                )
        else:
            reasons.setdefault("leads", (
                '<a href="/setup" class="underline text-teal-light">'
                "Complete setup</a> to configure pipeline"
            ))
            reasons.setdefault("meetings", (
                '<a href="/setup" class="underline text-teal-light">'
                "Complete setup</a> to configure meetings"
            ))
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    # Remove empty entries — those pages are unlocked
    return {k: v for k, v in reasons.items() if v}


templates.env.globals["get_sidebar_lock_reasons"] = get_sidebar_lock_reasons
