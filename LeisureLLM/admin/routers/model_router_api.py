"""Model Router API — LLM backend & pipeline configuration."""

import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from admin.dependencies import (
    CONFIG_DIR,
    ROUTER_CONFIG_PATH,
    get_model_router,
    require_admin,
    templates,
)
from admin.performance import peek_cache

logger = logging.getLogger("AdminServer")
router = APIRouter(tags=["model_router"], dependencies=[Depends(require_admin)])

_BACKEND_ALIASES = {
    "local": "ollama",
    "local_ollama": "ollama",
    "ollama-local": "ollama",
    "ollama_local": "ollama",
    "llama.cpp": "llamacpp",
    "llama_cpp": "llamacpp",
}


# ── Pydantic models ──────────────────────────────────────────────────────────

class RoleConfigUpdate(BaseModel):
    enabled: bool = True
    backend_name: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 4000
    system_prompt_override: Optional[str] = None
    ollama_options: Optional[Dict[str, Any]] = None


class PipelineConfigUpdate(BaseModel):
    initial: Optional[RoleConfigUpdate] = None
    critique: Optional[RoleConfigUpdate] = None
    synthesize: Optional[RoleConfigUpdate] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_pipeline_to_file():
    mr = get_model_router()
    if not mr or not mr.pipeline:
        return
    config = {
        "pipeline": {
            "name": mr.pipeline.name,
            "roles": {
                role.value: {
                    "backend_name": cfg.backend_name,
                    "model": cfg.model,
                    "temperature": cfg.temperature,
                    "max_tokens": cfg.max_tokens,
                    "enabled": cfg.enabled,
                    "system_prompt_override": cfg.system_prompt_override,
                    "ollama_options": cfg.ollama_options,
                }
                for role, cfg in mr.pipeline.roles.items()
            },
        }
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(ROUTER_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _normalize_backend_name(backend_name: str) -> str:
    key = (backend_name or "").strip().lower()
    return _BACKEND_ALIASES.get(key, key)


def _prune_unavailable_roles(mr, roles: Dict[Any, Any]) -> tuple[Dict[Any, Any], Dict[str, str]]:
    cleaned_roles: Dict[Any, Any] = {}
    dropped_roles: Dict[str, str] = {}
    for role, cfg in (roles or {}).items():
        backend_name = getattr(cfg, "backend_name", None)
        if backend_name not in mr.backends:
            dropped_roles[getattr(role, "value", str(role))] = str(backend_name or "")
            continue
        cleaned_roles[role] = cfg
    return cleaned_roles, dropped_roles


async def _resolve_backend_name(mr, backend_name: str, *, status_override: Optional[dict[str, Any]] = None) -> tuple[Optional[str], Dict[str, Any]]:
    from services.model_router import BackendConfig, BackendType
    from services.system_tools import SystemTools

    requested = backend_name or ""
    normalized = _normalize_backend_name(requested)
    resolution: Dict[str, Any] = {
        "requested": requested,
        "normalized": normalized,
        "registered_before": normalized in mr.backends,
    }

    if normalized in mr.backends:
        return normalized, resolution

    if normalized == "ollama":
        ollama_status = status_override or SystemTools.get_ollama_status()
        resolution["ollama_status"] = {
            "installed": bool(ollama_status.get("installed")),
            "running": bool(ollama_status.get("running")),
            "model_count": len(ollama_status.get("models", []) or []),
        }
        if not ollama_status.get("running"):
            return None, resolution

        config = BackendConfig(
            backend_type=BackendType.OLLAMA,
            name="ollama",
            endpoint_url="http://localhost:11434",
            available_models=list(ollama_status.get("models", []) or []),
            default_model=(ollama_status.get("models") or [None])[0],
        )
        try:
            registered = await mr.register_backend(config)
        except Exception as exc:
            resolution["registration_error"] = str(exc)
            logger.warning("Ollama backend auto-registration failed: %s", exc, exc_info=True)
            return None, resolution

        resolution["registered_after"] = normalized in mr.backends
        resolution["auto_registered"] = bool(registered)
        if registered and normalized in mr.backends:
            mr.backends[normalized].available_models = list(ollama_status.get("models", []) or [])
            if ollama_status.get("models"):
                mr.backends[normalized].default_model = ollama_status["models"][0]
            return normalized, resolution
        return None, resolution

    return (normalized if normalized in mr.backends else None), resolution


async def _sync_live_local_backends(mr, *, status_override: Optional[dict[str, Any]] = None) -> None:
    if not mr:
        return
    resolved, resolution = await _resolve_backend_name(mr, "ollama", status_override=status_override)
    if resolution.get("ollama_status", {}).get("running"):
        logger.info("Model router backend resolution on page load: %s", resolution)
    if resolved and resolved in mr.backends:
        return


# =============================================================================
# Page route
# =============================================================================

@router.get("/router", response_class=HTMLResponse)
async def model_router_page(request: Request):
    mr = get_model_router()
    from services.secrets import get_secrets_manager

    from admin.routers.settings import _build_model_setup_guidance, _build_onboarding_state

    cached_ollama_status = peek_cache("ollama_status")
    if cached_ollama_status and cached_ollama_status.get("running"):
        await _sync_live_local_backends(mr, status_override=cached_ollama_status)
    onboarding_state = await _build_onboarding_state(prefer_cached_provider_state=True)
    model_setup_guidance = _build_model_setup_guidance(onboarding_state)

    pipeline_config = None
    if mr and mr.pipeline:
        pipeline_config = {
            "name": mr.pipeline.name,
            "roles": {
                role.value: {
                    "enabled": cfg.enabled,
                    "backend_name": cfg.backend_name,
                    "model": cfg.model,
                    "temperature": cfg.temperature,
                    "max_tokens": cfg.max_tokens,
                    "system_prompt_override": cfg.system_prompt_override,
                    "ollama_options": cfg.ollama_options,
                }
                for role, cfg in mr.pipeline.roles.items()
            },
        }

    secrets = get_secrets_manager()
    all_backends = {
        "ollama": {"type": "ollama", "display_name": "Ollama (Local)", "description": "Run models locally",
                    "icon": "🦙", "configured": False, "status": "not_running", "models": [],
                    "setup_hint": "Install Ollama from the Dashboard", "requires_key": False},
        "llamacpp": {"type": "llamacpp", "display_name": "llama.cpp (Local)", "description": "2-3x faster than Ollama — power user backend",
                     "icon": "⚡", "configured": False, "status": "not_installed", "models": [],
                     "setup_hint": "Install llama.cpp", "requires_key": False},
        "openai": {"type": "openai", "display_name": "OpenAI", "description": "GPT-4, GPT-5, o1, o3",
                    "icon": "🤖", "configured": False, "status": "no_key", "models": [],
                    "setup_hint": "Add API key in Settings → LLM Providers", "requires_key": True},
        "anthropic": {"type": "anthropic", "display_name": "Anthropic", "description": "Claude 3.5 Sonnet, Haiku, Opus",
                      "icon": "🧠", "configured": False, "status": "no_key", "models": [],
                      "setup_hint": "Add API key in Settings → LLM Providers", "requires_key": True},
        "openrouter": {"type": "openrouter", "display_name": "OpenRouter", "description": "Access 100+ models",
                       "icon": "🌐", "configured": False, "status": "no_key", "models": [],
                       "setup_hint": "Add API key in Settings → LLM Providers", "requires_key": True},
    }
    if mr:
        for name, cfg in mr.backends.items():
            if name in all_backends:
                all_backends[name].update(configured=True, status="ready",
                                          models=cfg.available_models, default_model=cfg.default_model)
    for provider in ("openai", "anthropic", "openrouter"):
        if secrets.get(provider):
            if not all_backends[provider]["configured"]:
                all_backends[provider]["status"] = "key_set"

    ollama_status = cached_ollama_status or {"installed": False, "running": False, "models": []}
    if ollama_status.get("running"):
        # Merge live model list into the backend even if not yet configured
        live_models = ollama_status.get("models", [])
        if live_models:
            all_backends["ollama"]["models"] = live_models
        if all_backends["ollama"]["configured"]:
            all_backends["ollama"]["status"] = "ready"
        else:
            # Running with models but not yet saved to config — still allow selection
            all_backends["ollama"]["status"] = "ready" if live_models else "running_no_models"
    elif ollama_status.get("installed"):
        all_backends["ollama"]["status"] = "installed_not_running"

    # llama.cpp status
    try:
        from services.llamacpp_manager import get_llamacpp_manager
        lcpp = get_llamacpp_manager()
        lcpp_status = lcpp.get_status()
        if lcpp_status.running:
            all_backends["llamacpp"]["status"] = "ready"
            all_backends["llamacpp"]["models"] = lcpp_status.available_models
            all_backends["llamacpp"]["configured"] = "llamacpp" in (mr.backends if mr else {})
        elif lcpp_status.installed:
            all_backends["llamacpp"]["status"] = "installed_not_running"
            all_backends["llamacpp"]["models"] = lcpp_status.available_models
        else:
            all_backends["llamacpp"]["status"] = "not_installed"
    except Exception:
        pass  # llama.cpp is optional

    return templates.TemplateResponse(
        request,
        "model_router.html",
        {
            "active_page": "router",
            "pipeline": pipeline_config,
            "backends": all_backends,
            "onboarding_state": onboarding_state,
            "model_setup_guidance": model_setup_guidance,
        },
    )


# =============================================================================
# API routes
# =============================================================================

@router.get("/api/v1/router/backends")
async def api_get_backends():
    mr = get_model_router()
    if not mr:
        return {"success": False, "error": "Router not initialized"}
    backends = {}
    for name, cfg in mr.backends.items():
        backends[name] = {
            "type": cfg.backend_type.value,
            "name": cfg.name,
            "models": cfg.available_models,
            "default_model": cfg.default_model,
            "endpoint": cfg.endpoint_url,
        }
    return {"success": True, "backends": backends}


@router.get("/api/v1/router/pipeline")
async def api_get_pipeline():
    mr = get_model_router()
    if not mr or not mr.pipeline:
        return {"success": False, "error": "No pipeline configured"}
    return {
        "success": True,
        "name": mr.pipeline.name,
        "roles": {
            role.value: {
                "enabled": cfg.enabled,
                "backend_name": cfg.backend_name,
                "model": cfg.model,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
            }
            for role, cfg in mr.pipeline.roles.items()
        },
    }


@router.post("/api/v1/router/pipeline")
async def api_update_pipeline(config: PipelineConfigUpdate):
    from services.model_router import PipelineConfig, PipelineRole, RoleConfig
    mr = get_model_router()
    if not mr:
        return {"success": False, "error": "Router not initialized"}
    roles = {}
    for role_name, role_config in [("initial", config.initial), ("critique", config.critique), ("synthesize", config.synthesize)]:
        if role_config and role_config.enabled:
            role = PipelineRole(role_name)
            resolved_backend, resolution = await _resolve_backend_name(mr, role_config.backend_name)
            logger.info("Model router backend resolution for %s: %s", role_name, resolution)
            if not resolved_backend:
                return {
                    "success": False,
                    "error": "The selected AI provider is not connected yet. Start or refresh the provider, then try saving again.",
                }
            roles[role] = RoleConfig(role=role, backend_name=resolved_backend, model=role_config.model,
                                     temperature=role_config.temperature, max_tokens=role_config.max_tokens, enabled=True,
                                     ollama_options=role_config.ollama_options or {})
    mr.configure_pipeline(PipelineConfig(name="custom", roles=roles))
    try:
        _save_pipeline_to_file()
    except Exception as exc:
        logger.error("Pipeline save failed after configure: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"Pipeline was applied in memory but could not be saved to disk: {exc}. Check file permissions on the config directory.",
        }
    return {"success": True, "roles_configured": [r.value for r in roles]}


@router.post("/api/v1/router/pipeline/role/{role_name}")
async def api_update_single_role(role_name: str, config: RoleConfigUpdate):
    from services.model_router import PipelineConfig, PipelineRole, RoleConfig
    mr = get_model_router()
    if not mr:
        return {"success": False, "error": "Router not initialized"}
    if role_name not in ("initial", "critique", "synthesize"):
        return {"success": False, "error": f"Invalid role: {role_name}"}
    resolved_backend, resolution = await _resolve_backend_name(mr, config.backend_name)
    logger.info("Model router backend resolution for %s: %s", role_name, resolution)
    if not resolved_backend:
        return {
            "success": False,
            "error": "The selected AI provider is not connected yet. Start or refresh the provider, then try saving again.",
        }
    role = PipelineRole(role_name)
    existing_roles = dict(mr.pipeline.roles) if mr.pipeline else {}
    existing_roles, dropped_roles = _prune_unavailable_roles(mr, existing_roles)
    if dropped_roles:
        logger.warning("Dropping unavailable pipeline roles before save: %s", dropped_roles)
    if config.enabled:
        # Merge caller's ollama_options with defaults
        _default_ollama_opts = {
            "num_ctx": 8192,
            "repeat_penalty": 1.1,
            "top_k": 40,
            "top_p": 0.9,
            "stop": ["\n\nUser:", "\n\nHuman:", "---END---"],
        }
        merged_opts = {**_default_ollama_opts, **(config.ollama_options or {})}
        existing_roles[role] = RoleConfig(role=role, backend_name=resolved_backend, model=config.model,
                                          temperature=config.temperature, max_tokens=config.max_tokens,
                                          system_prompt_override=config.system_prompt_override, enabled=True,
                                          ollama_options=merged_opts)
    else:
        existing_roles.pop(role, None)
    try:
        mr.configure_pipeline(PipelineConfig(name="custom", roles=existing_roles))
    except ValueError as exc:
        logger.warning("Single-role pipeline configure failed: %s", exc)
        return {
            "success": False,
            "error": "The selected AI provider is not available yet. Refresh the provider list, then try saving again.",
        }
    try:
        _save_pipeline_to_file()
    except Exception as exc:
        logger.error("Single-role save failed after configure: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"Role was applied in memory but could not be saved to disk: {exc}. Check file permissions on the config directory.",
        }
    return {"success": True, "role": role_name, "backend_name": resolved_backend}


@router.post("/api/v1/router/test")
async def api_test_pipeline():
    mr = get_model_router()
    if not mr or not mr.pipeline:
        return {"success": False, "error": "No pipeline configured"}
    prompt = "In one sentence, what is the most important thing for a small team?"
    try:
        start = time.time()
        result = await mr.generate_pipeline(user_prompt=prompt, context="", system_prompt="Be concise.")
        elapsed = time.time() - start
        return {"success": True, "prompt": prompt, "final": result["final"], "stages": result["stages"],
                "models_used": result["models_used"], "elapsed_seconds": round(elapsed, 2)}
    except Exception as e:
        logger.error(f"Pipeline test failed: {e}")
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/router/backends/refresh")
async def api_refresh_backends():
    mr = get_model_router()
    if not mr:
        return {"success": False, "error": "Router not initialized"}
    refreshed = []
    try:
        from admin.server import _register_cloud_backends_from_secrets
        await _register_cloud_backends_from_secrets()
        refreshed.append("cloud backends re-registered")
    except Exception as e:
        logger.warning(f"Could not refresh cloud backends: {e}")
    for name, client in list(mr.clients.items()):
        try:
            models = await client.list_models()
            if name in mr.backends:
                mr.backends[name].available_models = models
            refreshed.append(f"{name}: {len(models)} models")
        except Exception as e:
            logger.warning(f"Could not refresh {name}: {e}")
    return {"success": True, "refreshed": refreshed}


# =============================================================================
# Pipeline Presets
# =============================================================================

@router.get("/api/v1/router/presets")
async def api_list_presets():
    """List available pipeline presets with suitability info."""
    try:
        from services.pipeline_presets import list_presets
        presets = await list_presets()
        return {"success": True, "presets": presets}
    except Exception as e:
        logger.error("Failed to list presets: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/router/presets/{preset_name}/apply")
async def api_apply_preset(preset_name: str):
    """Apply a named pipeline preset (speed / balanced / quality).

    Resolves preset model preferences against installed models and
    configures the full pipeline accordingly.
    """
    mr = get_model_router()
    if not mr:
        return {"success": False, "error": "Router not initialized"}

    try:
        from services.pipeline_presets import resolve_preset

        # Determine which backend to use (prefer ollama, fall back to first available)
        backend_name = "ollama"
        if "ollama" not in mr.backends:
            if mr.backends:
                backend_name = next(iter(mr.backends))
            else:
                return {"success": False, "error": "No backends registered"}

        config, error = await resolve_preset(preset_name, backend_name=backend_name)
        if error:
            return {"success": False, "error": error}

        mr.configure_pipeline(config)
        _save_pipeline_to_file()

        # Build response with resolved model info
        roles_info = {}
        for role, cfg in config.roles.items():
            roles_info[role.value] = {
                "model": cfg.model,
                "backend": cfg.backend_name,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
            }

        return {
            "success": True,
            "preset": preset_name,
            "pipeline_name": config.name,
            "roles": roles_info,
        }
    except Exception as e:
        logger.error("Failed to apply preset '%s': %s", preset_name, e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Auto-Configure Pipeline (hardware-adaptive)
# =============================================================================

@router.post("/api/v1/router/auto-configure")
async def api_auto_configure_pipeline():
    """Re-run hardware-adaptive pipeline auto-configuration.

    Scans hardware tier, installed models, and available API keys to
    write the best-fit ``model_router.json``.  Useful after installing
    new models or adding API keys.
    """
    try:
        from services.pipeline_presets import auto_configure_pipeline
        summary = await auto_configure_pipeline()
        return {"success": True, **summary}
    except Exception as e:
        logger.error("Auto-configure pipeline failed: %s", e, exc_info=True)
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# Token Estimation / Truncation Monitor
# =============================================================================

@router.get("/api/v1/router/token-monitor")
async def api_token_monitor():
    """Return recent truncation events and aggregate stats."""
    try:
        from services.token_estimator import get_token_estimator
        estimator = get_token_estimator()
        return {
            "success": True,
            "stats": estimator.get_stats(),
            "recent_events": estimator.get_recent_events(50),
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


# =============================================================================
# VSLM Auto-Router
# =============================================================================

@router.get("/api/v1/router/vslm/status")
async def api_vslm_status():
    """Return VSLM router status and classification stats."""
    try:
        from services.vslm_router import get_vslm_router
        vslm = get_vslm_router()
        return {"success": True, **vslm.get_status()}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/router/vslm/configure")
async def api_vslm_configure(request: Request):
    """Configure the VSLM router (enable/disable, change model)."""
    try:
        from services.vslm_router import get_vslm_router
        body = await request.json()
        vslm = get_vslm_router()
        vslm.configure(
            model=body.get("model"),
            backend_name=body.get("backend", "ollama"),
            enabled=body.get("enabled", True),
        )
        return {"success": True, "message": "VSLM router configured", **vslm.get_status()}
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}


@router.post("/api/v1/router/vslm/test")
async def api_vslm_test(request: Request):
    """Test VSLM classification on a sample query (does not execute pipeline)."""
    try:
        from services.vslm_router import get_vslm_router
        body = await request.json()
        query = body.get("query", "")
        if not query:
            return {"success": False, "error": "No query provided"}

        vslm = get_vslm_router()
        result = await vslm.classify(query, context_snippet=body.get("context", ""))
        return {
            "success": True,
            "complexity": result.complexity.value,
            "preset": result.preset_name,
            "confidence": result.confidence,
            "latency_ms": result.latency_ms,
            "model": result.model_used,
            "raw_output": result.raw_output,
        }
    except Exception:
        return {"success": False, "error": "request_failed", "message": "Something went wrong. Please try again."}

