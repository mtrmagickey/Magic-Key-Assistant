"""
Model Discovery Service — dynamic model catalog with upgrade detection.

Replaces static hardcoded model lists with a dynamic system that:
1. Reads a curated ``config/recommended_models.json`` catalog
2. Queries Ollama's live ``/api/tags`` to see what's actually installed
3. Computes upgrade suggestions (installed model X → newer model Y)
4. Exposes a unified model catalog that other modules can import

The recommended_models.json file is the single source of truth for which
models exist, their hardware requirements, and upgrade paths.  Updating
it (manually or via future auto-update) immediately makes new models
available throughout the system — no code changes needed.

Usage
-----
    from services.model_discovery import get_model_catalog, check_upgrades

    catalog = get_model_catalog()          # cached, reads JSON once
    upgrades = await check_upgrades()      # compares installed vs recommended
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_RECOMMENDED_MODELS_PATH = _CONFIG_DIR / "recommended_models.json"

# ── Cache TTL ─────────────────────────────────────────────────────────────────

_CATALOG_CACHE_TTL = 300          # re-read JSON every 5 minutes
_OLLAMA_CACHE_TTL = 60            # re-query Ollama every 60 seconds

# ── Timeout defaults (seconds) ───────────────────────────────────────────────
# Override via the ``pipeline.timeouts`` section of model_router.json.

_HEALTH_CHECK_TIMEOUT = 5
_MODEL_LIST_TIMEOUT = 10
_BENCHMARK_TIMEOUT_PER_MODEL = 2700  # 45 minutes

_catalog_cache: Optional[Dict[str, Any]] = None
_catalog_cache_time: float = 0.0
_ollama_cache: Optional[List[Dict[str, Any]]] = None
_ollama_cache_time: float = 0.0


# ═════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ModelUpgrade:
    """A suggested model upgrade."""
    installed_model: str
    installed_display: str
    installed_generation: int
    upgrade_model: str
    upgrade_display: str
    upgrade_generation: int
    upgrade_size_gb: float
    reason: str
    family: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed_model": self.installed_model,
            "installed_display": self.installed_display,
            "installed_generation": self.installed_generation,
            "upgrade_model": self.upgrade_model,
            "upgrade_display": self.upgrade_display,
            "upgrade_generation": self.upgrade_generation,
            "upgrade_size_gb": self.upgrade_size_gb,
            "reason": self.reason,
            "family": self.family,
        }


@dataclass
class DiscoveryReport:
    """Full model discovery report."""
    installed_models: List[str] = field(default_factory=list)
    catalog_models: List[Dict[str, Any]] = field(default_factory=list)
    upgrades: List[ModelUpgrade] = field(default_factory=list)
    unknown_installed: List[str] = field(default_factory=list)
    catalog_updated: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed_models": self.installed_models,
            "catalog_model_count": len(self.catalog_models),
            "upgrades_available": len(self.upgrades),
            "upgrades": [u.to_dict() for u in self.upgrades],
            "unknown_installed": self.unknown_installed,
            "catalog_updated": self.catalog_updated,
            "warnings": self.warnings,
        }


# ═════════════════════════════════════════════════════════════════════════════
# CATALOG LOADING
# ═════════════════════════════════════════════════════════════════════════════

def _load_catalog_raw() -> Dict[str, Any]:
    """Read and parse recommended_models.json with caching."""
    global _catalog_cache, _catalog_cache_time

    now = time.time()
    if _catalog_cache is not None and (now - _catalog_cache_time) < _CATALOG_CACHE_TTL:
        return _catalog_cache

    path = _RECOMMENDED_MODELS_PATH
    if not path.exists():
        logger.warning("recommended_models.json not found at %s — using empty catalog", path)
        _catalog_cache = {"models": [], "upgrade_paths": {}, "enrichment_preference_order": []}
        _catalog_cache_time = now
        return _catalog_cache

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _catalog_cache = data
        _catalog_cache_time = now
        logger.debug("Loaded model catalog: %d models", len(data.get("models", [])))
        return data
    except Exception as exc:
        logger.error("Failed to read recommended_models.json: %s", exc)
        if _catalog_cache is not None:
            return _catalog_cache  # stale is better than nothing
        return {"models": [], "upgrade_paths": {}, "enrichment_preference_order": []}


def get_model_catalog() -> List[Dict[str, Any]]:
    """Return the full list of recommended model entries.

    Each entry has: model, display, family, size_gb, param_b, purpose,
    category, min_ram_gb, min_vram_gb, tiers, enrichment_priority,
    generation, and optionally supersedes.
    """
    data = _load_catalog_raw()
    return data.get("models", [])


def get_enrichment_preference_order() -> List[str]:
    """Return the ordered list of model names preferred for chunk enrichment."""
    data = _load_catalog_raw()
    return data.get("enrichment_preference_order", [])


def get_upgrade_paths() -> Dict[str, str]:
    """Return the mapping of old_model → new_model upgrade suggestions."""
    data = _load_catalog_raw()
    return data.get("upgrade_paths", {})


def get_catalog_model_names() -> set[str]:
    """Return the set of all model names in the catalog."""
    return {entry["model"] for entry in get_model_catalog()}


def get_catalog_date() -> str:
    """Return the _updated date from the catalog."""
    data = _load_catalog_raw()
    return data.get("_updated", "unknown")


def invalidate_catalog_cache():
    """Force re-read of the catalog on next access."""
    global _catalog_cache, _catalog_cache_time
    _catalog_cache = None
    _catalog_cache_time = 0.0


# ═════════════════════════════════════════════════════════════════════════════
# OLLAMA LIVE QUERY
# ═════════════════════════════════════════════════════════════════════════════

async def _query_ollama_models() -> List[Dict[str, Any]]:
    """Query Ollama /api/tags and return the raw model list with caching."""
    global _ollama_cache, _ollama_cache_time

    now = time.time()
    if _ollama_cache is not None and (now - _ollama_cache_time) < _OLLAMA_CACHE_TTL:
        return _ollama_cache

    endpoint = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{endpoint}/api/tags", timeout=aiohttp.ClientTimeout(total=_HEALTH_CHECK_TIMEOUT)) as resp:
                if resp.status != 200:
                    logger.warning("Ollama /api/tags returned %d", resp.status)
                    return _ollama_cache or []
                data = await resp.json()
                models = data.get("models", [])
                _ollama_cache = models
                _ollama_cache_time = now
                return models
    except Exception as exc:
        logger.debug("Cannot query Ollama models: %s", exc)
        return _ollama_cache or []


async def get_installed_model_names() -> List[str]:
    """Return list of model names currently installed in Ollama."""
    models = await _query_ollama_models()
    return [m.get("name", "") for m in models if m.get("name")]


async def get_installed_model_details() -> List[Dict[str, Any]]:
    """Return detailed info about installed models (name, size, modified, etc.)."""
    models = await _query_ollama_models()
    result = []
    for m in models:
        result.append({
            "name": m.get("name", ""),
            "size_bytes": m.get("size", 0),
            "size_gb": round(m.get("size", 0) / (1024**3), 1),
            "modified_at": m.get("modified_at", ""),
            "digest": m.get("digest", "")[:12],
        })
    return result


# ═════════════════════════════════════════════════════════════════════════════
# UPGRADE DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _normalize_model_name(name: str) -> str:
    """Normalize model name for comparison (strip :latest tag)."""
    if name.endswith(":latest"):
        return name[:-7]
    return name


async def check_upgrades(tier: Optional[str] = None) -> DiscoveryReport:
    """Compare installed Ollama models against the catalog and find upgrades.

    Parameters
    ----------
    tier : optional
        If provided, only suggest upgrades that fit this capability tier
        (e.g. "standard", "power", "workstation").

    Returns a DiscoveryReport with upgrade suggestions and unknown models.
    """
    installed_raw = await get_installed_model_names()
    installed = {_normalize_model_name(m) for m in installed_raw}
    catalog = get_model_catalog()
    upgrade_paths = get_upgrade_paths()
    catalog_names = {entry["model"] for entry in catalog}
    catalog_by_name = {entry["model"]: entry for entry in catalog}

    upgrades: List[ModelUpgrade] = []
    unknown: List[str] = []

    # Find models installed but not in catalog
    for m in sorted(installed):
        if m not in catalog_names:
            unknown.append(m)

    # Check upgrade paths for installed models
    for old_model, new_model in upgrade_paths.items():
        if old_model not in installed:
            continue
        if new_model in installed:
            continue  # already have the upgrade

        old_entry = catalog_by_name.get(old_model, {})
        new_entry = catalog_by_name.get(new_model, {})

        if not new_entry:
            continue

        # If tier filter specified, check the upgrade fits
        if tier and tier not in new_entry.get("tiers", []):
            continue

        upgrades.append(ModelUpgrade(
            installed_model=old_model,
            installed_display=old_entry.get("display", old_model),
            installed_generation=old_entry.get("generation", 0),
            upgrade_model=new_model,
            upgrade_display=new_entry.get("display", new_model),
            upgrade_generation=new_entry.get("generation", 0),
            upgrade_size_gb=new_entry.get("size_gb", 0),
            reason=f"Newer generation ({new_entry.get('generation', '?')}) replaces gen {old_entry.get('generation', '?')} in the {new_entry.get('family', '')} family",
            family=new_entry.get("family", ""),
        ))

    # Sort: biggest generation jump first
    upgrades.sort(key=lambda u: u.upgrade_generation - u.installed_generation, reverse=True)

    warnings = []
    catalog_date = get_catalog_date()
    # Warn if catalog is old
    try:
        from datetime import datetime
        cat_dt = datetime.strptime(catalog_date, "%Y-%m-%d")
        age_days = (datetime.now() - cat_dt).days
        if age_days > 90:
            warnings.append(
                f"Model catalog was last updated {age_days} days ago ({catalog_date}). "
                f"Edit config/recommended_models.json to add newer models."
            )
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    if unknown:
        warnings.append(
            f"{len(unknown)} installed model(s) not in catalog: {', '.join(unknown[:5])}. "
            f"They work fine but won't get upgrade suggestions. "
            f"Add them to config/recommended_models.json if needed."
        )

    return DiscoveryReport(
        installed_models=sorted(installed),
        catalog_models=catalog,
        upgrades=upgrades,
        unknown_installed=unknown,
        catalog_updated=catalog_date,
        warnings=warnings,
    )


# ═════════════════════════════════════════════════════════════════════════════
# ENRICHMENT MODEL SELECTION (dynamic replacement for hardcoded priority)
# ═════════════════════════════════════════════════════════════════════════════

async def pick_best_enrichment_model(installed_models: Optional[List[str]] = None) -> Optional[str]:
    """Pick the best installed model for chunk enrichment using the catalog.

    Uses the ``enrichment_preference_order`` from recommended_models.json
    instead of hardcoded substring matching.  Falls back to the smallest
    installed general-purpose model if none from the preference list are
    installed.

    Parameters
    ----------
    installed_models : optional
        Pre-fetched list of installed model names.  If None, queries Ollama.

    Returns the model name string, or None if no models are installed.
    """
    if installed_models is None:
        installed_models = await get_installed_model_names()

    installed_set = {_normalize_model_name(m) for m in installed_models}

    # First: check explicit preference order
    prefs = get_enrichment_preference_order()
    for pref in prefs:
        if pref in installed_set:
            return pref

    # Fallback: pick smallest installed general-purpose model from catalog
    catalog = get_model_catalog()
    catalog_general = [
        e for e in catalog
        if e.get("category") in ("general", "specialist")
        and e["model"] in installed_set
    ]
    if catalog_general:
        catalog_general.sort(key=lambda e: e.get("size_gb", 999))
        return catalog_general[0]["model"]

    # Last resort: return first installed model (excluding embedding)
    non_embed = [m for m in installed_set if "embed" not in m.lower()]
    if non_embed:
        return sorted(non_embed)[0]

    return None


# ═════════════════════════════════════════════════════════════════════════════
# AUTO-DISCOVERY — EVERGREEN CATALOG UPDATE
# ═════════════════════════════════════════════════════════════════════════════

_VERSION_RE = re.compile(r"(\d+)(?:\.\d+)?")


async def _query_model_show(model_name: str) -> Optional[Dict[str, Any]]:
    """Query Ollama ``/api/show`` for detailed model metadata.

    Returns the ``details`` dict (keys: family, parameter_size,
    quantization_level) or *None* on failure.
    """
    endpoint = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session, session.post(
            f"{endpoint}/api/show",
            json={"name": model_name},
            timeout=aiohttp.ClientTimeout(total=_MODEL_LIST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("details", {})
    except Exception as exc:
        logger.debug("Cannot query /api/show for %s: %s", model_name, exc)
        return None


# ── Name / metadata inference helpers ─────────────────────────────────────


def _infer_family_from_name(model_name: str) -> str:
    """Best-effort family from model name (fallback when /api/show unavailable)."""
    base = model_name.split(":")[0]
    match = re.match(r"^([a-z]+)", base, re.IGNORECASE)
    return match.group(1).lower() if match else base.lower()


def _infer_generation_from_name(model_name: str) -> int:
    """Extract major version number from model name as generation."""
    base = model_name.split(":")[0]
    match = _VERSION_RE.search(base)
    if match:
        return int(match.group(1))
    return 1


def _param_str_to_float(param_str: str) -> float:
    """Convert ``'8.0B'`` or ``'70B'`` to a float."""
    match = re.search(r"([\d.]+)\s*[bB]", param_str)
    return float(match.group(1)) if match else 0.0


def _estimate_params_from_size(size_bytes: int) -> float:
    """Rough estimate of parameter count from file size (assumes ~Q4 quant)."""
    if size_bytes <= 0:
        return 0.0
    return round(size_bytes / 0.5e9, 1)


def _same_param_tier(a: float, b: float) -> bool:
    """Check whether two param counts fall in the same rough tier."""

    def _tier(p: float) -> int:
        if p <= 4:
            return 0
        if p <= 14:
            return 1
        if p <= 35:
            return 2
        return 3

    return _tier(a) == _tier(b)


def _infer_tiers(param_b: float) -> List[str]:
    """Infer hardware tiers from parameter count."""
    if param_b <= 4:
        return ["lite", "standard", "power", "workstation"]
    if param_b <= 14:
        return ["standard", "power", "workstation"]
    if param_b <= 35:
        return ["power", "workstation"]
    return ["workstation"]


def _infer_category(model_name: str) -> str:
    """Infer model category from name."""
    low = model_name.lower()
    if "embed" in low:
        return "embedding"
    if "code" in low or "coder" in low:
        return "code"
    return "general"


def _generate_display_name(
    name: str, family: str, param_b: float, param_str: str,
) -> str:
    """Generate a human-readable display name for an auto-discovered model."""
    family_title = family.replace("-", " ").title()
    size_label = param_str if param_str else f"{param_b:.0f}B"
    variant = ""
    if ":" in name:
        variant = name.split(":", 1)[1]
    if variant and variant != "latest":
        return f"{family_title} {variant} ({size_label})"
    return f"{family_title} ({size_label})"


def _persist_catalog(data: Dict[str, Any]) -> None:
    """Write catalog data to recommended_models.json atomically."""
    path = _RECOMMENDED_MODELS_PATH
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    # Atomic replace (Windows: target must be removed first)
    if os.name == "nt" and path.exists():
        path.unlink()
    tmp_path.replace(path)


# ── Main evergreen function ──────────────────────────────────────────────


async def auto_update_catalog() -> Dict[str, Any]:
    """Auto-discover installed Ollama models and update the catalog.

    Called on every startup to keep the catalog **evergreen**.  For each
    installed model *not* already in the catalog the function:

    1. Queries ``/api/show`` for metadata (family, parameter_size, quant)
    2. Infers remaining catalog fields from the model name / file size
    3. Detects supersession (newer generation replaces older in same family)
    4. Adds the entry and rebuilds upgrade paths
    5. Persists changes to ``config/recommended_models.json``

    Returns ``{"added": [...], "upgrade_paths_added": [...], "errors": []}``.
    """
    summary: Dict[str, Any] = {
        "added": [],
        "upgrade_paths_added": [],
        "errors": [],
    }

    try:
        installed_raw = await _query_ollama_models()
    except Exception:
        logger.warning("auto_update_catalog: Cannot reach Ollama — skipping")
        return summary

    if not installed_raw:
        logger.debug("auto_update_catalog: no installed models reported")
        return summary

    # ── Load current catalog ──────────────────────────────────────────
    data = _load_catalog_raw()
    catalog_models: List[Dict[str, Any]] = list(data.get("models", []))
    existing_names: set[str] = {e["model"] for e in catalog_models}

    # ── Discover unknown models ───────────────────────────────────────
    new_entries: List[Dict[str, Any]] = []

    for model_info in installed_raw:
        name = _normalize_model_name(model_info.get("name", ""))
        if not name or name in existing_names:
            continue

        # Rich metadata from Ollama
        details = await _query_model_show(name) or {}

        family = details.get("family", _infer_family_from_name(name))
        param_str = details.get("parameter_size", "")
        param_b = (
            _param_str_to_float(param_str)
            if param_str
            else _estimate_params_from_size(model_info.get("size", 0))
        )
        quant = details.get("quantization_level", "")
        size_gb = round(model_info.get("size", 0) / (1024**3), 1)
        generation = _infer_generation_from_name(name)
        category = _infer_category(name)
        tiers = _infer_tiers(param_b)

        entry: Dict[str, Any] = {
            "model": name,
            "display": _generate_display_name(name, family, param_b, param_str),
            "family": family,
            "size_gb": size_gb,
            "param_b": param_b,
            "purpose": (
                f"Auto-discovered {family} model"
                + (f" ({quant})" if quant else "")
            ),
            "category": category,
            "min_ram_gb": max(4, int(size_gb * 1.5)),
            "min_vram_gb": max(0, int(size_gb * 0.8)),
            "tiers": tiers,
            "enrichment_priority": 5 if category in ("general", "specialist") else 10,
            "generation": generation,
            "auto_discovered": True,
        }

        # ── Detect supersession (newer gen replaces older in same family)
        for existing in catalog_models:
            if (
                existing.get("family") == family
                and existing.get("generation", 0) < generation
                and _same_param_tier(existing.get("param_b", 0), param_b)
            ):
                entry["supersedes"] = existing["model"]
                break  # one supersession relationship per new model

        new_entries.append(entry)
        existing_names.add(name)  # prevent duplicates within batch
        summary["added"].append(name)

    if not new_entries:
        logger.debug("auto_update_catalog: all installed models already in catalog")
        return summary

    # ── Merge new entries ─────────────────────────────────────────────
    catalog_models.extend(new_entries)
    data["models"] = catalog_models

    # ── Rebuild upgrade paths ─────────────────────────────────────────
    upgrade_paths: Dict[str, str] = dict(data.get("upgrade_paths", {}))
    for entry in new_entries:
        if "supersedes" in entry:
            old = entry["supersedes"]
            if old not in upgrade_paths:  # don't overwrite curated paths
                upgrade_paths[old] = entry["model"]
                summary["upgrade_paths_added"].append(
                    f"{old} -> {entry['model']}"
                )
    data["upgrade_paths"] = upgrade_paths

    # ── Update timestamp ──────────────────────────────────────────────
    from datetime import date

    data["_updated"] = date.today().isoformat()

    # ── Persist ───────────────────────────────────────────────────────
    try:
        _persist_catalog(data)
        invalidate_catalog_cache()
        logger.info(
            "Evergreen catalog update: added %d model(s): %s",
            len(new_entries),
            ", ".join(summary["added"]),
        )
        if summary["upgrade_paths_added"]:
            logger.info(
                "New upgrade paths: %s",
                "; ".join(summary["upgrade_paths_added"]),
            )
    except Exception as exc:
        summary["errors"].append(str(exc))
        logger.error("Failed to persist catalog update: %s", exc)

    return summary


# ═════════════════════════════════════════════════════════════════════════════
# AUTO-PULL — PULL MISSING RECOMMENDED MODELS ON STARTUP
# ═════════════════════════════════════════════════════════════════════════════

# Subset of models that should be auto-pulled if missing.
# Only the *required* embedding model and one small general-purpose model
# per tier.  This avoids surprise 40 GB downloads on workstations.
_AUTO_PULL_REQUIRED = {"nomic-embed-text"}

# Models that are safe to auto-pull (small, < 7 GB on disk).
_AUTO_PULL_SMALL_GENERAL = {
    "phi4-mini", "gemma3:4b", "qwen3:8b", "llama4:scout",
    "phi3:mini", "gemma2:2b", "qwen2.5:7b", "llama3.1:8b", "mistral:7b",
}


async def _pull_model_quiet(model_name: str) -> bool:
    """Pull a model via Ollama CLI silently.  Returns True on success."""
    import subprocess as _sp

    try:
        from services.system_tools import SystemTools
        exe = SystemTools._ollama_executable()
        if not exe:
            return False
        SystemTools.ensure_ollama_running(exe)
    except Exception:
        exe = "ollama"

    try:
        proc = _sp.run(
            [exe, "pull", model_name],
            capture_output=True,
            timeout=_BENCHMARK_TIMEOUT_PER_MODEL,
            creationflags=_sp.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return proc.returncode == 0
    except Exception as exc:
        logger.warning("Auto-pull of %s failed: %s", model_name, exc)
        return False


async def auto_pull_recommended(tier: Optional[str] = None) -> Dict[str, Any]:
    """Pull missing recommended models appropriate for the user's hardware.

    Called on startup.  Only pulls:
    - ``nomic-embed-text`` (required for the knowledge base)
    - One small general-purpose model appropriate for the detected tier

    If at least one general-purpose model is already installed, **no
    additional general models are pulled** — the user has already made
    their choice.

    Parameters
    ----------
    tier : optional
        Hardware capability tier (e.g. "lite", "standard").  If None,
        the function will attempt to detect it.

    Returns ``{"pulled": [...], "skipped": [...], "errors": [...]}``.
    """
    result: Dict[str, Any] = {"pulled": [], "skipped": [], "errors": []}

    # ── Determine hardware tier if not provided ───────────────────────
    if tier is None:
        try:
            from services.device_capability import DeviceCapabilityScanner
            scanner = DeviceCapabilityScanner()
            report = await scanner.scan()
            tier = report.profile.tier if report and report.profile else "standard"
        except Exception:
            tier = "standard"

    # ── Get installed models ──────────────────────────────────────────
    installed = {_normalize_model_name(m) for m in await get_installed_model_names()}

    # ── Pull required models (embedding) ──────────────────────────────
    for req in _AUTO_PULL_REQUIRED:
        if req in installed:
            continue
        logger.info("Auto-pulling required model: %s", req)
        ok = await _pull_model_quiet(req)
        if ok:
            result["pulled"].append(req)
            installed.add(req)
            logger.info("Successfully auto-pulled %s", req)
        else:
            result["errors"].append(f"Failed to pull required model {req}")

    # ── Check if user already has a general-purpose model ─────────────
    catalog = get_model_catalog()
    general_in_catalog = {
        e["model"] for e in catalog
        if e.get("category") == "general" and tier in e.get("tiers", [])
    }
    has_general = bool(installed & general_in_catalog)

    # Also check if any non-catalog general model is installed
    if not has_general:
        non_embed_installed = {m for m in installed if "embed" not in m.lower()}
        has_general = bool(non_embed_installed - _AUTO_PULL_REQUIRED)

    if has_general:
        logger.debug("auto_pull: user already has general model(s) — skipping auto-pull of general models")
        return result

    # ── Pick the best small general model for this tier ────────────────
    # Prefer models from the catalog that match the tier, sorted by
    # enrichment priority (lower = better).
    candidates = [
        e for e in catalog
        if e.get("category") in ("general", "specialist")
        and tier in e.get("tiers", [])
        and e["model"] in _AUTO_PULL_SMALL_GENERAL
        and e["model"] not in installed
    ]
    candidates.sort(key=lambda e: e.get("enrichment_priority", 99))

    if not candidates:
        logger.debug("auto_pull: no candidates to pull for tier %s", tier)
        return result

    # Pull just the top candidate to get the user started quickly
    best = candidates[0]
    model_name = best["model"]
    logger.info("Auto-pulling recommended model for tier '%s': %s (%.1f GB)", tier, model_name, best.get("size_gb", 0))
    ok = await _pull_model_quiet(model_name)
    if ok:
        result["pulled"].append(model_name)
        logger.info("Successfully auto-pulled %s", model_name)
    else:
        result["errors"].append(f"Failed to pull {model_name}")
        # Try the next candidate as fallback
        if len(candidates) > 1:
            fallback = candidates[1]
            logger.info("Trying fallback: %s", fallback["model"])
            ok2 = await _pull_model_quiet(fallback["model"])
            if ok2:
                result["pulled"].append(fallback["model"])
            else:
                result["errors"].append(f"Fallback {fallback['model']} also failed")

    return result


async def startup_model_refresh(tier: Optional[str] = None) -> Dict[str, Any]:
    """Single entry point for startup model management.

    Combines catalog update + auto-pull in one call.  This is what the
    startup hooks call.

    1. ``auto_update_catalog()`` — discover installed models, update JSON
    2. ``auto_pull_recommended(tier)`` — pull missing essentials

    Returns combined results of both operations.
    """
    catalog_result = await auto_update_catalog()
    pull_result = await auto_pull_recommended(tier)
    return {
        "catalog_update": catalog_result,
        "auto_pull": pull_result,
    }
