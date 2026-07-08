"""
Query Planner — Multi-Hop Query Decomposition
==============================================

Before retrieval, decomposes complex / multi-part questions into targeted
sub-queries.  Each sub-query retrieves independently, and results are
merged.  This mirrors the "chain-of-thought query planning" step that
reasoning models (o1/o3) perform internally.

This is COMPLEMENTARY to HyDE — HyDE bridges vocabulary gaps within a
single query; the planner ensures we don't miss information when a
question has multiple facets.

Simple questions (single-facet, short) skip decomposition entirely.

Usage::

    from services.query_planner import decompose_query

    sub_queries = await decompose_query(question, generate_fn=my_llm)
    # Returns ["sub-query 1", "sub-query 2", ...] or [question] if simple
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# ── Heuristic complexity signals ─────────────────────────────────────────────

_MULTI_PART_CLUES = [
    " and ",
    " as well as ",
    " also ",
    " plus ",
    " along with ",
    " in addition to ",
    " versus ",
    " vs ",
    " compared to ",
    " but also ",
    ", and ",
]

_COMPOUND_QUESTION_PATTERN = re.compile(
    r"\?.*?\b(and|also|what about|how about|additionally)\b",
    re.IGNORECASE,
)

# Questions under this word count are almost always single-facet
_MIN_WORDS_FOR_DECOMPOSITION = 8


def _is_complex_query(question: str) -> bool:
    """Fast heuristic: does this question likely have multiple facets?"""
    if len(question.split()) < _MIN_WORDS_FOR_DECOMPOSITION:
        return False

    low = question.lower()

    # Multiple question marks → definitely multi-part
    if question.count("?") > 1:
        return True

    # Compound conjunctions
    if any(clue in low for clue in _MULTI_PART_CLUES):
        return True

    # Compound after a question mark
    if _COMPOUND_QUESTION_PATTERN.search(question):
        return True

    return False


# ── LLM-based decomposition ─────────────────────────────────────────────────

_DECOMPOSE_PROMPT = (
    "You are a search query planner for an internal knowledge base. "
    "Break this question into 2-4 focused sub-queries that each target "
    "ONE specific piece of information. Each sub-query should be a "
    "complete, self-contained search query.\n\n"
    "Rules:\n"
    "- Keep each sub-query SHORT (under 15 words)\n"
    "- Preserve proper nouns and entity names exactly\n"
    "- If the question is already simple and single-facet, return it unchanged\n"
    "- Do NOT add sub-queries that go beyond what the user asked\n\n"
    "Question: {question}\n\n"
    "Sub-queries (one per line, no numbering):"
)


async def decompose_query(
    question: str,
    *,
    generate_fn: Optional[Callable[[str], Awaitable[str]]] = None,
) -> List[str]:
    """Decompose a complex question into targeted sub-queries.

    Parameters
    ----------
    question    : the user's raw question
    generate_fn : async callable(prompt: str) -> str for LLM decomposition.
                  If None or if the question is simple, returns [question].

    Returns
    -------
    List[str] — sub-queries (always includes the original question as the
                first entry so standard retrieval still runs).
    """
    # Fast path: simple questions don't need decomposition
    if not _is_complex_query(question):
        return [question]

    if generate_fn is None:
        # No LLM available — fall back to heuristic splitting
        return _heuristic_split(question)

    try:
        prompt = _DECOMPOSE_PROMPT.format(question=question)
        raw = await generate_fn(prompt)
        if not raw or not raw.strip():
            return [question]

        # Parse lines
        sub_queries = []
        for line in raw.strip().splitlines():
            line = line.strip().lstrip("0123456789.-) ").strip()
            if line and len(line) > 5:
                sub_queries.append(line)

        if not sub_queries:
            return [question]

        # Always include the original question first (full-scope retrieval)
        if question not in sub_queries:
            sub_queries.insert(0, question)

        # Cap at 5 total queries to bound latency
        sub_queries = sub_queries[:5]

        logger.info(
            "Query planner: decomposed into %d sub-queries: %s",
            len(sub_queries),
            [q[:60] for q in sub_queries],
        )
        return sub_queries

    except Exception as exc:
        logger.warning("Query decomposition failed (using original): %s", exc)
        return [question]


def _heuristic_split(question: str) -> List[str]:
    """Cheaply split a multi-part question without an LLM.

    Splits on 'and', question marks, or semicolons while keeping the
    original question as the first entry.
    """
    parts = [question]

    # Split on multiple question marks
    if question.count("?") > 1:
        segments = [s.strip() + "?" for s in question.split("?") if s.strip()]
        if len(segments) > 1:
            parts = [question] + segments

    # Split on " and " in the middle of the sentence
    elif " and " in question.lower() and len(question.split()) >= 10:
        idx = question.lower().index(" and ")
        before = question[:idx].strip()
        after = question[idx + 5:].strip()
        if len(before.split()) >= 3 and len(after.split()) >= 3:
            parts = [question, before, after]

    return parts[:5]
