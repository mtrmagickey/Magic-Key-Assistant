"""
Self-Correcting Retrieval Service
==================================

When the initial retrieval context is sparse or the self-assessment flags
low groundedness, this service reformulates the query using multiple
strategies and retries retrieval to surface better-matching documents.

Strategies (tried in order, short-circuited on success):

1. **Synonym expansion** — LLM rewrites the question with alternative
   vocabulary that may match document phrasing better.
2. **Sub-question decomposition** — breaks a complex question into simpler
   sub-questions and retrieves for each.
3. **Entity-focused search** — extracts key entities / proper nouns and
   uses them as dedicated search queries.
4. **Broadening** — strips specifics from the question to find more
   general matches.

The service is designed to integrate transparently into the existing
retrieval pipeline (HyDE + standard search) as an *additional* layer
that only activates when initial results are weak.

Usage::

    from services.self_correcting_retrieval import corrective_retrieve

    docs = await corrective_retrieve(
        vectorstore, question,
        initial_docs=existing_docs,      # from HyDE/standard retrieval
        generate_fn=my_llm_fn,           # async str→str
    )
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, List, Optional, Set

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# Minimum word count in initial context to consider it "sufficient"
SPARSE_WORD_THRESHOLD = 80

# If the best similarity score (Chroma L2 distance) is above this, context is weak
WEAK_SIMILARITY_THRESHOLD = 1.4

# Maximum extra retrieval queries to run
MAX_REFORMULATIONS = 3

# Number of docs per reformulated query
REFORMULATION_K = 10


# ── Prompts ───────────────────────────────────────────────────────────────────

_REFORMULATION_PROMPT = """\
You are a search query optimizer. The user's question didn't retrieve \
good results from our internal knowledge base. Generate {n} alternative \
search queries that might match document phrasing better.

Original question: {question}

Rules:
- Use different vocabulary / synonyms for key terms
- Try both specific and broader phrasings
- One query should decompose the question into a simpler sub-question
- Keep each query short (under 20 words)

Return ONLY the queries, one per line, no numbering or bullets."""

_ENTITY_EXTRACTION_PROMPT = """\
Extract the 2-3 most important named entities, proper nouns, or domain terms \
from this question. Return each on its own line, nothing else.

