from __future__ import annotations

from dataclasses import dataclass

_DEEP_INTENT_TERMS = {
    "analyze",
    "analysis",
    "compare",
    "comparison",
    "diagnose",
    "diagnosis",
    "strategy",
    "strategic",
    "tradeoff",
    "trade-offs",
    "tradeoffs",
    "options",
    "proposal",
    "propose",
    "plan",
    "root cause",
}

_DEEP_INTENT_PHRASES = (
    "what are the tradeoffs",
    "compare the options",
    "diagnose why",
    "root cause",
    "go deep",
    "deep dive",
    "analyze this",
    "compare this",
)

_ARTIFACT_TERMS = {
    "action item",
    "proposal",
    "plan",
    "memo",
    "brief",
    "report",
    "strategy",
    "outline",
    "spec",
    "decision record",
}

_ARTIFACT_VERBS = {
    "draft",
    "write",
    "prepare",
    "generate",
    "create",
    "build",
}

_MULTI_SOURCE_PHRASES = (
    "compare",
    "cross-reference",
    "cross reference",
    "across the sources",
    "from the sources",
    "based on the docs",
    "based on the documents",
    "with citations",
    "cite sources",
    "show sources",
    "evidence",
)

_HIGH_CONFIDENCE_TERMS = {
    "exact",
    "policy",
    "pricing",
    "price",
    "contract",
    "compliance",
    "deadline",
    "due",
    "owner",
    "decision",
    "source",
    "citation",
}

_EXPLICIT_DEEP_MODE_PHRASES = (
    "deep mode",
    "use deep mode",
    "deep answer",
    "deep analysis",
    "deep dive",
    "/deep",
)


@dataclass
class ChatPolicyDecision:
    lane: str
    complexity: str
    explicit_deep_intent: bool
    artifact_request: bool
    multi_source_requirement: bool
    high_confidence_required: bool
    deep_mode_requested: bool
    use_query_decomposition: bool
    use_corrective_retrieval: bool
    use_evidence_evaluation: bool
    max_aux_retrieval_calls: int
    max_generation_stages: int
    reason: str

    def escalated_to_deep(
        self,
        *,
        max_aux_retrieval_calls: int,
        max_generation_stages: int,
        reason: str,
    ) -> "ChatPolicyDecision":
        return ChatPolicyDecision(
            lane="deep",
            complexity="deep",
            explicit_deep_intent=self.explicit_deep_intent,
            artifact_request=self.artifact_request,
            multi_source_requirement=self.multi_source_requirement,
            high_confidence_required=self.high_confidence_required,
            deep_mode_requested=self.deep_mode_requested,
            use_query_decomposition=True,
            use_corrective_retrieval=True,
            use_evidence_evaluation=True,
            max_aux_retrieval_calls=max(0, max_aux_retrieval_calls),
            max_generation_stages=max(1, max_generation_stages),
            reason=reason,
        )


