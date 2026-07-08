"""
Pipeline Presets — pre-configured pipeline configurations that match
installed models to "Speed", "Balanced", or "Quality" presets.

Reads preset templates from ``config/recommended_models.json`` and resolves
them against actually-installed models to produce a ready-to-apply
``PipelineConfig``.

Also provides ``auto_configure_pipeline()`` which is called at the end of
setup to write a ``model_router.json`` tailored to the user's hardware
tier + installed models + available API keys.  Because it draws from the
model catalog (``recommended_models.json``), it automatically picks up
newly-added frontier models without code changes.

Usage
-----
    from services.pipeline_presets import resolve_preset, auto_configure_pipeline

    config = await resolve_preset("balanced")
    router.configure_pipeline(config)

    # Or, at end of setup wizard:
    result = await auto_configure_pipeline()
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
ROUTER_CONFIG_PATH = CONFIG_DIR / "model_router.json"


def _load_preset_definitions() -> Dict[str, Any]:
    """Load pipeline_presets from recommended_models.json."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "recommended_models.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("pipeline_presets", {})
    except Exception as e:
        logger.warning("Could not load pipeline presets: %s", e)
        return {}


async def get_installed_model_set() -> set[str]:
    """Get names of locally installed Ollama models."""
    try:
        from services.model_discovery import get_installed_model_names
        installed = await get_installed_model_names()
        # Normalise: "qwen3:8b:latest" → "qwen3:8b"
        normalised = set()
        for m in installed:
            name = m.replace(":latest", "")
            normalised.add(name)
        return normalised
    except Exception:
        return set()


