"""
Answer Self-Assessment Service
==============================

After the bot generates a response, this service asks the LLM to evaluate
its own answer quality and identify what knowledge would have made it better.

Two capabilities:
1. **Self-assessment** — LLM scores its confidence and identifies missing docs.
2. **Near-miss retrieval** — searches the corpus for chunks that *almost*
   answer the question, attached to gaps to accelerate resolution.

This replaces the heuristic-only gap detection (string-matching hedge phrases)
with genuine LLM introspection, catching cases where the bot *sounds* confident
but is actually confabulating or guessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Self-Assessment Prompt ────────────────────────────────────────────────────

_SELF_ASSESSMENT_PROMPT = """\
You are a quality auditor reviewing an AI assistant's response.

## User Question
{question}

## Retrieved Knowledge Base Context
{context}

## Assistant's Response
{response}

## Task
Evaluate the response honestly. Score from 1-10 and identify what's missing.

Respond in STRICT JSON only — no markdown, no commentary:
{{
  "confidence": <1-10 integer>,
  "grounded": <true if answer is supported by the retrieved context, false if the assistant went beyond what the context provides>,
  "gap_detected": <true if a knowledge gap should be logged>,
  "missing_knowledge": "<1-2 sentence description of what document, policy, or information would have let the assistant give a perfect answer. Empty string if nothing is missing.>",
  "suggested_topic": "<short topic label (3-8 words) for the knowledge gap, or empty string if no gap>"
}}

## Scoring Guide
- **9-10**: Answer is fully grounded in retrieved context, comprehensive, specific.
- **7-8**: Answer is mostly grounded but makes minor assumptions or lacks detail.
- **5-6**: Answer is partially grounded — some claims aren't in the context.
- **3-4**: Answer is mostly generated/assumed — little grounding in context.
- **1-2**: Answer is fabricated or the question is completely outside the knowledge base.

Set "gap_detected" to true if confidence <= 6 OR grounded is false.
"""

# ── Thresholds ────────────────────────────────────────────────────────────────

CONFIDENCE_GAP_THRESHOLD = 6   # Score at or below → log a gap
NEAR_MISS_K = 5                # How many near-miss chunks to retrieve
NEAR_MISS_MIN_SCORE = 0.35     # Minimum similarity to count as a near-miss
                                # (Chroma distance scores — lower is more similar)


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class SelfAssessmentResult:
    """Result of an LLM self-assessment of its own answer."""
    confidence: int = 10
    grounded: bool = True
    gap_detected: bool = False
    missing_knowledge: str = ""
    suggested_topic: str = ""
    raw_json: str = ""
    error: Optional[str] = None


@dataclass
class NearMissResult:
    """Near-miss chunks that almost answer the question."""
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""


# ── Self-Assessment ──────────────────────────────────────────────────────────

async def assess_answer_quality(
    question: str,
    response: str,
    context: str,
    router=None,
) -> SelfAssessmentResult:
    """Ask the LLM to evaluate its own response quality.

    Uses the INITIAL (fastest/cheapest) model in the pipeline for speed.
    Falls back to heuristic detection if the router isn't available.

    Parameters
    ----------
    question : str
        The user's original question.
    response : str
        The bot's generated response text.
    context : str
        The RAG context that was provided to the bot.
    router : ModelRouter, optional
        Pipeline router for LLM access. Auto-loaded if not provided.

    Returns
    -------
    SelfAssessmentResult
    """
    if not router:
        try:
            from services.rag_pipeline import get_pipeline_router
            router = await get_pipeline_router()
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

    if not router or not router.pipeline:
        # No pipeline available — fall back to heuristic
        return _heuristic_fallback(response, context)

    # Use the fastest model (INITIAL role) for the self-assessment
    from services.model_router import PipelineRole
    role_config = router.pipeline.roles.get(PipelineRole.INITIAL)
    if not role_config or not role_config.enabled:
        # Try any available role
        for role in PipelineRole:
            rc = router.pipeline.roles.get(role)
            if rc and rc.enabled:
                role_config = rc
                break

    if not role_config:
        return _heuristic_fallback(response, context)

    # Truncate context to avoid blowing token limits on the assessment call
    context_truncated = context[:3000] if len(context) > 3000 else context
    response_truncated = response[:2000] if len(response) > 2000 else response

    prompt = _SELF_ASSESSMENT_PROMPT.format(
        question=question,
        context=context_truncated or "(No context retrieved)",
        response=response_truncated,
    )

    try:
        raw = await asyncio.wait_for(
            router.generate_single(
                backend_name=role_config.backend_name,
                model=role_config.model,
                prompt=prompt,
                temperature=0.1,   # Low temp for deterministic scoring
                max_tokens=250,
            ),
            timeout=router.timeouts.self_assessment,  # Centralised in model_router.json
        )

        result = _parse_assessment_json(raw)
        return result

    except asyncio.TimeoutError:
        logger.debug("Self-assessment timed out — falling back to heuristic")
        return _heuristic_fallback(response, context)
    except Exception as e:
        logger.debug("Self-assessment failed: %s — falling back to heuristic", e)
        return _heuristic_fallback(response, context)


def _parse_assessment_json(raw: str) -> SelfAssessmentResult:
    """Parse the LLM's JSON response into a SelfAssessmentResult."""
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        import re
        match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            return SelfAssessmentResult(
                error=f"Could not parse JSON from: {text[:200]}"
            )

    confidence = int(data.get("confidence", 10))
    grounded = bool(data.get("grounded", True))
    gap_detected = bool(data.get("gap_detected", False))

    # Override: if confidence is low, always flag a gap
    if confidence <= CONFIDENCE_GAP_THRESHOLD:
        gap_detected = True

    # Override: if ungrounded, always flag a gap
    if not grounded:
        gap_detected = True

    return SelfAssessmentResult(
        confidence=confidence,
        grounded=grounded,
        gap_detected=gap_detected,
        missing_knowledge=str(data.get("missing_knowledge", "")),
        suggested_topic=str(data.get("suggested_topic", "")),
        raw_json=text,
    )