Question: {question}"""


# ── Core API ──────────────────────────────────────────────────────────────────

async def corrective_retrieve(
    vectorstore: Any,
    question: str,
    *,
    initial_docs: Optional[List[Document]] = None,
    initial_context_words: int = 0,
    generate_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    k: int = REFORMULATION_K,
    sparse_threshold: int = SPARSE_WORD_THRESHOLD,
) -> List[Document]:
    """Run corrective retrieval if initial context is sparse or weak.

    Parameters
    ----------
    vectorstore : Chroma (or any LangChain vectorstore)
    question : the user's raw question
    initial_docs : documents already retrieved (to merge/dedup with)
    initial_context_words : word count of formatted initial context
    generate_fn : async callable for LLM-powered reformulation.
        If None, uses heuristic reformulation only.
    k : results per reformulated query
    sparse_threshold : word count below which context is "sparse"

    Returns
    -------
    List[Document] — merged, deduplicated documents (initial + corrective)
    """
    initial_docs = initial_docs or []

    # Bail early if context is already sufficient
    if initial_context_words >= sparse_threshold:
        return initial_docs

    logger.info(
        "Corrective retrieval activated: %d initial docs, %d context words (threshold %d)",
        len(initial_docs), initial_context_words, sparse_threshold,
    )

    # Generate reformulated queries
    queries = await _generate_reformulations(
        question, generate_fn=generate_fn, n=MAX_REFORMULATIONS,
    )

    if not queries:
        return initial_docs

    # Run reformulated searches
    seen_keys: Set[str] = {_doc_key(d) for d in initial_docs}
    new_docs: List[Document] = []

    for query in queries:
        try:
            results = await asyncio.to_thread(
                vectorstore.similarity_search_with_score, query, k=k,
            )
            for doc, score in results:
                key = _doc_key(doc)
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_docs.append(doc)
        except Exception as exc:
            logger.debug("Corrective search failed for %r: %s", query, exc)
            continue

    if new_docs:
        logger.info(
            "Corrective retrieval found %d additional docs from %d reformulated queries",
            len(new_docs), len(queries),
        )

    # Merge: original first (preserves ordering), then new docs
    return initial_docs + new_docs


async def reformulate_after_assessment(
    question: str,
    assessment_result: Any,
    vectorstore: Any,
    *,
    generate_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    k: int = REFORMULATION_K,
) -> List[Document]:
    """Run corrective retrieval informed by a self-assessment result.

    Called by the chat pipeline when ``assessment.gap_detected`` is True
    and ``assessment.grounded`` is False.  Uses the ``missing_knowledge``
    field to guide reformulation.

    Returns additional documents that may fill the gap.
    """
    extra_hint = ""
    if hasattr(assessment_result, "missing_knowledge") and assessment_result.missing_knowledge:
        extra_hint = assessment_result.missing_knowledge

    # Build targeted queries from assessment insight
    queries: List[str] = []

    if extra_hint:
        # Use the missing knowledge description as a direct search query
        queries.append(extra_hint[:200])

    # Also run standard reformulation
    reformulated = await _generate_reformulations(
        question, generate_fn=generate_fn, n=2,
    )
    queries.extend(reformulated)

    if not queries:
        return []

    seen: Set[str] = set()
    docs: List[Document] = []
    for query in queries[:MAX_REFORMULATIONS + 1]:
        try:
            results = await asyncio.to_thread(
                vectorstore.similarity_search_with_score, query, k=k,
            )
            for doc, _score in results:
                key = _doc_key(doc)
                if key not in seen:
                    seen.add(key)
                    docs.append(doc)
        except Exception:
            continue

    logger.info(
        "Post-assessment corrective retrieval found %d docs for %r",
        len(docs), question[:80],
    )
    return docs


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _generate_reformulations(
    question: str,
    *,
    generate_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    n: int = 3,
) -> List[str]:
    """Generate alternative search queries via LLM or heuristics."""
    queries: List[str] = []

    # Try LLM-based reformulation first
    if generate_fn:
        try:
            prompt = _REFORMULATION_PROMPT.format(question=question, n=n)
            raw = await asyncio.wait_for(generate_fn(prompt), timeout=10.0)
            for line in raw.strip().splitlines():
                cleaned = line.strip().lstrip("0123456789.-•) ")
                if len(cleaned) > 8 and cleaned.lower() != question.lower():
                    queries.append(cleaned)
                if len(queries) >= n:
                    break
        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("LLM reformulation failed: %s", exc)

    # Heuristic fallback: generate simple variants
    if len(queries) < n:
        queries.extend(_heuristic_reformulations(question, n - len(queries)))

    return queries[:n]


def _heuristic_reformulations(question: str, n: int = 2) -> List[str]:
    """Generate simple query variants without an LLM."""
    variants: List[str] = []

    # Strategy 1: Remove question words and punctuation → keyword search
    keywords = re.sub(
        r"\b(what|when|where|who|how|why|is|are|was|were|do|does|did|can|"
        r"could|would|should|the|a|an|in|on|at|for|to|of|and|or|my|our)\b",
        "", question, flags=re.IGNORECASE,
    )
    keywords = re.sub(r"[?!.,;:]", "", keywords)
    keywords = " ".join(keywords.split())
    if keywords and len(keywords) > 5:
        variants.append(keywords)

    # Strategy 2: First-noun-phrase broadening
    words = question.split()
    if len(words) > 4:
        # Take the last 60% of words (often the key subject)
        tail = " ".join(words[len(words) // 3 :])
        tail = re.sub(r"[?!.,;:]", "", tail).strip()
        if tail and tail not in variants:
            variants.append(tail)

    # Strategy 3: Quoted key terms (helps if question contains a proper noun)
    _QUESTION_WORDS = {"what", "when", "where", "who", "how", "why", "which", "whom", "whose"}
    caps = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", question)
    caps = [c for c in caps if c.lower() not in _QUESTION_WORDS]
    if caps:
        term = " ".join(caps[:3])
        if term and term not in variants:
            variants.append(term)

    return variants[:n]


def _doc_key(doc: Document) -> str:
    """Stable dedup key for a Document."""
    return str(hash(doc.page_content))