class AuxCallBudget:
    """Tracks best-effort budget usage for retrieval-side LLM calls."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max(0, max_calls)
        self.calls_used = 0

    def try_consume(self, _stage_name: str) -> bool:
        if self.calls_used >= self.max_calls:
            return False
        self.calls_used += 1
        return True


def decide_chat_policy(
    query: str,
    *,
    adaptive_enabled: bool,
    max_aux_retrieval_calls: int,
    max_generation_stages: int,
    deep_mode: bool = False,
    simple_query_word_threshold: int,
) -> ChatPolicyDecision:
    words = [w for w in (query or "").lower().split() if w]
    word_count = len(words)
    low = (query or "").lower()

    explicit_deep_intent = any(term in low for term in _DEEP_INTENT_TERMS) or any(
        phrase in low for phrase in _DEEP_INTENT_PHRASES
    )
    artifact_request = any(verb in words for verb in _ARTIFACT_VERBS) and any(
        term in low for term in _ARTIFACT_TERMS
    )
    multi_source_requirement = any(phrase in low for phrase in _MULTI_SOURCE_PHRASES)
    deep_mode_requested = deep_mode or any(phrase in low for phrase in _EXPLICIT_DEEP_MODE_PHRASES)
    high_confidence_required = (
        artifact_request
        or multi_source_requirement
        or explicit_deep_intent
        or any(term in words for term in _HIGH_CONFIDENCE_TERMS)
    )
    short_query_prior = word_count <= simple_query_word_threshold

    if not adaptive_enabled:
        return ChatPolicyDecision(
            lane="deep",
            complexity="adaptive-disabled",
            explicit_deep_intent=explicit_deep_intent,
            artifact_request=artifact_request,
            multi_source_requirement=multi_source_requirement,
            high_confidence_required=high_confidence_required,
            deep_mode_requested=deep_mode_requested,
            use_query_decomposition=True,
            use_corrective_retrieval=True,
            use_evidence_evaluation=True,
            max_aux_retrieval_calls=max(0, max_aux_retrieval_calls),
            max_generation_stages=max(1, max_generation_stages),
            reason="adaptive gating disabled",
        )

    if deep_mode_requested:
        return ChatPolicyDecision(
            lane="deep",
            complexity="deep",
            explicit_deep_intent=explicit_deep_intent,
            artifact_request=artifact_request,
            multi_source_requirement=multi_source_requirement,
            high_confidence_required=True,
            deep_mode_requested=True,
            use_query_decomposition=True,
            use_corrective_retrieval=True,
            use_evidence_evaluation=True,
            max_aux_retrieval_calls=max(0, max_aux_retrieval_calls),
            max_generation_stages=max(1, max_generation_stages),
            reason="user explicitly requested deep mode",
        )

    if explicit_deep_intent:
        return ChatPolicyDecision(
            lane="deep",
            complexity="deep",
            explicit_deep_intent=True,
            artifact_request=artifact_request,
            multi_source_requirement=multi_source_requirement,
            high_confidence_required=True,
            deep_mode_requested=False,
            use_query_decomposition=True,
            use_corrective_retrieval=True,
            use_evidence_evaluation=True,
            max_aux_retrieval_calls=max(0, max_aux_retrieval_calls),
            max_generation_stages=max(1, max_generation_stages),
            reason="explicit deep-analysis intent detected",
        )

    if artifact_request:
        return ChatPolicyDecision(
            lane="deep",
            complexity="deep",
            explicit_deep_intent=explicit_deep_intent,
            artifact_request=True,
            multi_source_requirement=multi_source_requirement,
            high_confidence_required=True,
            deep_mode_requested=False,
            use_query_decomposition=True,
            use_corrective_retrieval=True,
            use_evidence_evaluation=True,
            max_aux_retrieval_calls=max(0, max_aux_retrieval_calls),
            max_generation_stages=max(1, max_generation_stages),
            reason="structured artifact request detected",
        )

    if multi_source_requirement:
        return ChatPolicyDecision(
            lane="deep",
            complexity="deep",
            explicit_deep_intent=explicit_deep_intent,
            artifact_request=artifact_request,
            multi_source_requirement=True,
            high_confidence_required=True,
            deep_mode_requested=False,
            use_query_decomposition=True,
            use_corrective_retrieval=True,
            use_evidence_evaluation=True,
            max_aux_retrieval_calls=max(0, max_aux_retrieval_calls),
            max_generation_stages=max(1, max_generation_stages),
            reason="multi-source or citation-heavy answer required",
        )

    if short_query_prior:
        return ChatPolicyDecision(
            lane="assistive",
            complexity="assistive",
            explicit_deep_intent=explicit_deep_intent,
            artifact_request=artifact_request,
            multi_source_requirement=multi_source_requirement,
            high_confidence_required=high_confidence_required,
            deep_mode_requested=deep_mode_requested,
            use_query_decomposition=False,
            use_corrective_retrieval=False,
            use_evidence_evaluation=False,
            max_aux_retrieval_calls=1,
            max_generation_stages=1,
            reason="default assistive lane (short-query prior, no deep signals)",
        )

    return ChatPolicyDecision(
        lane="assistive",
        complexity="assistive",
        explicit_deep_intent=explicit_deep_intent,
        artifact_request=artifact_request,
        multi_source_requirement=multi_source_requirement,
        high_confidence_required=high_confidence_required,
        deep_mode_requested=deep_mode_requested,
        use_query_decomposition=False,
        use_corrective_retrieval=False,
        use_evidence_evaluation=False,
        max_aux_retrieval_calls=1,
        max_generation_stages=1,
        reason="default assistive lane (no explicit deep, artifact, or multi-source signals)",
    )


def should_escalate_after_retrieval(
    policy: ChatPolicyDecision,
    *,
    retrieval_doc_count: int,
    context_words: int,
    relevance_score: float,
) -> tuple[bool, str]:
    if policy.lane == "deep":
        return False, "already deep"

    weak_retrieval = (
        retrieval_doc_count < 3
        or context_words < 120
        or relevance_score < 0.2
    )
    if not weak_retrieval:
        return False, "retrieval sufficient"
    if not policy.high_confidence_required:
        return False, "weak retrieval but request does not require high-confidence deep escalation"

    if retrieval_doc_count < 3:
        return True, "escalated to deep: weak retrieval doc count for high-confidence answer"
    if context_words < 120:
        return True, "escalated to deep: sparse context for high-confidence answer"
    return True, "escalated to deep: weak relevance for high-confidence answer"


async def async_enhance_policy(
    policy: ChatPolicyDecision,
    query: str,
) -> ChatPolicyDecision:
    """Optionally enhance a heuristic policy decision using the VSLM router.

    If the heuristic decided ``assistive`` but VSLM classifies the query as
    COMPLEX with reasonable confidence, escalate to the ``deep`` lane so
    corrective retrieval, query decomposition, and evidence evaluation are
    enabled.

    Returns the original policy unchanged when:
    - the VSLM router is unavailable or disabled
    - the policy is already ``deep``
    - VSLM agrees the query is simple/moderate
    """
    if policy.lane == "deep":
        return policy

    try:
        from services.vslm_router import QueryComplexity, get_vslm_router

        vslm = get_vslm_router()
        if not vslm.enabled:
            return policy

        classification = await vslm.classify(query)
        if (
            classification.complexity == QueryComplexity.COMPLEX
            and classification.confidence >= 0.5
        ):
            return policy.escalated_to_deep(
                max_aux_retrieval_calls=policy.max_aux_retrieval_calls,
                max_generation_stages=policy.max_generation_stages,
                reason=f"VSLM escalated: heuristic={policy.lane}, "
                       f"vslm={classification.complexity.value} "
                       f"(confidence={classification.confidence:.2f}, "
                       f"model={classification.model_used})",
            )
    except Exception:
        pass  # VSLM is best-effort; fall through to heuristic

    return policy