def _pick_best_model(
    preference_list: List[str],
    installed: set[str],
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Pick the first model from preference_list that is installed.

    Falls back to *any* installed general model, then to ``fallback``.
    """
    for model in preference_list:
        if model in installed:
            return model
    # Try partial matching (e.g. "qwen3.5:35b-a3b" matches "qwen3.5:35b-a3b-q4_K_M")
    for model in preference_list:
        base = model.split(":")[0]
        for inst in installed:
            if inst.startswith(base):
                return inst
    return fallback


def _resolve_cloud_backend(role_def: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """If a role uses ``auto_cloud``, resolve to the first available
    cloud provider.  Returns ``(backend_name, model)`` or ``None``.
    """
    if role_def.get("backend") != "auto_cloud":
        return None
    cloud_prefs = role_def.get("cloud_model_preference", {})
    if not cloud_prefs:
        return None
    cloud = _get_available_cloud_backends()
    for provider in cloud_prefs:
        if provider in cloud:
            return provider, cloud_prefs[provider]
    return None


async def list_presets() -> List[Dict[str, Any]]:
    """Return available presets with metadata and suitability info.

    Each preset includes:
    - name, display, description
    - can_apply: whether all required models are installed (or cloud available)
    - resolved_models: which actual models would be used
    """
    presets_def = _load_preset_definitions()
    if not presets_def:
        return []

    installed = await get_installed_model_set()
    result = []

    for name, preset in presets_def.items():
        resolved_models: Dict[str, Optional[str]] = {}
        all_found = True

        for role in ("initial", "critique", "synthesize"):
            role_def = preset.get(role, {})
            if role_def.get("enabled") is False:
                resolved_models[role] = None
                continue

            # Check for cloud-backed roles first
            cloud_pick = _resolve_cloud_backend(role_def)
            if cloud_pick:
                resolved_models[role] = f"{cloud_pick[0]}:{cloud_pick[1]}"
                continue

            prefs = role_def.get("model_preference", [])
            if prefs:
                picked = _pick_best_model(prefs, installed)
                resolved_models[role] = picked
                if not picked:
                    all_found = False
            elif role_def.get("backend") == "auto_cloud":
                # Cloud role but no cloud keys available
                resolved_models[role] = None
                all_found = False
            else:
                resolved_models[role] = None
                all_found = False

        result.append({
            "name": name,
            "display": preset.get("display", name.title()),
            "description": preset.get("description", ""),
            "can_apply": all_found,
            "resolved_models": resolved_models,
            "roles": {
                role: {
                    "enabled": preset.get(role, {}).get("enabled", True) if preset.get(role) else False,
                    "temperature": preset.get(role, {}).get("temperature"),
                    "max_tokens": preset.get(role, {}).get("max_tokens"),
                    "model_preference": preset.get(role, {}).get("model_preference", []),
                    "resolved_model": resolved_models.get(role),
                }
                for role in ("initial", "critique", "synthesize")
            },
        })

    return result


async def resolve_preset(
    preset_name: str,
    backend_name: str = "ollama",
) -> Tuple[Optional[Any], str]:
    """Resolve a preset name into a PipelineConfig.

    Returns (PipelineConfig, "") on success or (None, error_message) on failure.
    """
    from services.model_router import PipelineConfig, PipelineRole, RoleConfig

    presets_def = _load_preset_definitions()
    if preset_name not in presets_def:
        return None, f"Unknown preset: '{preset_name}'. Available: {', '.join(presets_def.keys())}"

    preset = presets_def[preset_name]
    installed = await get_installed_model_set()

    roles: Dict[PipelineRole, RoleConfig] = {}
    missing_models: List[str] = []

    for role_name, role_enum in [
        ("initial", PipelineRole.INITIAL),
        ("critique", PipelineRole.CRITIQUE),
        ("synthesize", PipelineRole.SYNTHESIZE),
    ]:
        role_def = preset.get(role_name, {})
        if not role_def or role_def.get("enabled") is False:
            continue

        # Check for cloud-backed roles
        cloud_pick = _resolve_cloud_backend(role_def)
        if cloud_pick:
            roles[role_enum] = RoleConfig(
                role=role_enum,
                backend_name=cloud_pick[0],
                model=cloud_pick[1],
                temperature=role_def.get("temperature", 0.3),
                max_tokens=role_def.get("max_tokens", 4000),
                enabled=True,
            )
            continue

        prefs = role_def.get("model_preference", [])
        model = _pick_best_model(prefs, installed)
        if not model:
            if role_def.get("backend") == "auto_cloud":
                missing_models.append(f"{role_name} (needs cloud API key)")
            else:
                missing_models.append(f"{role_name} (wanted: {', '.join(prefs[:3])})")
            continue

        roles[role_enum] = RoleConfig(
            role=role_enum,
            backend_name=backend_name,
            model=model,
            temperature=role_def.get("temperature", 0.3),
            max_tokens=role_def.get("max_tokens", 4000),
            enabled=True,
        )

    if not roles:
        return None, f"Cannot apply preset '{preset_name}': no models installed for any role. Need: {missing_models}"

    if missing_models:
        logger.warning("Preset '%s' partially applied — missing: %s", preset_name, missing_models)

    config = PipelineConfig(
        name=f"preset:{preset_name}",
        roles=roles,
    )

    return config, ""


# =============================================================================
# Auto-configuration — called once at end of setup to write model_router.json
# =============================================================================

def _get_available_cloud_backends() -> Dict[str, str]:
    """Return {backend_name: default_model} for each cloud provider whose
    API key is set.  Checks environment and keyring.
    """
    result: Dict[str, str] = {}
    try:
        from services.secrets import get_secrets_manager
        sm = get_secrets_manager()
        if sm.get("openai"):
            result["openai"] = "gpt-4o"
        if sm.get("anthropic"):
            result["anthropic"] = "claude-sonnet-4-20250514"
        if sm.get("openrouter"):
            result["openrouter"] = "anthropic/claude-sonnet-4-20250514"
    except Exception as e:
        logger.warning("_get_available_cloud_backends: suppressed %s", e)
    # Also check env vars directly (setup may have just written .env)
    import os
    if not result.get("openai") and os.getenv("OPENAI_API_KEY"):
        result["openai"] = "gpt-4o"
    if not result.get("anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        result["anthropic"] = "claude-sonnet-4-20250514"
    if not result.get("openrouter") and os.getenv("OPENROUTER_API_KEY"):
        result["openrouter"] = "anthropic/claude-sonnet-4-20250514"
    return result


def _best_local_model_for_role(
    role: str,
    tier: str,
    installed: set[str],
    catalog: List[Dict[str, Any]],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Pick the best installed model for a role from the catalog.

    For *initial*: prefer highest-generation general model that fits tier.
    For *critique*: prefer reasoning specialists (deepseek-r1) then large
                    general models.
    For *synthesize*: prefer mid-size general models (fast enough for second pass).

    Returns (model_name, catalog_entry) or None.
    """
    # Filter to models in this tier and not deprecated
    eligible = [
        e for e in catalog
        if tier in e.get("tiers", [])
        and not e.get("deprecated")
        and e.get("category") in ("general", "specialist")
        and e["model"] in installed
    ]
    if not eligible:
        return None

    if role == "critique":
        # Prefer reasoning specialists, then largest general
        specialists = [e for e in eligible if e.get("category") == "specialist"]
        if specialists:
            specialists.sort(key=lambda e: (-e.get("generation", 0), -e.get("param_b", 0)))
            return specialists[0]["model"], specialists[0]
        eligible.sort(key=lambda e: (-e.get("generation", 0), -e.get("param_b", 0)))
        return eligible[0]["model"], eligible[0]

    if role == "synthesize":
        # Prefer smaller but current-gen (fast enough for a refinement pass)
        eligible.sort(key=lambda e: (-e.get("generation", 0), e.get("param_b", 999)))
        return eligible[0]["model"], eligible[0]

    # initial — prefer highest generation, then MoE (better speed/quality), then largest
    def _initial_sort_key(e: Dict[str, Any]) -> tuple:
        gen = e.get("generation", 0)
        is_moe = 1 if e.get("architecture") == "moe" else 0
        params = e.get("active_param_b") or e.get("param_b", 0)
        return (-gen, -is_moe, -params)

    eligible.sort(key=_initial_sort_key)
    return eligible[0]["model"], eligible[0]


def _build_role_dict(
    backend_name: str,
    model: str,
    temperature: float = 0.4,
    max_tokens: int = 4000,
    *,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Build the role dict for model_router.json."""
    role: Dict[str, Any] = {
        "backend_name": backend_name,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enabled": enabled,
        "system_prompt_override": None,
    }
    # Ollama-specific tuning defaults
    if backend_name == "ollama":
        role["ollama_options"] = {
            "num_ctx": 8192,
            "repeat_penalty": 1.1,
            "top_k": 40,
            "top_p": 0.9,
            "stop": ["\n\nUser:", "\n\nHuman:", "---END---"],
        }
    return role


async def auto_configure_pipeline() -> Dict[str, Any]:
    """Sniff hardware tier + installed models + API keys and write the
    best-fit ``model_router.json``.

    Called automatically when setup completes.  Returns a summary dict
    describing what was configured and why.

    Decision matrix (tier × cloud keys):
    ─────────────────────────────────────────────────────────────────────
    cloud_only + keys  → Phase 1 cloud, Phase 2–3 off
    cloud_only + no keys → cannot configure — leave empty, warn
    lite + no keys     → Phase 1 smallest local (phi4-mini/gemma3:4b)
    lite + keys        → Phase 1 cloud (faster than tiny local)
    standard + no keys → Phase 1 best local (MoE preferred)
    standard + keys    → Phase 1 best local, Phase 2 cloud critique
    power + no keys    → Phase 1 best local, Phase 2 local critique
    power + keys       → Phase 1 best local, Phase 2 cloud critique
    workstation + no k → Phase 1 best, Phase 2 local critique, Phase 3 local synth
    workstation + keys → Phase 1 best, Phase 2 cloud critique, Phase 3 local synth
    ─────────────────────────────────────────────────────────────────────

    The model catalog's ``tiers[]`` arrays ensure that when new frontier
    models are added to ``recommended_models.json``, they're automatically
    picked up here without code changes.
    """
    from services.device_capability import DeviceCapabilityScanner, get_effective_catalog

    summary: Dict[str, Any] = {"configured": False, "decisions": []}

    # 1. Sniff hardware
    try:
        scanner = DeviceCapabilityScanner()
        report = await scanner.scan()
        tier = report.profile.tier.value
    except Exception as exc:
        logger.warning("Device scan failed during auto-config: %s", exc)
        tier = "standard"  # safe default
        summary["decisions"].append(f"Device scan failed ({exc}); assuming standard tier")

    # 2. Discover installed local models
    installed = await get_installed_model_set()
    summary["tier"] = tier
    summary["installed_models"] = sorted(installed)

    # 3. Discover available cloud backends
    cloud = _get_available_cloud_backends()
    has_cloud = bool(cloud)
    summary["cloud_backends"] = list(cloud.keys())

    # Pick first available cloud backend + model for cloud roles
    cloud_backend = next(iter(cloud), None)
    cloud_model = cloud.get(cloud_backend, "") if cloud_backend else ""

    # 4. Load the catalog
    catalog = get_effective_catalog()

    # 5. Build pipeline roles
    roles: Dict[str, Dict[str, Any]] = {}

    # ── Phase 1 (always) ────────────────────────────────────────────────
    if tier == "cloud_only":
        if has_cloud:
            roles["initial"] = _build_role_dict(cloud_backend, cloud_model, 0.4, 4000)
            summary["decisions"].append(
                f"Phase 1 → {cloud_backend}/{cloud_model} (cloud_only tier, local models too slow)"
            )
        else:
            summary["decisions"].append(
                "No local or cloud backend available — pipeline left unconfigured. "
                "Add an API key or install Ollama with a model."
            )
            summary["configured"] = False
            return summary
    elif tier == "lite" and has_cloud:
        # Tiny local models are slow; cloud is a better default for casuals
        roles["initial"] = _build_role_dict(cloud_backend, cloud_model, 0.4, 4000)
        summary["decisions"].append(
            f"Phase 1 → {cloud_backend}/{cloud_model} (lite tier + cloud keys = faster than tiny local)"
        )
    else:
        # Standard / power / workstation / lite-without-cloud: use best local
        pick = _best_local_model_for_role("initial", tier, installed, catalog)
        if pick:
            model_name, entry = pick
            roles["initial"] = _build_role_dict("ollama", model_name, 0.4, 4000)
            summary["decisions"].append(
                f"Phase 1 → ollama/{model_name} (best local for {tier} tier)"
            )
        elif has_cloud:
            roles["initial"] = _build_role_dict(cloud_backend, cloud_model, 0.4, 4000)
            summary["decisions"].append(
                f"Phase 1 → {cloud_backend}/{cloud_model} (no local models installed, falling back to cloud)"
            )
        else:
            summary["decisions"].append(
                "No suitable model for Phase 1 — install a model via Ollama or add an API key."
            )
            summary["configured"] = False
            return summary

    # ── Phase 2 (critique — optional) ───────────────────────────────────
    if tier in ("power", "workstation"):
        if has_cloud:
            roles["critique"] = _build_role_dict(cloud_backend, cloud_model, 0.2, 4000)
            summary["decisions"].append(
                f"Phase 2 → {cloud_backend}/{cloud_model} (cloud critique for {tier} tier)"
            )
        else:
            pick = _best_local_model_for_role("critique", tier, installed, catalog)
            if pick:
                model_name, entry = pick
                roles["critique"] = _build_role_dict("ollama", model_name, 0.2, 4000)
                summary["decisions"].append(
                    f"Phase 2 → ollama/{model_name} (local critique for {tier} tier)"
                )
            else:
                summary["decisions"].append("Phase 2 skipped — no suitable critique model installed")
    elif tier == "standard" and has_cloud:
        roles["critique"] = _build_role_dict(cloud_backend, cloud_model, 0.2, 4000)
        summary["decisions"].append(
            f"Phase 2 → {cloud_backend}/{cloud_model} (cloud critique for standard tier)"
        )
    else:
        summary["decisions"].append(
            f"Phase 2 skipped — {tier} tier {'without cloud keys' if not has_cloud else ''}"
        )

    # ── Phase 3 (synthesize — only for workstation) ─────────────────────
    if tier == "workstation":
        pick = _best_local_model_for_role("synthesize", tier, installed, catalog)
        if pick:
            model_name, entry = pick
            roles["synthesize"] = _build_role_dict("ollama", model_name, 0.3, 6000)
            summary["decisions"].append(
                f"Phase 3 → ollama/{model_name} (local synthesis for workstation tier)"
            )
        else:
            summary["decisions"].append("Phase 3 skipped — no suitable synthesis model installed")
    else:
        summary["decisions"].append(f"Phase 3 skipped — {tier} tier (single/dual phase is sufficient)")

    # 6. Write model_router.json
    config = {
        "pipeline": {
            "name": f"auto:{tier}",
            "roles": roles,
        }
    }

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(ROUTER_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    summary["configured"] = True
    summary["pipeline_name"] = config["pipeline"]["name"]
    summary["roles"] = {
        role: {"backend": d["backend_name"], "model": d["model"]}
        for role, d in roles.items()
    }
    logger.info(
        "Auto-configured pipeline: %s (tier=%s, cloud=%s, local_models=%d)",
        config["pipeline"]["name"], tier, list(cloud.keys()), len(installed),
    )
    for d in summary["decisions"]:
        logger.info("  → %s", d)

    return summary
