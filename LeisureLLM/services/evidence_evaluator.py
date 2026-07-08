"""
Evidence Evaluator — LLM-Based Document Relevance Assessment
==============================================================

After retrieval but BEFORE final answer generation, this service asks a
fast LLM to evaluate which retrieved documents are actually relevant to
the question and why.  This replaces pure keyword-based scoring with
genuine comprehension:

- A keyword scorer might miss that "MTR Magic Key founding partners"
  is answered by a document about "company history — team bios".
- An LLM evaluator reads the actual content and understands the match.

The evaluator also identifies GAPS — information the question asks about
that NO retrieved document covers — which feeds into iterative retrieval.

This mirrors what reasoning models (o1/o3) do internally during their
hidden chain-of-thought: they evaluate evidence before synthesising.

Usage::

    from services.evidence_evaluator import evaluate_evidence

    eval_result = await evaluate_evidence(question, docs, generate_fn)
    # eval_result.relevant_indices  — which docs to keep
    # eval_result.gap_queries       — what to search for next
    # eval_result.evaluation_note   — inject into LLM context
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────

# Max docs to send to the evaluator (to bound prompt size)
_MAX_DOCS_TO_EVALUATE = 8

# Max chars per doc snippet sent to evaluator
_MAX_SNIPPET_CHARS = 600

# Only run evaluation when we have at least this many docs
_MIN_DOCS_FOR_EVALUATION = 3


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class EvidenceEvaluation:
    """Result of LLM evidence evaluation."""

    # Indices (0-based) of documents the LLM considers relevant
    relevant_indices: List[int] = field(default_factory=list)

    # Short queries for information the question needs but docs don't cover
    gap_queries: List[str] = field(default_factory=list)

    # Human-readable evaluation note to inject into final LLM context
    evaluation_note: str = ""

    # Whether evaluation actually ran (False = skipped/failed)
    evaluated: bool = False


# ── Evaluation prompt ────────────────────────────────────────────────────────

_EVALUATE_PROMPT = """\
You are a relevance evaluator for a RAG system. Given a user's question \
and retrieved documents, assess which documents help answer the question.

QUESTION: {question}

DOCUMENTS:
{doc_summaries}

Respond in this EXACT JSON format (no markdown, no extra text):
{{"relevant": [0, 2, 3], "gaps": ["gap query 1", "gap query 2"], "reasoning": "brief reasoning"}}

Rules:
- "relevant": list of document numbers (0-indexed) that contain information useful for answering the question. Include partial matches.
- "gaps": 0-2 short search queries for information the question needs but NONE of the documents cover. Empty list if the docs are sufficient.
- "reasoning": 1-2 sentences explaining your assessment.
- Be INCLUSIVE with relevance — if a document has even indirect useful context, include it.
- Only flag a gap if the question CLEARLY asks for something no document addresses."""


# ── Core API ─────────────────────────────────────────────────────────────────

async def evaluate_evidence(
    question: str,
    docs: List[Document],
    generate_fn: Optional[Callable[[str], Awaitable[str]]] = None,
) -> EvidenceEvaluation:
    """Evaluate which retrieved documents are relevant and identify gaps.

    Parameters
    ----------
    question    : the user's raw question
    docs        : retrieved documents (post-filtering)
    generate_fn : async callable(prompt: str) -> str for LLM evaluation.
                  If None, returns a no-op result.

    Returns
    -------
    EvidenceEvaluation with relevant indices, gap queries, and a note.
    """
    # Guard: skip for too few docs or no LLM
    if not docs or len(docs) < _MIN_DOCS_FOR_EVALUATION or generate_fn is None:
        return EvidenceEvaluation()

    eval_docs = docs[:_MAX_DOCS_TO_EVALUATE]

    # Build doc summaries for the prompt
    summaries = []
    for i, doc in enumerate(eval_docs):
        snippet = (doc.page_content or "")[:_MAX_SNIPPET_CHARS]
        source = ""
        if doc.metadata:
            source = doc.metadata.get("source_relpath") or doc.metadata.get("source") or ""
        header = f"[DOC {i}]"
        if source:
            header += f" (source: {source})"
        summaries.append(f"{header}\n{snippet}")

    doc_summaries = "\n\n".join(summaries)
    prompt = _EVALUATE_PROMPT.format(question=question, doc_summaries=doc_summaries)

    try:
        raw = await generate_fn(prompt)
        if not raw or not raw.strip():
            return EvidenceEvaluation()

        result = _parse_evaluation(raw, max_doc_idx=len(eval_docs) - 1)
        result.evaluated = True

        logger.info(
            "Evidence evaluation: %d/%d docs relevant, %d gaps identified. %s",
            len(result.relevant_indices),
            len(eval_docs),
            len(result.gap_queries),
            result.evaluation_note[:100] if result.evaluation_note else "",
        )

        return result

    except Exception as exc:
        logger.warning("Evidence evaluation failed (non-fatal): %s", exc)
        return EvidenceEvaluation()


def _parse_evaluation(raw: str, max_doc_idx: int) -> EvidenceEvaluation:
    """Parse the LLM's JSON response into an EvidenceEvaluation."""
    # Extract JSON from possible markdown wrapping
    json_match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not json_match:
        logger.debug("Evidence eval: no JSON found in response")
        return EvidenceEvaluation()

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        logger.debug("Evidence eval: invalid JSON: %s", raw[:200])
        return EvidenceEvaluation()

    # Parse relevant indices
    relevant = data.get("relevant", [])
    if isinstance(relevant, list):
        relevant = [int(i) for i in relevant if isinstance(i, (int, float)) and 0 <= int(i) <= max_doc_idx]
    else:
        relevant = list(range(max_doc_idx + 1))  # assume all relevant

    # Parse gap queries
    gaps = data.get("gaps", [])
    if isinstance(gaps, list):
        gaps = [str(g).strip() for g in gaps if g and str(g).strip()]
    else:
        gaps = []

    # Build evaluation note
    reasoning = data.get("reasoning", "")
    note = ""
    if gaps:
        note = (
            "[EVIDENCE EVALUATION: The retrieved documents address part of this "
            f"question but may not cover everything needed. Identified gaps: "
            f"{'; '.join(gaps)}. Fill gaps from web results or general knowledge "
            "if available, and be transparent about what isn't covered.]"
        )
    elif relevant and len(relevant) < max_doc_idx + 1:
        note = (
            "[EVIDENCE EVALUATION: Some retrieved documents are more relevant "
            "than others. Focus on the documents that directly address the question.]"
        )

    return EvidenceEvaluation(
        relevant_indices=relevant,
        gap_queries=gaps[:3],  # cap at 3
        evaluation_note=note,
    )
