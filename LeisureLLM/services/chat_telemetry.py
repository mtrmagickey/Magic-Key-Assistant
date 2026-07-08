from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ChatStageFlags:
    hyde_used: bool = False
    critique_used: bool = False
    synth_used: bool = False
    rerank_used: bool = False
    web_used: bool = False


@dataclass
class ChatLatencyMetrics:
    retrieval_ms: Optional[int] = None
    generation_ms: Optional[int] = None
    first_token_ms: Optional[int] = None
    total_ms: Optional[int] = None


@dataclass
class ChatTokenMetrics:
    input_tokens_est: int = 0
    output_tokens_est: int = 0
    total_tokens_est: int = 0


@dataclass
class ChatPolicyMetrics:
    complexity: str = "unknown"
    reason: str = ""
    max_aux_retrieval_calls: int = 0
    aux_retrieval_calls: int = 0
    max_generation_stages: int = 0


@dataclass
class ChatRequestTelemetry:
    query: str
    started_at: float = field(default_factory=time.time)
    retrieval_started_at: float = field(init=False)
    generation_started_at: Optional[float] = None
    route_mode: str = "unknown"
    cache_hit: bool = False
    llm_calls: int = 0
    retrieved_docs: int = 0
    context_words: int = 0
    stage_flags: ChatStageFlags = field(default_factory=ChatStageFlags)
    latency: ChatLatencyMetrics = field(default_factory=ChatLatencyMetrics)
    tokens: ChatTokenMetrics = field(default_factory=ChatTokenMetrics)
    policy: ChatPolicyMetrics = field(default_factory=ChatPolicyMetrics)

    def __post_init__(self) -> None:
        self.retrieval_started_at = self.started_at

    def finish_retrieval(self) -> None:
        if self.latency.retrieval_ms is None:
            self.latency.retrieval_ms = int((time.time() - self.retrieval_started_at) * 1000)

    def start_generation(self) -> None:
        if self.generation_started_at is None:
            self.finish_retrieval()
            self.generation_started_at = time.time()

    def note_first_token(self) -> None:
        if self.latency.first_token_ms is None:
            self.latency.first_token_ms = int((time.time() - self.started_at) * 1000)

    def finish(self) -> None:
        now = time.time()
        self.finish_retrieval()
        if self.generation_started_at is not None and self.latency.generation_ms is None:
            self.latency.generation_ms = int((now - self.generation_started_at) * 1000)
        self.latency.total_ms = int((now - self.started_at) * 1000)

    def mark_stage(self, *, hyde: bool = False, critique: bool = False, synth: bool = False, rerank: bool = False, web: bool = False) -> None:
        if hyde:
            self.stage_flags.hyde_used = True
        if critique:
            self.stage_flags.critique_used = True
        if synth:
            self.stage_flags.synth_used = True
        if rerank:
            self.stage_flags.rerank_used = True
        if web:
            self.stage_flags.web_used = True

    def update_from_pipeline_result(self, result: Optional[Dict[str, Any]]) -> None:
        stages = (result or {}).get("stages") or {}
        if stages.get("critique"):
            self.stage_flags.critique_used = True
        if stages.get("synthesize"):
            self.stage_flags.synth_used = True

    def estimate_tokens(
        self,
        *,
        model: str,
        system_prompt: str = "",
        context: str = "",
        user_prompt: str = "",
        history: str = "",
        reply_text: str = "",
    ) -> None:
        try:
            from services.token_estimator import get_token_estimator

            estimator = get_token_estimator()
            combined_input = "\n\n".join(
                part for part in (system_prompt, history, context, user_prompt) if part
            )
            self.tokens.input_tokens_est = estimator.estimate_tokens(combined_input, model)
            self.tokens.output_tokens_est = estimator.estimate_tokens(reply_text, model)
            self.tokens.total_tokens_est = (
                self.tokens.input_tokens_est + self.tokens.output_tokens_est
            )
        except Exception:
            # Monitoring must remain best-effort.
            self.tokens = ChatTokenMetrics()

    def to_log_payload(self) -> Dict[str, Any]:
        self.finish()
        return {
            "cache_hit": self.cache_hit,
            "route_mode": self.route_mode,
            "llm_calls": self.llm_calls,
            "retrieved_docs": self.retrieved_docs,
            "context_words": self.context_words,
            "stage_flags": asdict(self.stage_flags),
            "latency": asdict(self.latency),
            "tokens": asdict(self.tokens),
            "policy": asdict(self.policy),
        }

    def to_trace_summary(self) -> Dict[str, Any]:
        return self.to_log_payload()


def build_local_only_blocked_reply(reason: str = "") -> str:
    suffix = f" Reason: {reason}." if reason else ""
    return (
        "Local-only mode is enabled, and this request could not be completed without a cloud model. "
        "Assign a local model to the active chat pipeline or disable LOCAL_LLM_ONLY to allow cloud routing."
        f"{suffix}"
    )