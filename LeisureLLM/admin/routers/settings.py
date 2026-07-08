"""Settings router — Secrets, Bot Config, Prompts, Setup Wizard, Org Profile."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from admin.dependencies import (
    _ENV_PATH,
    _SETUP_COMPLETE_FLAG,
    CONFIG_DIR,
    LEISURELLM_DIR,
    get_db,
    get_db_optional,
    get_model_router,
    is_first_run,
    require_admin,
    templates,
)

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["settings"], dependencies=[Depends(require_admin)])


def _settings_fail_soft(error: str, message: str, **extra):
    payload = {"success": False, "error": error, "message": message}
    payload.update(extra)
    return payload


def _sanitize_env_value(val: str) -> str:
    """Strip newlines/control chars to prevent .env injection."""
    return "".join(ch for ch in val.strip() if ch >= " " and ch not in ("\r", "\n"))


def _upsert_env_var(env_key: str, value: str) -> None:
    _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updated = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f"{env_key}="):
            lines[i] = f"{env_key}={_sanitize_env_value(value)}"
            updated = True
            break
    if not updated:
        lines.append(f"{env_key}={_sanitize_env_value(value)}")

    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove_env_var(env_key: str) -> None:
    if not _ENV_PATH.exists():
        return
    lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    filtered = [ln for ln in lines if not ln.lstrip().startswith(f"{env_key}=")]
    if filtered != lines:
        _ENV_PATH.write_text("\n".join(filtered) + "\n", encoding="utf-8")


def _count_docs_in_workspace() -> int:
    try:
        from admin.routers.knowledge import get_cached_knowledge_stats

        stats = get_cached_knowledge_stats()
        return int(((stats.get("documents") or {}).get("count")) or 0)
    except Exception:
        return 0


async def _count_rows(db, table: str, where: Optional[str] = None) -> int:
    if db is None:
        return 0
    query = f"SELECT COUNT(*) FROM {table}"  # noqa: S608
    if where:
        query += f" WHERE {where}"  # noqa: S608
    try:
        async with db.connection.execute(query) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


async def _seed_workspace_once(db) -> Dict[str, Any]:
    if db is None:
        return {"skipped": True, "reason": "no_db"}

    from core.seed_workspace import has_seed_data, is_seeded, seed_workspace

    if is_seeded() or await has_seed_data(db):
        return {"skipped": True, "reason": "already_seeded"}
    return await seed_workspace(db)


async def _build_onboarding_state(db=None, *, prefer_cached_provider_state: bool = False) -> Dict[str, Any]:
    from core.seed_workspace import is_seeded
    from services.model_router import PipelineRole
    from services.secrets import get_secrets_manager

    from admin.performance import get_cached_ollama_status, peek_cache

    mr = get_model_router()
    secrets = get_secrets_manager()
    if prefer_cached_provider_state:
        ollama_status = peek_cache("ollama_status") or {"installed": False, "running": False, "models": []}
    else:
        ollama_status = get_cached_ollama_status()

    cloud_keys = {
        "openai": bool(secrets.get("openai") or os.environ.get("OPENAI_API_KEY")),
        "anthropic": bool(secrets.get("anthropic") or os.environ.get("ANTHROPIC_API_KEY")),
        "openrouter": bool(secrets.get("openrouter") or os.environ.get("OPENROUTER_API_KEY")),
    }

    phase1_cfg = None
    if mr and getattr(mr, "pipeline", None) and getattr(mr.pipeline, "roles", None):
        phase1_cfg = mr.pipeline.roles.get(PipelineRole.INITIAL)

    discovered_models = set(ollama_status.get("models", []) or [])
    registered_backends: List[str] = []
    backend_models: Dict[str, set[str]] = {}
    if mr:
        for backend_name, backend_cfg in getattr(mr, "backends", {}).items():
            registered_backends.append(backend_name)
            models = set(getattr(backend_cfg, "available_models", []) or [])
            backend_models[backend_name] = models
            for model_name in models:
                discovered_models.add(model_name)

    counts = {
        "docs": _count_docs_in_workspace(),
        "inbox_threads": await _count_rows(db, "inbox_threads"),
        "tasks": await _count_rows(db, "tasks"),
        "decisions": await _count_rows(db, "decisions"),
        "gaps_total": await _count_rows(db, "knowledge_gaps"),
        "gaps_resolved": await _count_rows(db, "knowledge_gaps", "status = 'resolved'"),
        "feedback": await _count_rows(db, "response_feedback"),
        "leads": await _count_rows(db, "leads"),
        "meeting_notes": await _count_rows(db, "meeting_notes"),
    }

    org_profile_exists = (CONFIG_DIR / "org_profile.yaml").exists()
    workflows_exist = (CONFIG_DIR / "workflows.yaml").exists()
    provider_detected = bool(ollama_status.get("installed")) or any(cloud_keys.values())
    provider_connected = bool(ollama_status.get("running")) or bool(registered_backends)
    phase1_backend = getattr(phase1_cfg, "backend_name", None) if phase1_cfg else None
    phase1_model = getattr(phase1_cfg, "model", None) if phase1_cfg else None
    phase1_enabled = bool(getattr(phase1_cfg, "enabled", True)) if phase1_cfg else False
    phase1_backend_available = bool(phase1_backend and phase1_backend in registered_backends)
    phase1_model_available = False
    if phase1_backend_available and phase1_backend and phase1_model:
        if phase1_backend in {"ollama", "llamacpp"}:
            phase1_model_available = phase1_model in backend_models.get(phase1_backend, set())
        else:
            phase1_model_available = True
    phase1_saved = bool(
        phase1_enabled
        and phase1_backend
        and phase1_model
        and phase1_backend_available
        and phase1_model_available
    )

    return {
        "app_installed": True,
        "auth_initialized": _ENV_PATH.exists(),
        "org_profile_configured": org_profile_exists,
        "workflow_mode_configured": workflows_exist,
        "ollama_detected": bool(ollama_status.get("installed")),
        "provider_detected": provider_detected,
        "provider_connected": provider_connected,
        "model_discovered": bool(discovered_models),
        "phase1_model_selected": bool(phase1_model),
        "phase1_saved": phase1_saved,
        "phase1_backend": phase1_backend,
        "phase1_backend_available": phase1_backend_available,
        "phase1_model": phase1_model,
        "phase1_model_available": phase1_model_available,
        "starter_content_seeded": is_seeded(),
        "knowledge_docs_added": counts["docs"] >= 1,
        "first_question_asked": counts["inbox_threads"] >= 1,
        "first_action_captured": counts["tasks"] >= 1,
        "first_decision_captured": counts["decisions"] >= 1,
        "setup_complete": _SETUP_COMPLETE_FLAG.exists(),
        "registered_backends": registered_backends,
        "cloud_keys": cloud_keys,
        "local_models": sorted(discovered_models),
        "counts": counts,
    }


def _assistant_path_ready(onboarding_state: Dict[str, Any]) -> bool:
    return bool(onboarding_state.get("phase1_saved"))


def _launch_guide_needed(onboarding_state: Dict[str, Any]) -> bool:
    return not _assistant_path_ready(onboarding_state)


def _build_onboarding_experience(onboarding_state: Dict[str, Any]) -> Dict[str, Any]:
    local_models = list(onboarding_state.get("local_models") or [])
    cloud_keys = dict(onboarding_state.get("cloud_keys") or {})
    cloud_configured = any(bool(value) for value in cloud_keys.values())
    phase1_backend = str(onboarding_state.get("phase1_backend") or "").strip().lower()
    registered_backends = {str(name).strip().lower() for name in (onboarding_state.get("registered_backends") or [])}
    local_runtime_detected = bool(onboarding_state.get("ollama_detected")) or phase1_backend == "ollama" or "ollama" in registered_backends
    local_runtime_connected = bool(onboarding_state.get("provider_connected")) and local_runtime_detected
    local_path_ready = local_runtime_connected and bool(local_models)
    local_path_available = local_runtime_detected or local_path_ready
    default_path = "local_only" if local_path_available else "cloud_assisted"

    if local_path_ready:
        local_status = "ready"
        local_status_label = "Ready on this device"
        local_detail = "A local runtime and at least one model are already available, so the simplest private path is ready now."
    elif local_path_available:
        local_status = "setup_needed"
        local_status_label = "Local runtime found"
        local_detail = "A local runtime is present, but it still needs a model or a running service before local-only answers are available."
    else:
        local_status = "unavailable"
        local_status_label = "Not ready yet"
        local_detail = "No local runtime was detected yet. You can still finish setup, open the sample workspace, and add cloud access later only if you want it."

    return {
        "headline": "What do you want help with first?",
        "subheadline": (
            "Pick the first kind of help you want. Once that is clear, setup will explain the simplest way to run"
            " the assistant on this device or with a cloud provider."
        ),
        "default_path": default_path,
        "intents": [
            {
                "key": "follow_through",
                "label": "Keep track of actions and follow-through",
                "description": "Capture open work, assign owners, and notice what is overdue before it slips.",
            },
            {
                "key": "decisions",
                "label": "Preserve decisions and rationale",
                "description": "Keep a visible record of what was decided, why it happened, and what needs review later.",
            },
            {
                "key": "shared_memory",
                "label": "Keep shared notes and knowledge in one place",
                "description": "Keep notes, documents, and conversations together so the assistant can use them later.",
            },
            {
                "key": "demo_workspace",
                "label": "Try a sample workspace first",
                "description": "Open a small working example with overdue work, unresolved decisions, and a weekly review rhythm.",
            },
        ],
        "modes": [
            {
                "key": "local_only",
                "label": "Local-only",
                "short_label": "Private on this device",
                "recommended": default_path == "local_only",
                "status": local_status,
                "status_label": local_status_label,
                "description": "Best default if you want the assistant to stay on this device.",
                "privacy": "Questions and saved knowledge stay on this device unless you later turn on a cloud service.",
                "detail": local_detail,
            },
            {
                "key": "cloud_assisted",
                "label": "Cloud-assisted",
                "short_label": "External model provider",
                "recommended": default_path == "cloud_assisted",
                "status": "ready" if cloud_configured else "optional",
                "status_label": "Available if you add a key" if not cloud_configured else "Key already configured",
                "description": "Useful if this machine cannot run local AI yet or you want a faster setup path.",
                "privacy": "Questions and selected context may be sent to the provider you configure. Use this only if that is acceptable for your work.",
                "detail": "Cloud access is optional. You can skip it now and add it later from Settings.",
            },
        ],
        "work_styles": [
            {
                "key": "solo",
                "label": "Just me on this machine",
                "description": "Best for one operator or one shared operations laptop.",
            },
            {
                "key": "team",
                "label": "Small team with later collaboration",
                "description": "Keeps the web console primary, but leaves room for Discord and shared workflows later.",
            },
        ],
        "privacy_notes": [
            "Local-only keeps your questions and retrieved notes on this device by default.",
            "Cloud-assisted sends questions and selected context to the provider you configure.",
            "Web search is separate from the assistant itself. You can leave it off and still use the app locally.",
        ],
        "demo_workspace": {
            "title": "Sample workspace",
            "summary": "Loads a small starter workspace so you can see tasks, decisions, and reviews right away.",
            "records": [
                "One overdue opening check that clearly needs an owner",
                "One upcoming public notice that still needs an edit and sign-off",
                "One written team rule with the reason behind it",
                "One recurring Friday review so the cadence is visible",
            ],
        },
        "fallback_behavior": {
            "title": "If local AI is not ready yet",
            "summary": (
                "You can still finish setup, open the sample workspace if you want it, and let the app keep trying the"
                " local path first. Add cloud access later only if you decide to."
            ),
            "steps": [
                "Finish setup without adding any cloud key.",
                "If local AI is not ready, the app will keep trying the local path after setup.",
                "If local AI still is not available, you can add a cloud key later from Settings.",
            ],
        },
    }


def _build_model_setup_guidance(onboarding_state: Dict[str, Any]) -> Dict[str, Any]:
    phase1_saved = bool(onboarding_state.get("phase1_saved"))
    provider_detected = bool(onboarding_state.get("provider_detected"))
    provider_connected = bool(onboarding_state.get("provider_connected"))

    if phase1_saved:
        return {
            "show_banner": False,
            "state_key": "phase1_saved",
            "tone": "teal",
            "border_class": "border-teal",
            "title": "Your default assistant is ready.",
            "detail": "The default AI model is configured and ready to use.",
            "router_hint": "Your default assistant is ready. Open Model Router any time to refine the pipeline.",
            "router_tile_title": "Open Model Router",
            "primary_href": "/router",
            "primary_label": "Open Model Router",
            "secondary_href": "/setup?force=true",
            "secondary_label": "Reopen Launch Guide",
        }

    if not provider_detected:
        return {
            "show_banner": True,
            "state_key": "provider_not_detected",
            "tone": "coral",
            "border_class": "border-coral",
            "title": "Local AI is not ready yet.",
            "detail": "Finish setup anyway to open the sample workspace. The app will try the simplest local path first and only needs cloud access if you choose it later.",
            "router_hint": "No local runtime is ready yet. Finish setup first, then use Model Router only if you want to refine or override the default path.",
            "router_tile_title": "Set Up AI",
            "primary_href": "/router",
            "primary_label": "Open Model Router",
            "secondary_href": "/settings",
            "secondary_label": "Open Settings",
        }

    if not provider_connected:
        return {
            "show_banner": True,
            "state_key": "provider_not_connected",
            "tone": "gold",
            "border_class": "border-gold",
            "title": "A local runtime was found, but it is not active yet.",
            "detail": "Start the local runtime or let setup finish and come back later. The system will keep the local-first path as the default when it becomes available.",
            "router_hint": "A local runtime exists but is not active yet. Start it or refresh it later if you want to change the default path.",
            "router_tile_title": "Start Local AI",
            "primary_href": "/router",
            "primary_label": "Open Model Router",
            "secondary_href": None,
            "secondary_label": None,
        }

    return {
        "show_banner": True,
        "state_key": "provider_ready_phase1_not_saved",
        "tone": "teal",
        "border_class": "border-teal",
        "title": "One more step \u2014 choose the AI model that answers questions.",
        "detail": "A working AI provider is ready. Open Model Router and choose the default model for the assistant.",
        "router_hint": "Choose the default AI model for the assistant.",
        "router_tile_title": "Choose Default AI",
        "primary_href": "/router",
        "primary_label": "Open Model Router",
        "secondary_href": None,
        "secondary_label": None,
    }


# ── Pydantic models ──────────────────────────────────────────────────────────

class SecretUpdate(BaseModel):
    key: str
    value: str


class ConfigSectionUpdate(BaseModel):
    values: Dict[str, Any]


class ConfigImport(BaseModel):
    config: Dict[str, Any]


class PromptUpdate(BaseModel):
    content: str


class SetupKeysPayload(BaseModel):
    discord_token: Optional[str] = None
    openai_key: Optional[str] = None
    anthropic_key: Optional[str] = None
    tavily_key: Optional[str] = None
    operation_mode: Optional[str] = None  # 'solo' or 'team'


class SetupRailMapPayload(BaseModel):
    rail_map: str  # "launch" or "stabilize"


class SetupCompletePayload(BaseModel):
    seed_demo_workspace: bool = False
    goal: Optional[str] = None
    privacy_mode: Optional[str] = None
    operation_mode: Optional[str] = None


# ── Prompt file registry ─────────────────────────────────────────────────────

PROMPTS_DIR = LEISURELLM_DIR / "prompts"

PROMPT_FILES = {
    "system_prompt": {
        "path": PROMPTS_DIR / "system_prompt.txt",
        "name": "Main System Prompt",
        "description": "The primary personality and behavior instructions for the assistant",
    },
    "operational_context": {
        "path": PROMPTS_DIR / "operational_context.txt",
        "name": "Operational Context",
        "description": "Business facts: rates, team, portfolio, contracts (deployment-specific)",
    },
    "persona_scout": {
        "path": PROMPTS_DIR / "personas" / "scout.txt",
        "name": "Scout Persona Prompt",
        "description": "Voice and priorities for the Scout persona",
    },
    "persona_dreamer": {
        "path": PROMPTS_DIR / "personas" / "dreamer.txt",
        "name": "Dreamer Persona Prompt",
        "description": "Voice and priorities for the Dreamer persona",
    },
    "persona_rainmaker": {
        "path": PROMPTS_DIR / "personas" / "rainmaker.txt",
        "name": "Rainmaker Persona Prompt",
        "description": "Voice and priorities for the Rainmaker persona",
    },
    "persona_steward": {
        "path": PROMPTS_DIR / "personas" / "steward.txt",
        "name": "Steward Persona Prompt",
        "description": "Voice and priorities for the Steward persona",
    },
}


# =============================================================================
# Page routes
# =============================================================================

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from services.secrets import get_secrets_manager
    secrets = get_secrets_manager()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_page": "settings",
            "secrets_by_category": secrets.list_keys_by_category(),
            "storage_info": secrets.get_storage_info(),
        },
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, force: bool = False):
    onboarding_state = await _build_onboarding_state(prefer_cached_provider_state=True)
    if not force and bool(onboarding_state.get("setup_complete")) and _assistant_path_ready(onboarding_state):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "onboarding_state": onboarding_state,
            "onboarding_experience": _build_onboarding_experience(onboarding_state),
            "model_setup_guidance": _build_model_setup_guidance(onboarding_state),
        },
    )


@router.get("/org", response_class=HTMLResponse)
async def org_page(request: Request):
    return templates.TemplateResponse(request, "org.html", {"active_page": "org"})


# =============================================================================
# Secrets Management
# =============================================================================

@router.get("/api/v1/secrets/list")
async def api_list_secrets():
    from services.secrets import get_secrets_manager
    secrets = get_secrets_manager()
    return {
        "secrets": secrets.list_keys(),
        "by_category": secrets.list_keys_by_category(),
        "storage": secrets.get_storage_info(),
    }


@router.get("/api/v1/settings/secrets")
async def api_settings_secrets_status():
    from services.secrets import get_secrets_manager

    secrets = get_secrets_manager()
    items = secrets.list_keys()
    by_env = {item["env_var"]: item["has_value"] for item in items}
    by_key = {item["key"]: item["has_value"] for item in items}
    return {
        "success": True,
        "secrets": by_env,
        "keys": by_key,
    }


@router.post("/api/v1/secrets/set")
async def api_set_secret(data: SecretUpdate):
    from services.secrets import KNOWN_KEYS, get_secrets_manager
    secrets = get_secrets_manager()
    if data.key not in KNOWN_KEYS:
        return {"success": False, "error": f"Unknown key: {data.key}"}
    success = secrets.set(data.key, data.value)
    if success and data.key in ("openai", "anthropic", "openrouter"):
        try:
            from admin.server import _register_cloud_backends_from_secrets
            await _register_cloud_backends_from_secrets()
        except Exception as e:
            logger.warning(f"Backend refresh after secret set failed: {e}")
    if not success:
        return {"success": False, "error": "Could not persist key — OS keyring may be unavailable."}
    try:
        env_key = KNOWN_KEYS[data.key]["env_var"]
        os.environ[env_key] = data.value
        _upsert_env_var(env_key, data.value)
    except Exception as e:
        logger.warning("Failed to persist %s to .env: %s", data.key, e)
    return {"success": True, "key": data.key}


@router.post("/api/v1/secrets/delete/{key}")
async def api_delete_secret(key: str):
    from services.secrets import KNOWN_KEYS, get_secrets_manager
    secrets = get_secrets_manager()
    env_key = KNOWN_KEYS.get(key, {}).get("env_var")
    if env_key:
        os.environ.pop(env_key, None)
        try:
            _remove_env_var(env_key)
        except Exception as e:
            logger.warning("Failed to remove %s from .env: %s", key, e)
    return {"success": secrets.delete(key), "key": key}


@router.get("/api/v1/secrets/test/{key}")
async def api_test_secret(key: str):
    from services.secrets import get_secrets_manager
    secrets = get_secrets_manager()
    value = secrets.get(key)
    if not value:
        return {"valid": False, "error": "No value stored"}

    if key == "openai":
        try:
            import openai
            client = openai.OpenAI(api_key=value)
            models = client.models.list()
            return {"valid": True, "message": f"Connected! {len(list(models))} models available"}
        except Exception:
            return {"valid": False, "error": "validation_failed", "message": "Validation check failed."}

    if key == "openrouter":
        try:
            import openai
            client = openai.OpenAI(api_key=value, base_url="https://openrouter.ai/api/v1")
            models = client.models.list()
            return {"valid": True, "message": f"Connected! {len(list(models))} models available"}
        except Exception:
            return {"valid": False, "error": "validation_failed", "message": "Validation check failed."}

    if key == "anthropic":
        try:
            import aiohttp
            headers = {
                "Content-Type": "application/json",
                "x-api-key": value,
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": "claude-3-5-haiku-latest",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post("https://api.anthropic.com/v1/messages", json=payload) as resp:
                    if resp.status == 200:
                        return {"valid": True, "message": "Connected! Anthropic key accepted"}
                    error_text = await resp.text()
                    return {"valid": False, "error": f"HTTP {resp.status}: {error_text}"}
        except Exception:
            return {"valid": False, "error": "validation_failed", "message": "Validation check failed."}

    return {"valid": None, "message": "Test not implemented for this key"}


# =============================================================================
# Bot Configuration
# =============================================================================

@router.get("/api/v1/config/all")
async def api_get_all_config():
    from services.bot_config import CONFIG_SECTIONS, get_bot_config_manager
    config_mgr = get_bot_config_manager()
    return {"success": True, "config": config_mgr.get_all(), "sections": CONFIG_SECTIONS}


@router.get("/api/v1/config/sections")
async def api_get_config_sections():
    from services.bot_config import CONFIG_SECTIONS
    return {"success": True, "sections": CONFIG_SECTIONS}


@router.get("/api/v1/config/{section}")
async def api_get_config_section(section: str):
    from services.bot_config import CONFIG_SECTIONS, get_bot_config_manager
    config_mgr = get_bot_config_manager()
    data = config_mgr.get_section(section)
    if data is None:
        return {"success": False, "error": f"Unknown section: {section}"}
    return {"success": True, "section": section, "config": data, "metadata": CONFIG_SECTIONS.get(section, {})}


@router.post("/api/v1/config/{section}")
async def api_update_config_section(section: str, update: ConfigSectionUpdate):
    from services.bot_config import CONFIG_SECTIONS, get_bot_config_manager
    if section not in CONFIG_SECTIONS:
        return {"success": False, "error": f"Unknown section: {section}"}
    config_mgr = get_bot_config_manager()
    success = config_mgr.update_section(section, update.values)
    if success:
        logger.info(f"Config section '{section}' updated: {list(update.values.keys())}")
    return {"success": success, **({"section": section} if success else {"error": "Failed to save"})}


@router.post("/api/v1/config/{section}/reset")
async def api_reset_config_section(section: str):
    from services.bot_config import CONFIG_SECTIONS, get_bot_config_manager
    if section not in CONFIG_SECTIONS:
        return {"success": False, "error": f"Unknown section: {section}"}
    config_mgr = get_bot_config_manager()
    success = config_mgr.reset_section(section)
    return {"success": success, **({"section": section} if success else {"error": "Failed to reset"})}


@router.post("/api/v1/config/reset-all")
async def api_reset_all_config():
    from services.bot_config import get_bot_config_manager
    config_mgr = get_bot_config_manager()
    return {"success": config_mgr.reset_all()}


@router.get("/api/v1/config/export")
async def api_export_config():
    from services.bot_config import get_bot_config_manager
    config_mgr = get_bot_config_manager()
    return JSONResponse(
        content=config_mgr.get_all(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=bot_settings.json"},
    )


@router.post("/api/v1/config/import")
async def api_import_config(data: ConfigImport):
    from services.bot_config import CONFIG_SECTIONS, get_bot_config_manager
    try:
        config_mgr = get_bot_config_manager()
        for section in CONFIG_SECTIONS.keys():
            if section in data.config:
                config_mgr.update_section(section, data.config[section])
        return {"success": True, "message": "Configuration imported"}
    except Exception as e:
        logger.error(f"Config import failed: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# System Prompts
# =============================================================================

@router.get("/api/v1/prompts")
async def api_list_prompts():
    prompts = {}
    for key, info in PROMPT_FILES.items():
        path = info["path"]
        prompts[key] = {
            "name": info["name"],
            "description": info["description"],
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "modified": path.stat().st_mtime if path.exists() else None,
        }
    return {"success": True, "prompts": prompts}


@router.get("/api/v1/prompts/{prompt_key}")
async def api_get_prompt(prompt_key: str):
    if prompt_key not in PROMPT_FILES:
        return {"success": False, "error": f"Unknown prompt: {prompt_key}"}
    path = PROMPT_FILES[prompt_key]["path"]
    if not path.exists():
        return {"success": True, "prompt_key": prompt_key, "content": "", "exists": False, **PROMPT_FILES[prompt_key]}
    try:
        content = path.read_text(encoding="utf-8")
        return {"success": True, "prompt_key": prompt_key, "content": content, "exists": True, **PROMPT_FILES[prompt_key]}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/prompts/{prompt_key}")
async def api_update_prompt(prompt_key: str, update: PromptUpdate):
    if prompt_key not in PROMPT_FILES:
        return {"success": False, "error": f"Unknown prompt: {prompt_key}"}
    path = PROMPT_FILES[prompt_key]["path"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(update.content, encoding="utf-8")
        logger.info(f"Updated prompt '{prompt_key}' ({len(update.content)} chars)")
        return {"success": True, "prompt_key": prompt_key, "size": len(update.content),
                "message": "Prompt updated. Changes take effect on next restart or /reload."}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/prompts/{prompt_key}/backup")
async def api_backup_prompt(prompt_key: str):
    if prompt_key not in PROMPT_FILES:
        return {"success": False, "error": f"Unknown prompt: {prompt_key}"}
    path = PROMPT_FILES[prompt_key]["path"]
    if not path.exists():
        return {"success": False, "error": "Prompt file does not exist"}
    try:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_suffix(f".{timestamp}.bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return {"success": True, "backup_file": backup_path.name}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Organization Config
# =============================================================================

@router.get("/api/v1/org/profile")
async def api_get_org_profile():
    try:
        import yaml
        profile_path = CONFIG_DIR / "org_profile.yaml"
        if not profile_path.exists():
            return {"success": True, "profile": {}, "exists": False}
        with open(profile_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {"success": True, "profile": data, "exists": True}
    except ImportError:
        return {"success": False, "error": "PyYAML not installed"}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/org/profile")
async def api_update_org_profile(request: Request):
    try:
        import yaml
        body = await request.json()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "org_profile.yaml", "w", encoding="utf-8") as f:
            yaml.dump(body, f, default_flow_style=False, allow_unicode=True)
        mode = (body.get("mode") or "").strip().lower()
        if mode in {"solo", "small", "team"}:
            _upsert_env_var("OPERATION_MODE", "team" if mode in {"small", "team"} else "solo")
        return {"success": True}
    except ImportError:
        return {"success": False, "error": "PyYAML not installed"}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/org/workflows")
async def api_get_workflows():
    try:
        import yaml
        wf_path = CONFIG_DIR / "workflows.yaml"
        if not wf_path.exists():
            return {"success": True, "workflows": {}, "exists": False}
        with open(wf_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {"success": True, "workflows": data, "exists": True}
    except ImportError:
        return {"success": False, "error": "PyYAML not installed"}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/org/workflows")
async def api_update_workflows(request: Request):
    try:
        import yaml
        body = await request.json()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "workflows.yaml", "w", encoding="utf-8") as f:
            yaml.dump(body, f, default_flow_style=False, allow_unicode=True)
        return {"success": True}
    except ImportError:
        return {"success": False, "error": "PyYAML not installed"}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Setup Wizard
# =============================================================================

@router.get("/api/v1/setup/status")
async def api_setup_status():
    has_env = _ENV_PATH.exists()
    org_profile = {}
    try:
        import yaml
        org_path = CONFIG_DIR / "org_profile.yaml"
        if org_path.exists():
            with open(org_path, encoding="utf-8") as f:
                org_profile = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("api_setup_status: suppressed %s", e)
    onboarding_state = await _build_onboarding_state(prefer_cached_provider_state=True)
    model_setup_guidance = _build_model_setup_guidance(onboarding_state)
    onboarding_experience = _build_onboarding_experience(onboarding_state)
    return {
        "is_first_run": is_first_run(),
        "has_env": has_env,
        "setup_complete": _SETUP_COMPLETE_FLAG.exists(),
        "org_profile": org_profile,
        "operation_mode": org_profile.get("mode", "solo"),
        "onboarding_state": onboarding_state,
        "onboarding_experience": onboarding_experience,
        "model_setup_guidance": model_setup_guidance,
        "assistant_ready": _assistant_path_ready(onboarding_state),
        "launch_guide_needed": _launch_guide_needed(onboarding_state),
    }


@router.get("/api/v1/setup/preflight")
async def api_setup_preflight():
    """Quick environment health-check shown on the setup wizard welcome step."""
    try:
        import sys as _sys

        checks: List[Dict[str, Any]] = []

        v = _sys.version_info
        checks.append({
            "name": "Python",
            "ok": v >= (3, 10),
            "detail": f"{v.major}.{v.minor}.{v.micro}",
        })

        missing: List[str] = []
        for mod in ("fastapi", "discord", "langchain", "chromadb", "aiosqlite"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        checks.append({
            "name": "Dependencies",
            "ok": len(missing) == 0,
            "detail": "All installed" if not missing else f"Missing: {', '.join(missing)}",
        })

        db_path = LEISURELLM_DIR / "assistant.db"
        checks.append({
            "name": "Database",
            "ok": db_path.exists(),
            "detail": "Ready" if db_path.exists() else "Will be created during setup",
        })

        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            test_path = CONFIG_DIR / ".write_test"
            test_path.write_text("ok")
            test_path.unlink()
            writable = True
        except Exception:
            writable = False
        checks.append({
            "name": "Config directory",
            "ok": writable,
            "detail": "Writable" if writable else "Not writable — check permissions",
        })

        all_ok = all(c["ok"] for c in checks)
        return {"success": True, "checks": checks, "all_ok": all_ok}
    except Exception as exc:
        logger.error("Setup preflight failed: %s", exc, exc_info=True)
        return _settings_fail_soft(
            "setup_preflight_unavailable",
            "Could not load local setup checks right now.",
            checks=[],
            all_ok=False,
        )


@router.post("/api/v1/setup/keys")
async def api_setup_keys(data: SetupKeysPayload):
    try:
        op_mode = (data.operation_mode or "solo").lower()
        _upsert_env_var("OPERATION_MODE", op_mode)
        _upsert_env_var("DATABASE_PATH", "assistant.db")

        if data.discord_token:
            _upsert_env_var("DISCORD_TOKEN", data.discord_token)
        if data.openai_key:
            _upsert_env_var("OPENAI_API_KEY", data.openai_key)
        if data.tavily_key:
            _upsert_env_var("TAVILY_API_KEY", data.tavily_key)

        try:
            from services.secrets import get_secrets_manager
            secrets = get_secrets_manager()
            if data.discord_token:
                secrets.set("discord_token", data.discord_token)
            if data.openai_key:
                secrets.set("openai", data.openai_key)
            if data.anthropic_key:
                secrets.set("anthropic", data.anthropic_key)
            if data.tavily_key:
                secrets.set("tavily", data.tavily_key)
            from admin.server import _register_cloud_backends_from_secrets
            await _register_cloud_backends_from_secrets()
        except Exception as e:
            logger.warning("Secrets manager storage skipped: %s", e)

        return {"success": True}
    except Exception as e:
        logger.error("Setup keys failed: %s", e, exc_info=True)
        return _settings_fail_soft("setup_keys_failed", "Could not save local setup settings.")


@router.post("/api/v1/setup/complete")
async def api_setup_complete(payload: Optional[SetupCompletePayload] = None, db=Depends(get_db_optional)):
    try:
        onboarding_state_before = await _build_onboarding_state(
            db,
            prefer_cached_provider_state=True,
        )
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _SETUP_COMPLETE_FLAG.write_text("Setup completed via admin GUI.\n", encoding="utf-8")

        try:
            import yaml

            org_profile_path = CONFIG_DIR / "org_profile.yaml"
            org_profile = {}
            if org_profile_path.exists():
                with open(org_profile_path, encoding="utf-8") as handle:
                    org_profile = yaml.safe_load(handle) or {}
            if payload and payload.goal:
                org_profile["onboarding_goal"] = payload.goal
            if payload and payload.privacy_mode:
                org_profile["onboarding_privacy_mode"] = payload.privacy_mode
            if payload and payload.operation_mode:
                org_profile["mode"] = payload.operation_mode
            if payload is not None:
                org_profile["demo_workspace_enabled"] = bool(payload.seed_demo_workspace)
            if org_profile:
                with open(org_profile_path, "w", encoding="utf-8") as handle:
                    yaml.dump(org_profile, handle, default_flow_style=False, allow_unicode=True)
        except Exception as org_err:
            logger.warning("Persisting onboarding profile failed (non-fatal): %s", org_err)

        # Auto-configure pipeline based on hardware + installed models + API keys
        pipeline_summary = {}
        try:
            from services.pipeline_presets import auto_configure_pipeline
            pipeline_summary = await auto_configure_pipeline()
            logger.info("Auto-configured pipeline: %s", pipeline_summary.get("pipeline_name", "none"))
        except Exception as pipe_err:
            logger.warning("Pipeline auto-config failed (non-fatal): %s", pipe_err)

        # If the pipeline wasn't configured (no Ollama models, no cloud keys),
        # kick off llama.cpp auto-provision in the background so the user gets
        # a working local LLM without touching the Model Router page.
        if not pipeline_summary.get("configured"):
            import asyncio
            async def _background_provision():
                try:
                    from services.llamacpp_manager import auto_provision_llamacpp
                    result = await auto_provision_llamacpp()
                    logger.info("llama.cpp auto-provision: %s", result)
                except Exception as exc:
                    logger.warning("llama.cpp auto-provision failed (non-fatal): %s", exc)
            asyncio.create_task(_background_provision())

        # Seed sample data so the GUI isn't empty on first visit
        seed_result = {"skipped": True, "reason": "demo_workspace_disabled"}
        should_seed_demo_workspace = bool(payload is not None and payload.seed_demo_workspace)
        already_seeded = bool(onboarding_state_before.get("starter_content_seeded"))
        if db is not None and should_seed_demo_workspace and not already_seeded:
            try:
                seed_result = await _seed_workspace_once(db)
                logger.info("Seed workspace result: %s", seed_result)
            except Exception as seed_err:
                logger.warning("Seed workspace failed (non-fatal): %s", seed_err)
                seed_result = {"skipped": True, "reason": f"seed_failed:{seed_err}"}
        elif should_seed_demo_workspace and already_seeded:
            seed_result = {"skipped": True, "reason": "already_seeded"}

        return {"success": True, "pipeline": pipeline_summary, "seed": seed_result}
    except Exception as e:
        logger.error("Setup complete failed: %s", e, exc_info=True)
        return _settings_fail_soft("setup_complete_failed", "Could not finish setup right now.")


async def build_setup_completion(db=None, *, prefer_cached_provider_state: bool = False) -> Dict[str, Any]:
    onboarding_state = await _build_onboarding_state(
        db,
        prefer_cached_provider_state=prefer_cached_provider_state,
    )
    model_setup_guidance = _build_model_setup_guidance(onboarding_state)
    milestones: list[dict] = []
    total_weight = 0
    earned_weight = 0

    def _check(label: str, met: bool, weight: int, hint: str = ""):
        nonlocal total_weight, earned_weight
        total_weight += weight
        if met:
            earned_weight += weight
        milestones.append({
            "label": label,
            "met": met,
            "weight": weight,
            "hint": hint,
        })

    _check(
        "Credentials initialized",
        bool(onboarding_state.get("auth_initialized")),
        10,
        "Save your local settings from Launch Guide.",
    )
    _check(
        "Organization profile configured",
        bool(onboarding_state.get("org_profile_configured")),
        10,
        "Complete the setup wizard or onboarding chat.",
    )
    _check(
        "Workflow mode configured",
        bool(onboarding_state.get("workflow_mode_configured")),
        10,
        "Choose solo or team workflows during onboarding.",
    )
    _check(
        "An assistant path is available",
        bool(onboarding_state.get("provider_detected")),
        10,
        "Let setup prefer the local path first, or add cloud access later only if you need it.",
    )
    _check(
        "The assistant path can answer",
        bool(onboarding_state.get("provider_connected")),
        10,
        "Start the local runtime or finish setup and return later after background provisioning.",
    )
    _check(
        "Default assistant ready",
        bool(onboarding_state.get("phase1_saved")),
        10,
        "Open Model Router later only if you want to tune the default assistant path.",
    )
    _check(
        "Sample continuity workspace ready",
        bool(onboarding_state.get("starter_content_seeded")),
        10,
        "Enable the sample continuity workspace during setup if you want a guided demo.",
    )
    _check(
        "Knowledge docs added",
        bool(onboarding_state.get("knowledge_docs_added")),
        10,
        "Drop at least one file into LeisureLLM/docs/.",
    )
    _check(
        "First question asked",
        bool(onboarding_state.get("first_question_asked")),
        10,
        "Ask one real question in Chat or Inbox.",
    )
    _check(
        "First action captured",
        bool(onboarding_state.get("first_action_captured")),
        10,
        "Create one action item in Actions.",
    )
    _check(
        "First decision captured",
        bool(onboarding_state.get("first_decision_captured")),
        10,
        "Capture one decision via Teach or chat.",
    )

    pct = round(earned_weight / total_weight * 100) if total_weight else 0
    is_complete = bool(onboarding_state.get("phase1_saved"))

    return {
        "success": True,
        "completion_pct": pct,
        "is_complete": is_complete,
        "milestones": milestones,
        "counts": dict(onboarding_state.get("counts") or {}),
        "onboarding_state": onboarding_state,
        "model_setup_guidance": model_setup_guidance,
    }


@router.get("/api/v1/setup/completion")
async def api_setup_completion(db=Depends(get_db)):
    """Return first-run completion as a practical 10-step launch checklist.

    This metric is intentionally behavior-based so progress reflects real
    onboarding outcomes (configured, asked, captured, reviewed) instead of only
    setup form completion.
    """
    try:
        return await build_setup_completion(db)
    except Exception as e:
        logger.error("Setup completion check failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Conversational Onboarding ("Character Creation")
# =============================================================================


class OnboardingMessagePayload(BaseModel):
    message: str
    phase: str = "conversation"  # conversation | welcome | intro | projects | brain_dump (legacy)
    context: Optional[Dict[str, Any]] = None  # Accumulated state from prior turns
    apply: bool = True
    extracted: Optional[Dict[str, Any]] = None
    selected_artifacts: Optional[List[int]] = None
    apply_mask: Optional[Dict[str, bool]] = None
    session_id: Optional[str] = None
    expected_version: Optional[int] = None


@router.get("/api/v1/onboarding/welcome")
async def api_onboarding_welcome():
    """Get the initial welcome message to start conversational onboarding."""
    try:
        from core.conversational_onboarding import OnboardingConversation
        conv = OnboardingConversation()
        response = conv.get_welcome()
        return {"success": True, **response.to_dict()}
    except Exception as e:
        logger.error("Onboarding welcome failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/onboarding/session")
async def api_onboarding_session():
    """Return or create the current onboarding session (for optimistic locking)."""
    try:
        from core.conversational_onboarding import _init_session, _read_session

        session = _read_session()
        if not session.get("session_id"):
            session = _init_session("")
        return {"success": True, "session": session}
    except Exception as e:
        logger.error("Onboarding session lookup failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/onboarding/status")
async def api_onboarding_status():
    """Return the current onboarding state for resumption."""
    try:
        from core.onboarding_sprint import is_sprint_complete

        onboarding_state = await _build_onboarding_state()
        has_org_profile = bool(onboarding_state.get("org_profile_configured"))
        setup_complete = bool(onboarding_state.get("setup_complete"))
        assistant_ready = _assistant_path_ready(onboarding_state)
        sprint_complete = is_sprint_complete()

        # Determine recommended phase based on what exists
        if assistant_ready:
            phase = "complete"
        elif has_org_profile:
            phase = "conversation"  # Profile exists, resume conversation
        else:
            phase = "welcome"

        return {
            "success": True,
            "phase": phase,
            "has_org_profile": has_org_profile,
            "setup_complete": setup_complete,
            "assistant_ready": assistant_ready,
            "sprint_complete": sprint_complete,
            "onboarding_state": onboarding_state,
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/onboarding/skip")
async def api_onboarding_skip(db=Depends(get_db_optional)):
    """Skip conversational onboarding without claiming setup is finished."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Still seed workspace so the GUI isn't empty
        if db is not None:
            try:
                await _seed_workspace_once(db)
            except Exception as e:
                logger.warning("api_onboarding_skip: suppressed %s", e)
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/onboarding/reset")
async def api_onboarding_reset():
    """Reset onboarding state and clear setup artifacts."""
    try:
        files_to_remove = [
            CONFIG_DIR / "org_profile.yaml",
            CONFIG_DIR / "workflows.yaml",
            CONFIG_DIR / ".setup_complete",
            CONFIG_DIR / "onboarding_transcript.jsonl",
            CONFIG_DIR / "onboarding_diff.json",
            CONFIG_DIR / "onboarding_session.json",
            Path(__file__).resolve().parent.parent / ".foundational_gaps_seeded",
            Path(__file__).resolve().parent.parent / ".capture_sprint_complete",
        ]

        for f in files_to_remove:
            try:
                if f.exists():
                    f.unlink()
            except Exception as e:
                logger.warning("api_onboarding_reset: suppressed %s", e)

        # Remove onboarding docs
        try:
            import config as app_config
            docs_root = Path(app_config.directory_path)
            onboarding_dir = docs_root / "onboarding"
            if onboarding_dir.exists():
                for p in onboarding_dir.glob("*"):
                    try:
                        p.unlink()
                    except Exception as e:
                        logger.warning("operation: suppressed %s", e)
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/onboarding/delete-session-data")
async def api_onboarding_delete_session_data():
    """Delete transcript + diff logs without resetting config.

    Narrower than /reset — leaves org_profile.yaml and workflows.yaml intact
    but removes the conversation evidence.
    """
    try:
        files_to_remove = [
            CONFIG_DIR / "onboarding_transcript.jsonl",
            CONFIG_DIR / "onboarding_diff.json",
            CONFIG_DIR / "onboarding_session.json",
        ]
        removed = 0
        for f in files_to_remove:
            if f.exists():
                f.unlink()
                removed += 1
        return {"success": True, "files_removed": removed}
    except Exception as e:
        logger.error("Delete session data failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/onboarding/cleanup-transcripts")
