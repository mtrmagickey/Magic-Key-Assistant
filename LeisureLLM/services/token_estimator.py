"""
Token Estimation & Context Truncation Monitor

Provides lightweight token-count estimates for local models and logs
warnings when prompt + context approaches or exceeds the configured
context window — catching silent truncation before it degrades answers.

The estimator uses a character-ratio heuristic (accurate within ~10%
for English text) rather than requiring a tokeniser dependency.  This
is intentional: the goal is *monitoring*, not exact counts.

Usage
-----
    from services.token_estimator import TokenEstimator

    estimator = TokenEstimator()
    estimate = estimator.estimate_messages(messages, model="qwen3:8b")
    if estimate.exceeds_context:
        logger.warning("Context will be truncated: %s", estimate)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# TOKEN ESTIMATION HEURISTICS
# ═════════════════════════════════════════════════════════════════════════════

# Characters-per-token ratios by model family.  These are empirically
# measured averages for English text.  CJK text uses ~1.5 chars/token,
# code uses ~3.5 chars/token.  We use a conservative (lower) ratio to
# err on the side of *overestimating* token counts — it's better to
# warn about potential truncation than to miss it.
_CHARS_PER_TOKEN: Dict[str, float] = {
    "qwen": 3.8,
    "llama": 4.0,
    "phi": 3.8,
    "gemma": 3.9,
    "mistral": 3.9,
    "deepseek": 3.8,
    "nomic": 4.0,
    # Default for unknown models
    "_default": 3.8,
}

# Known context windows by model pattern.  When Ollama's num_ctx is
# not explicitly set, this is what the model actually supports.
_MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "qwen3.5": 131072,
    "qwen3": 32768,
    "qwen2.5": 32768,
    "llama4": 131072,
    "llama3.3": 131072,
    "llama3.1": 131072,
    "phi4": 16384,
    "phi3": 4096,
    "gemma3": 8192,
    "gemma2": 8192,
    "mistral-small": 32768,
    "mistral:7b": 8192,
    "deepseek-r1": 65536,
    "nomic-embed": 8192,
}


@dataclass
class TokenEstimate:
    """Result of a token estimation."""
    input_tokens: int = 0           # Estimated input/prompt tokens
    max_output_tokens: int = 0      # Configured max_tokens for generation
    total_tokens: int = 0           # input + max_output
    context_window: int = 8192      # Effective context window (num_ctx or model default)
    headroom_tokens: int = 0        # context_window - total_tokens
    exceeds_context: bool = False   # True if total_tokens > context_window
    truncation_risk: str = "none"   # "none" | "low" | "medium" | "high" | "certain"
    model: str = ""
    backend: str = ""

    # Breakdown
    system_tokens: int = 0
    user_tokens: int = 0
    context_tokens: int = 0         # RAG context portion
    history_tokens: int = 0         # Conversation history portion

    def __str__(self) -> str:
        status = "⚠️ TRUNCATION" if self.exceeds_context else "✓ OK"
        return (
            f"TokenEstimate({status}: {self.input_tokens:,} input + "
            f"{self.max_output_tokens:,} output = {self.total_tokens:,} / "
            f"{self.context_window:,} ctx | risk={self.truncation_risk} "
            f"model={self.model})"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "total_tokens": self.total_tokens,
            "context_window": self.context_window,
            "headroom_tokens": self.headroom_tokens,
            "exceeds_context": self.exceeds_context,
            "truncation_risk": self.truncation_risk,
            "model": self.model,
            "backend": self.backend,
            "breakdown": {
                "system": self.system_tokens,
                "user": self.user_tokens,
                "context": self.context_tokens,
                "history": self.history_tokens,
            },
        }


@dataclass
class TruncationEvent:
    """A logged truncation event for diagnostics."""
    timestamp: float
    model: str
    backend: str
    input_tokens: int
    context_window: int
    overflow_tokens: int
    truncation_risk: str
    pipeline_role: str = ""
    query_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "backend": self.backend,
            "input_tokens": self.input_tokens,
            "context_window": self.context_window,
            "overflow_tokens": self.overflow_tokens,
            "truncation_risk": self.truncation_risk,
            "pipeline_role": self.pipeline_role,
            "query_preview": self.query_preview,
        }


class TokenEstimator:
    """Lightweight token estimator for monitoring context truncation.

    Does NOT require any tokeniser library — uses character-ratio
    heuristics that are accurate within ~10% for English text.
    """

    # Rolling buffer of recent truncation events for the admin UI
    MAX_EVENTS = 200

    def __init__(self) -> None:
        self._events: List[TruncationEvent] = []
        self._stats = {
            "estimates_total": 0,
            "truncations_detected": 0,
            "truncations_high_risk": 0,
        }

    def estimate_tokens(self, text: str, model: str = "") -> int:
        """Estimate token count for a text string."""
        if not text:
            return 0
        family = self._model_family(model)
        chars_per_tok = _CHARS_PER_TOKEN.get(family, _CHARS_PER_TOKEN["_default"])
        # Add overhead for special tokens, message framing, etc.
        raw = len(text) / chars_per_tok
        # Each message adds ~4 tokens of framing (role, delimiters)
        return int(raw)

    def estimate_messages(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        max_tokens: int = 4000,
        num_ctx: int = 0,
        backend: str = "ollama",
    ) -> TokenEstimate:
        """Estimate token usage for a message list against a context window.

        Args:
            messages: Chat messages [{"role": ..., "content": ...}, ...]
            model: Model name for family-specific ratio
            max_tokens: Configured max output tokens (num_predict)
            num_ctx: Explicit context window. If 0, uses model's known default.
            backend: Backend name for logging
        """
        self._stats["estimates_total"] += 1

        # Determine effective context window
        context_window = num_ctx or self._infer_context_window(model)

        # Count tokens per role
        system_tokens = 0
        user_tokens = 0
        history_tokens = 0
        total_input = 0

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            tokens = self.estimate_tokens(content, model) + 4  # framing overhead
            total_input += tokens

            if role == "system":
                system_tokens += tokens
            elif role == "user":
                user_tokens += tokens
            else:
                history_tokens += tokens

        # Detect RAG context within user messages (heuristic: "Context:\n" block)
        context_tokens = 0
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                # Look for context injection patterns
                for marker in ("Context:\n", "=== SOURCE DOCUMENTS", "[Conversation History]"):
                    idx = content.find(marker)
                    if idx >= 0:
                        context_text = content[idx:]
                        context_tokens += self.estimate_tokens(context_text, model)
                        break

        total = total_input + max_tokens
        headroom = context_window - total

        # Determine risk level
        if total > context_window:
            risk = "certain"
        elif headroom < 200:
            risk = "high"
        elif headroom < context_window * 0.15:
            risk = "medium"
        elif headroom < context_window * 0.30:
            risk = "low"
        else:
            risk = "none"

        exceeds = total > context_window

        estimate = TokenEstimate(
            input_tokens=total_input,
            max_output_tokens=max_tokens,
            total_tokens=total,
            context_window=context_window,
            headroom_tokens=max(0, headroom),
            exceeds_context=exceeds,
            truncation_risk=risk,
            model=model,
            backend=backend,
            system_tokens=system_tokens,
            user_tokens=user_tokens,
            context_tokens=context_tokens,
            history_tokens=history_tokens,
        )

        # Log warnings for dangerous situations
        if risk in ("high", "certain"):
            self._stats["truncations_detected"] += 1
            if risk == "certain":
                self._stats["truncations_high_risk"] += 1
                logger.warning(
                    "🚨 Context WILL be truncated: %s (overflow by %d tokens)",
                    estimate, abs(headroom),
                )
            else:
                logger.warning(
                    "⚠️ Context truncation risk HIGH: %s (only %d tokens headroom)",
                    estimate, headroom,
                )

            # Record event
            query_preview = ""
            for msg in messages:
                if msg.get("role") == "user":
                    query_preview = msg.get("content", "")[:100]
                    break

            event = TruncationEvent(
                timestamp=time.time(),
                model=model,
                backend=backend,
                input_tokens=total_input,
                context_window=context_window,
                overflow_tokens=abs(headroom) if exceeds else 0,
                truncation_risk=risk,
                query_preview=query_preview,
            )
            self._events.append(event)
            if len(self._events) > self.MAX_EVENTS:
                self._events = self._events[-self.MAX_EVENTS:]

        elif risk == "medium":
            logger.info(
                "Token usage medium-risk: %d / %d (%.0f%% used) model=%s",
                total, context_window, (total / context_window * 100), model,
            )

        return estimate

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent truncation events for the admin UI."""
        return [e.to_dict() for e in self._events[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        """Return aggregate statistics."""
        return dict(self._stats)

    def _model_family(self, model: str) -> str:
        """Extract family name from model string."""
        model_lower = model.lower()
        for family in _CHARS_PER_TOKEN:
            if family != "_default" and family in model_lower:
                return family
        return "_default"

    def _infer_context_window(self, model: str) -> int:
        """Infer context window from model name."""
        model_lower = model.lower()
        for pattern, ctx in _MODEL_CONTEXT_WINDOWS.items():
            if pattern.lower() in model_lower:
                return ctx
        # Conservative default — Ollama's actual default is 2048 unless
        # num_ctx is explicitly set.  We use 8192 because MKA always
        # sets num_ctx=8192 in its default ollama_options.
        return 8192


# ═════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ═════════════════════════════════════════════════════════════════════════════

_instance: Optional[TokenEstimator] = None


def get_token_estimator() -> TokenEstimator:
    """Get the global TokenEstimator singleton."""
    global _instance
    if _instance is None:
        _instance = TokenEstimator()
    return _instance