def _heuristic_fallback(response: str, context: str) -> SelfAssessmentResult:
    """Heuristic gap detection when the LLM isn't available for self-assessment."""
    from services.rag_pipeline import GAP_INDICATORS, SPARSE_CONTEXT_THRESHOLD

    response_lower = response.lower()
    has_indicator = any(ind in response_lower for ind in GAP_INDICATORS)
    context_words = len(context.split()) if context else 0
    sparse = context_words < SPARSE_CONTEXT_THRESHOLD

    if has_indicator or sparse:
        return SelfAssessmentResult(
            confidence=3 if has_indicator else 5,
            grounded=False,
            gap_detected=True,
            missing_knowledge="Detected via heuristic: " + (
                "response contained hedge phrases"
                if has_indicator
                else f"sparse retrieval context ({context_words} words)"
            ),
        )

    return SelfAssessmentResult(confidence=8, grounded=True, gap_detected=False)


# ── Near-Miss Retrieval ──────────────────────────────────────────────────────

async def find_near_misses(
    question: str,
    vectorstore=None,
    k: int = NEAR_MISS_K,
) -> NearMissResult:
    """Search the corpus for chunks that *almost* answer the question.

    Useful when a knowledge gap is detected — near-misses tell the user
    "we have something close, here's what's missing specifically."

    Parameters
    ----------
    question : str
        The user's question to search for near-matches.
    vectorstore : optional
        LangChain Chroma vectorstore. Auto-loaded if not provided.
    k : int
        Number of near-miss chunks to retrieve.

    Returns
    -------
    NearMissResult
        With chunks (list of {source, content_preview, score, metadata})
        and a summary string for display.
    """
    if not vectorstore:
        try:
            from core.chroma_factory import get_vectorstore
            vectorstore = get_vectorstore()
        except Exception as e:
            logger.debug("Near-miss retrieval skipped — no vectorstore: %s", e)
            return NearMissResult()

    try:
        results = await asyncio.to_thread(
            vectorstore.similarity_search_with_score,
            question,
            k=k,
        )
    except Exception as e:
        logger.debug("Near-miss similarity search failed: %s", e)
        return NearMissResult()

    if not results:
        return NearMissResult()

    chunks = []
    for doc, score in results:
        meta = doc.metadata or {}
        source = meta.get("source_relpath") or meta.get("source") or "unknown"
        content_preview = doc.page_content[:200].strip() if doc.page_content else ""

        chunks.append({
            "source": source,
            "content_preview": content_preview,
            "score": round(float(score), 4),
            "doc_type": meta.get("doc_type", ""),
            "topics": meta.get("topics", ""),
            "confidence": meta.get("confidence", ""),
        })

    if not chunks:
        return NearMissResult()

    # Build a human-readable summary
    summaries = []
    for i, c in enumerate(chunks[:3], 1):
        source_name = c["source"].split("/")[-1].split("\\")[-1] if c["source"] else "?"
        summaries.append(f"{i}. **{source_name}** — {c['content_preview'][:80]}...")

    summary = "Near-miss documents found:\n" + "\n".join(summaries)

    return NearMissResult(chunks=chunks, summary=summary)


def format_near_misses_for_context(near_misses: NearMissResult) -> str:
    """Format near-miss results as a string suitable for the gap `context` field."""
    if not near_misses.chunks:
        return ""

    parts = []
    for c in near_misses.chunks[:3]:
        source_name = c["source"].split("/")[-1].split("\\")[-1] if c["source"] else "?"
        parts.append(f"• {source_name} (score: {c['score']}): {c['content_preview'][:120]}")

    return "Near-miss docs:\n" + "\n".join(parts)