async def api_onboarding_cleanup_transcripts(max_age_days: int = 30):
    """Prune transcript/diff entries older than *max_age_days*."""
    try:
        from core.conversational_onboarding import cleanup_old_transcripts
        result = cleanup_old_transcripts(max_age_days=max_age_days)
        return {"success": True, **result}
    except Exception as e:
        logger.error("Transcript cleanup failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/rail-maps")
async def api_get_rail_maps():
    """Return available rail maps from rail_maps.yaml."""
    try:
        import yaml
        maps_path = CONFIG_DIR / "rail_maps.yaml"
        if not maps_path.exists():
            return {"success": True, "maps": {}}
        with open(maps_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {"success": True, "maps": data.get("maps", {})}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.get("/api/v1/rail-maps/sidebar")
async def api_get_sidebar_config():
    """Return sidebar visibility config from rail_maps.yaml."""
    try:
        import yaml
        maps_path = CONFIG_DIR / "rail_maps.yaml"
        if not maps_path.exists():
            return {"success": True, "sidebar": {}, "active_rail_map": None}
        with open(maps_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Read active rail map from org_profile
        active_map = None
        org_path = CONFIG_DIR / "org_profile.yaml"
        if org_path.exists():
            with open(org_path, encoding="utf-8") as f:
                org = yaml.safe_load(f) or {}
            active_map = org.get("rail_map")
        return {
            "success": True,
            "sidebar": data.get("sidebar", {}),
            "active_rail_map": active_map,
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/setup/rail-map")
async def api_setup_rail_map(data: SetupRailMapPayload):
    """Save the chosen rail map to org_profile.yaml."""
    try:
        import yaml
        maps_path = CONFIG_DIR / "rail_maps.yaml"
        if maps_path.exists():
            with open(maps_path, encoding="utf-8") as f:
                maps_data = yaml.safe_load(f) or {}
            available = list((maps_data.get("maps") or {}).keys())
            if data.rail_map not in available:
                return {"success": False, "error": f"Unknown rail map: {data.rail_map!r}. Available: {available}"}

        # Read-modify-write org_profile.yaml
        org_path = CONFIG_DIR / "org_profile.yaml"
        org_data: Dict[str, Any] = {}
        if org_path.exists():
            with open(org_path, encoding="utf-8") as f:
                org_data = yaml.safe_load(f) or {}
        org_data["rail_map"] = data.rail_map
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(org_path, "w", encoding="utf-8") as f:
            yaml.dump(org_data, f, default_flow_style=False, allow_unicode=True)
        return {"success": True, "rail_map": data.rail_map}
    except Exception as e:
        logger.error("Setup rail-map failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Web Capture Sprint (post-onboarding knowledge building)
# =============================================================================


class SprintCapturePayload(BaseModel):
    step_number: int
    text: str


@router.get("/api/v1/sprint/steps")
async def api_sprint_steps():
    """Return the capture sprint step definitions for the UI."""
    try:
        from core.web_sprint import WebSprintProcessor
        processor = WebSprintProcessor()
        steps = processor.get_steps()
        return {
            "success": True,
            "steps": [s.to_dict() for s in steps],
            "complete": _is_sprint_complete(),
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/sprint/capture")
async def api_sprint_capture(
    payload: SprintCapturePayload,
    db=Depends(get_db),
):
    """Process one capture sprint step — classify text and create artifacts."""
    try:
        from core.web_sprint import WebSprintProcessor

        model_router = None
        try:
            from admin.dependencies import get_model_router
            model_router = get_model_router()
        except Exception as e:
            logger.warning("api_sprint_capture: suppressed %s", e)

        processor = WebSprintProcessor(db=db, model_router=model_router)
        result = await processor.capture_step(
            step_number=payload.step_number,
            user_text=payload.text,
        )
        return {"success": True, **result.to_dict()}
    except Exception as e:
        logger.error("Sprint capture failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/sprint/complete")
async def api_sprint_complete():
    """Mark the capture sprint as complete."""
    try:
        from core.onboarding_sprint import mark_sprint_complete
        mark_sprint_complete()
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/sprint/skip")
async def api_sprint_skip():
    """Skip the capture sprint."""
    try:
        from core.onboarding_sprint import mark_sprint_complete
        mark_sprint_complete()
        return {"success": True}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


def _is_sprint_complete() -> bool:
    try:
        from core.onboarding_sprint import is_sprint_complete
        return is_sprint_complete()
    except Exception:
        return False

