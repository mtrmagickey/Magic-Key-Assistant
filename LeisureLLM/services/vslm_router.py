"""
VSLM Router — Very Small Language Model-based query classification.

A lightweight model (≤4B params) classifies each incoming query by
complexity *before* the expensive synthesis pipeline runs.  Based on the
classification, the pipeline preset is automatically selected:

    simple   → Speed preset  (single fast model, phases 2-3 disabled)
    moderate → Balanced preset (capable model, optional critique)
    complex  → Quality preset (best model, full 3-phase pipeline)

This eliminates manual per-role model configuration for the vast majority
of users and directly addresses the "automatic model selection" goal from
the deep-research brief (Q17).

The classification prompt is carefully tuned:
- Deterministic (temperature 0, max_tokens ~20, single-label output)
- Runs in <200ms on even modest hardware (2B-4B model)
- Zero-shot — no fine-tuning needed

Usage
-----
    from services.vslm_router import get_vslm_router, QueryComplexity

    vslm = get_vslm_router()
    complexity = await vslm.classify(user_prompt, context_snippet)
    # → QueryComplexity.SIMPLE / .MODERATE / .COMPLEX
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Data Types ──────────────────────────────────────────────────────────────

class QueryComplexity(str, Enum):
    """Complexity tiers — each maps to a pipeline preset."""
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


# Preset mapping: complexity → preset name (from recommended_models.json)
COMPLEXITY_TO_PRESET = {
    QueryComplexity.SIMPLE: "speed",
    QueryComplexity.MODERATE: "balanced",
    QueryComplexity.COMPLEX: "quality",
}


@dataclass
class ClassificationResult:
    """Result of a VSLM classification."""
    complexity: QueryComplexity
    confidence: float  # 0.0 – 1.0 estimated confidence
    latency_ms: float  # wall-clock time for the classification call
    model_used: str
    raw_output: str  # the raw model output (for debugging)
    preset_name: str  # resolved preset name


@dataclass
class VSLMRouterStats:
    """Running stats for the VSLM router."""
    total_classifications: int = 0
    simple_count: int = 0
    moderate_count: int = 0
    complex_count: int = 0
    fallback_count: int = 0  # times classification failed → fell back to balanced
    avg_latency_ms: float = 0.0
    _latency_sum: float = field(default=0.0, repr=False)


# ── VSLM Router ─────────────────────────────────────────────────────────────

# Classification prompt — designed for deterministic single-label output
_CLASSIFICATION_PROMPT = """Classify the following user query into exactly ONE complexity category.

Categories:
- SIMPLE: Factual lookups, yes/no questions, definitions, single-fact retrieval. Example: "What time does the pool open?"
- MODERATE: Questions needing synthesis of 2-3 facts, comparisons, or short explanations. Example: "Compare the adult and junior membership prices."
- COMPLEX: Multi-step reasoning, analysis, planning, policy interpretation, or questions spanning multiple topics. Example: "Draft a proposal for restructuring our membership tiers based on current utilisation data."

User query: {query}

Respond with ONLY the category name (SIMPLE, MODERATE, or COMPLEX). Nothing else."""


# Models to try for VSLM classification, in preference order.
# These MUST be small (≤4B active params) to keep classification fast.
_VSLM_PREFERENCE = [
    "qwen3:4b",
    "qwen3.5:4b",  # compact Qwen 3.5 — fast classification
    "phi4-mini",
    "phi3:mini",
    "gemma3:4b",
    "gemma2:2b",
    "llama3.2",
    "llama3.2:1b",
    "qwen2.5:3b",
    "qwen2.5:1.5b",
    "tinyllama",
]


class VSLMRouter:
    """Classifies queries by complexity using a very small LM."""

    def __init__(self) -> None:
        self._enabled: bool = True
        self._model: Optional[str] = None  # resolved at first use
        self._backend_name: str = "ollama"  # default to Ollama
        self._stats = VSLMRouterStats()
        self._lock = asyncio.Lock()
        self._resolved = False

    # ── Configuration ────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, val: bool) -> None:
        self._enabled = val

    @property
    def stats(self) -> VSLMRouterStats:
        return self._stats

    def configure(
        self,
        model: Optional[str] = None,
        backend_name: str = "ollama",
        enabled: bool = True,
    ) -> None:
        """Manually configure the VSLM router."""
        self._model = model
        self._backend_name = backend_name
        self._enabled = enabled
        self._resolved = bool(model)

    async def _resolve_model(self) -> Optional[str]:
        """Auto-detect the best installed VSLM model."""
        if self._resolved:
            return self._model

        async with self._lock:
            if self._resolved:
                return self._model

            try:
                from services.pipeline_presets import get_installed_model_set
                installed = await get_installed_model_set()

                for candidate in _VSLM_PREFERENCE:
                    if candidate in installed:
                        self._model = candidate
                        self._resolved = True
                        logger.info("VSLM router auto-selected model: %s", candidate)
                        return candidate

                    # Partial match (e.g., "gemma2:2b" matches "gemma2:2b-instruct-q4_0")
                    base = candidate.split(":")[0]
                    for inst in installed:
                        if inst.startswith(base):
                            self._model = inst
                            self._resolved = True
                            logger.info("VSLM router auto-selected model: %s (matched %s)", inst, candidate)
                            return inst

                # Also check llama.cpp models
                try:
                    from services.llamacpp_manager import get_llamacpp_manager
                    lcpp = get_llamacpp_manager()
                    lcpp_status = lcpp.get_status()
                    if lcpp_status.running and lcpp_status.available_models:
                        # Any small GGUF will do — llamacpp backend
                        for m in lcpp_status.available_models:
                            self._model = m
                            self._backend_name = "llamacpp"
                            self._resolved = True
                            logger.info("VSLM router using llama.cpp model: %s", m)
                            return m
                except Exception as e:
                    logger.warning("operation: suppressed %s", e)

                logger.info("VSLM router: no suitable small model found — auto-routing disabled")
                self._resolved = True  # don't retry
                return None
            except Exception as e:
                logger.warning("VSLM router model resolution failed: %s", e)
                return None

    # ── Classification ───────────────────────────────────────────

    async def classify(
        self,
        user_prompt: str,
        context_snippet: str = "",
    ) -> ClassificationResult:
        """Classify a query's complexity.

        If VSLM is disabled or no model is available, falls back to
        MODERATE (balanced preset) — a safe default.
        """
        if not self._enabled:
            return self._make_fallback("disabled")

        model = await self._resolve_model()
        if not model:
            return self._make_fallback("no_model")

        # Build the classification prompt
        # Include a snippet of context to help the model judge complexity
        query_text = user_prompt
        if context_snippet:
            # Limit context snippet to avoid overwhelming the tiny model
            snippet = context_snippet[:500]
            query_text = f"{user_prompt}\n\n[Available context: {snippet}...]"

        prompt = _CLASSIFICATION_PROMPT.format(query=query_text)

        t0 = time.perf_counter()
        try:
            from services.model_router import get_model_router

            mr = get_model_router()
            if not mr:
                return self._make_fallback("no_router")

            raw = await mr.generate_single(
                backend_name=self._backend_name,
                model=model,
                prompt=prompt,
                temperature=0.0,
                max_tokens=20,
                ollama_options={"num_ctx": 2048},  # small context is fine
            )
            latency_ms = (time.perf_counter() - t0) * 1000

            complexity = self._parse_output(raw)
            confidence = 0.95 if complexity else 0.0

            if complexity is None:
                # Couldn't parse → fall back to MODERATE
                logger.debug("VSLM output unparseable: %r → defaulting to MODERATE", raw[:100])
                complexity = QueryComplexity.MODERATE
                confidence = 0.3
                self._stats.fallback_count += 1

            # Update stats
            self._update_stats(complexity, latency_ms)

            return ClassificationResult(
                complexity=complexity,
                confidence=confidence,
                latency_ms=round(latency_ms, 1),
                model_used=model,
                raw_output=raw[:200],
                preset_name=COMPLEXITY_TO_PRESET[complexity],
            )

        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000
            logger.warning("VSLM classification error (%.0fms): %s", latency_ms, e)
            return self._make_fallback("error", latency_ms=latency_ms)

    def _parse_output(self, raw: str) -> Optional[QueryComplexity]:
        """Parse the model's classification output."""
        if not raw:
            return None

        text = raw.strip().upper()

        # Direct match
        if text in ("SIMPLE", "MODERATE", "COMPLEX"):
            return QueryComplexity(text.lower())

        # Extract from surrounding text (e.g., "The query is SIMPLE.")
        for label in ("COMPLEX", "MODERATE", "SIMPLE"):
            if label in text:
                return QueryComplexity(label.lower())

        # Regex fallback — look for the word anywhere
        m = re.search(r'\b(SIMPLE|MODERATE|COMPLEX)\b', text)
        if m:
            return QueryComplexity(m.group(1).lower())

        return None

    def _make_fallback(
        self, reason: str, latency_ms: float = 0.0
    ) -> ClassificationResult:
        """Create a fallback MODERATE classification."""
        self._stats.fallback_count += 1
        return ClassificationResult(
            complexity=QueryComplexity.MODERATE,
            confidence=0.0,
            latency_ms=round(latency_ms, 1),
            model_used=f"fallback:{reason}",
            raw_output="",
            preset_name="balanced",
        )

    def _update_stats(self, complexity: QueryComplexity, latency_ms: float) -> None:
        """Update running stats."""
        self._stats.total_classifications += 1
        if complexity == QueryComplexity.SIMPLE:
            self._stats.simple_count += 1
        elif complexity == QueryComplexity.MODERATE:
            self._stats.moderate_count += 1
        else:
            self._stats.complex_count += 1

        self._stats._latency_sum += latency_ms
        self._stats.avg_latency_ms = round(
            self._stats._latency_sum / self._stats.total_classifications, 1
        )

    # ── Introspection ────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return current router status for admin API."""
        return {
            "enabled": self._enabled,
            "model": self._model,
            "backend": self._backend_name,
            "resolved": self._resolved,
            "stats": {
                "total": self._stats.total_classifications,
                "simple": self._stats.simple_count,
                "moderate": self._stats.moderate_count,
                "complex": self._stats.complex_count,
                "fallback": self._stats.fallback_count,
                "avg_latency_ms": self._stats.avg_latency_ms,
            },
        }


# ── Singleton ────────────────────────────────────────────────────────────────

_instance: Optional[VSLMRouter] = None


def get_vslm_router() -> VSLMRouter:
    """Get or create the global VSLM router singleton."""
    global _instance
    if _instance is None:
        _instance = VSLMRouter()
    return _instance
