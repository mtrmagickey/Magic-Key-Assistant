"""
HyDE (Hypothetical Document Embeddings) retrieval service.

Improves RAG recall by searching with both the original question AND a
short LLM-generated hypothetical answer.  The hypothesis bridges the
vocabulary gap between "question language" and "document language":

    User asks:  "What's our pricing for swim lessons?"
    Hypothesis: "Swim lessons are priced at £X per session for members…"

The hypothesis matches document phrasing better than the bare question,
surfacing chunks that a single-query search would miss.

Usage:
    from services.hyde_retrieval import hyde_retrieve

    docs = await hyde_retrieve(vectorstore, question, generate_fn=my_llm)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# ── HyDE hypothesis prompt ──────────────────────────────────────────────────

_HYPOTHESIS_PROMPT = (
    "Write a short, factual paragraph (3-5 sentences) that directly answers "
    "the following question.  Invent plausible details — the goal is to "
    "produce text whose vocabulary and style resemble the real answer that "
    "might exist in our internal documents.\n\n"
    "Question: {question}\n\n"
    "Hypothetical answer:"
)


# ── Core retrieval function ──────────────────────────────────────────────────


async def hyde_retrieve(
    vectorstore: Any,
    question: str,
    *,
    generate_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    k: int = 20,
    hypothesis_max_tokens: int = 200,
    sub_queries: Optional[List[str]] = None,
) -> List[Document]:
    """
    Retrieve documents using HyDE: search with both the original question
    and a hypothetical answer, then merge and deduplicate by chunk ID.

    Parameters
    ----------
    vectorstore : Chroma (or any LangChain vectorstore with
                  ``similarity_search_with_score``)
    question    : the user's raw question
    generate_fn : async callable(prompt: str) -> str that calls an LLM.
                  If None or if hypothesis generation fails, falls back to
                  standard single-query retrieval.
    k           : number of results per search query
    hypothesis_max_tokens : soft hint for the hypothesis length
    sub_queries : optional list of decomposed sub-queries from the query
                  planner.  Each is searched in parallel alongside the
                  original question for broader recall.

    Returns
    -------
    List[Document] – deduplicated, ordered by best similarity score (lowest
                     distance first).
    """

    # ── Phase 1: Original search + hypothesis generation + entity extraction
    # These three operations are independent — run them concurrently.
    # ─────────────────────────────────────────────────────────────────────

    async def _original_search() -> List[tuple]:
        try:
            return await asyncio.to_thread(
                vectorstore.similarity_search_with_score, question, k=k,
            )
        except Exception as exc:
            logger.error("HyDE: standard search failed: %s", exc)
            return []

    async def _generate_hypothesis() -> Optional[str]:
        if generate_fn is None:
            return None
        try:
            prompt = _HYPOTHESIS_PROMPT.format(question=question)
            hyp = await generate_fn(prompt)
            if hyp:
                hyp = hyp.strip()
                if len(hyp) < 20:
                    return None
            return hyp
        except Exception as exc:
            logger.warning("HyDE: hypothesis generation failed (proceeding without): %s", exc)
            return None

    # Entity extraction is CPU-bound and fast — run synchronously
    try:
        entities = _extract_entities(question)
    except Exception:
        entities = []

    # Fire original search and hypothesis generation concurrently
    original_results, hypothesis = await asyncio.gather(
        _original_search(),
        _generate_hypothesis(),
    )

    # ── Phase 2: Hypothesis search + entity searches (all independent)
    # Now that we have the hypothesis text and entities, fire all
    # remaining vectorstore queries in parallel.
    # ─────────────────────────────────────────────────────────────────────

    phase2_tasks: List[asyncio.Task] = []
    _phase2_labels: List[str] = []  # for logging

    # Sub-query searches (from query planner decomposition)
    _extra_sub_queries = [q for q in (sub_queries or []) if q != question]
    for sq in _extra_sub_queries[:4]:  # cap at 4 extra sub-queries
        async def _sub_query_search(_q: str = sq) -> List[tuple]:
            try:
                results = await asyncio.to_thread(
                    vectorstore.similarity_search_with_score, _q, k=k // 2,
                )
                logger.debug(
                    "HyDE: sub-query search for '%s' returned %d results",
                    _q[:60], len(results),
                )
                return results
            except Exception:
                return []

        phase2_tasks.append(asyncio.ensure_future(_sub_query_search()))
        _phase2_labels.append(f"subquery:{sq[:40]}")

    if hypothesis:
        async def _hypothesis_search() -> List[tuple]:
            try:
                results = await asyncio.to_thread(
                    vectorstore.similarity_search_with_score, hypothesis, k=k,
                )
                logger.debug(
                    "HyDE: hypothesis search returned %d results (hypothesis: %.80s…)",
                    len(results), hypothesis,
                )
                return results
            except Exception as exc:
                logger.warning("HyDE: hypothesis search failed: %s", exc)
                return []

        phase2_tasks.append(asyncio.ensure_future(_hypothesis_search()))
        _phase2_labels.append("hypothesis")

    for entity in entities[:3]:
        async def _entity_search(_ent: str = entity) -> List[tuple]:
            try:
                results = await asyncio.to_thread(
                    vectorstore.similarity_search_with_score, _ent, k=k // 2,
                )
                logger.debug(
                    "HyDE: entity search for '%s' returned %d results",
                    _ent, len(results),
                )
                return results
            except Exception:
                return []

        phase2_tasks.append(asyncio.ensure_future(_entity_search()))
        _phase2_labels.append(f"entity:{entity}")

    # Await all phase-2 searches at once
    hypothesis_results: List[tuple] = []
    entity_results: List[tuple] = []
    subquery_results: List[tuple] = []
    if phase2_tasks:
        phase2_results = await asyncio.gather(*phase2_tasks, return_exceptions=True)
        for label, res in zip(_phase2_labels, phase2_results):
            if isinstance(res, BaseException):
                logger.debug("HyDE: %s search raised %s", label, res)
                continue
            if label == "hypothesis":
                hypothesis_results = res
            elif label.startswith("subquery:"):
                subquery_results.extend(res)
            else:
                entity_results.extend(res)

    # ── Merge & deduplicate ────────────────────────────────────────────
    seen: Dict[str, tuple] = {}  # chunk_id -> (doc, best_score)

    for doc, score in original_results + hypothesis_results + entity_results + subquery_results:
        # Use page_content hash as dedup key (chunk IDs aren't in metadata)
        chunk_key = _chunk_key(doc)
        if chunk_key not in seen or score < seen[chunk_key][1]:
            seen[chunk_key] = (doc, score)

    # Sort by score (lower = more similar in Chroma's L2 distance)
    merged = sorted(seen.values(), key=lambda pair: pair[1])

    # Embed retrieval score into document metadata so downstream code
    # (web search trigger, ranking) can assess context relevance without
    # needing a separate query.  Score is Chroma L2 distance: lower = better.
    docs = []
    for doc, score in merged:
        doc.metadata = dict(doc.metadata) if doc.metadata else {}
        doc.metadata["retrieval_score"] = round(float(score), 4)
        docs.append(doc)

    if hypothesis or entity_results or subquery_results:
        logger.info(
            "HyDE: merged %d original + %d hypothesis + %d entity + %d subquery -> %d unique docs",
            len(original_results),
            len(hypothesis_results),
            len(entity_results),
            len(subquery_results),
            len(docs),
        )

    return docs


async def gap_retrieve(
    vectorstore: Any,
    gap_queries: List[str],
    *,
    existing_docs: Optional[List[Document]] = None,
    k: int = 10,
) -> List[Document]:
    """Run targeted retrieval for gaps identified by the evidence evaluator.

    Searches each gap query in parallel, deduplicates against existing docs,
    and returns only NEW documents not already retrieved.

    Parameters
    ----------
    vectorstore  : Chroma vectorstore
    gap_queries  : short targeted queries for missing information
    existing_docs: docs already retrieved (for dedup)
    k            : results per gap query

    Returns
    -------
    List[Document] — new documents only, scored and sorted.
    """
    if not gap_queries:
        return []

    existing_keys = {_chunk_key(d) for d in (existing_docs or [])}

    async def _search(query: str) -> List[tuple]:
        try:
            return await asyncio.to_thread(
                vectorstore.similarity_search_with_score, query, k=k,
            )
        except Exception:
            return []

    # Fire all gap searches concurrently
    all_results = await asyncio.gather(
        *[_search(q) for q in gap_queries[:3]],
        return_exceptions=True,
    )

    seen: Dict[str, tuple] = {}
    for res in all_results:
        if isinstance(res, BaseException):
            continue
        for doc, score in res:
            key = _chunk_key(doc)
            if key in existing_keys:
                continue
            if key not in seen or score < seen[key][1]:
                seen[key] = (doc, score)

    merged = sorted(seen.values(), key=lambda pair: pair[1])
    new_docs = []
    for doc, score in merged:
        doc.metadata = dict(doc.metadata) if doc.metadata else {}
        doc.metadata["retrieval_score"] = round(float(score), 4)
        new_docs.append(doc)

    if new_docs:
        logger.info(
            "Gap retrieval: %d queries -> %d new docs (deduped against %d existing)",
            len(gap_queries), len(new_docs), len(existing_keys),
        )

    return new_docs


def _chunk_key(doc: Document) -> str:
    """Stable dedup key for a Document — uses page_content hash."""
    return str(hash(doc.page_content))


# ── Named-entity extraction for supplementary retrieval ──────────────────────

# Words that start sentences but aren't entities
_ENTITY_STOP = frozenset({
    "Tell", "What", "Who", "Where", "When", "Why", "How", "Can",
    "Could", "Would", "Should", "Does", "Did", "Will", "Are", "Is",
    "The", "This", "That", "These", "Those", "I", "We", "You",
    "My", "Our", "Your", "It", "Its", "They", "Them", "Their",
    "He", "She", "Him", "Her", "His", "Do", "Have", "Has",
    "If", "In", "On", "At", "By", "For", "And", "But", "Or",
    "Not", "No", "So", "Also", "Just", "About", "From", "With",
    "Some", "Any", "All", "Each", "Every", "Both", "Many", "Few",
    "More", "Most", "Other", "Such", "Very", "Too", "Here", "There",
    "Now", "Then", "However", "Although", "Because", "Since",
    "Risks", "Next", "Steps", "Note", "Gaps",
})


def _extract_entities(text: str) -> List[str]:
    """Extract multi-word proper noun phrases from text for entity search.

    Finds sequences of 2–4 capitalised words (e.g. "Jane Doe",
    "Acme Corp") that are likely named entities.
    Returns lowercased phrases for search.
    """
    entities: List[str] = []
    for m in re.finditer(r"\b([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){1,3})\b", text):
        phrase = m.group(1).strip()
        words = phrase.split()
        # Skip if first word is a common sentence-starter
        if words[0] in _ENTITY_STOP:
            continue
        # Must have at least 2 words
        if len(words) >= 2:
            entities.append(phrase.lower())
    # Deduplicate preserving order
    seen: set = set()
    result: List[str] = []
    for e in entities:
        if e not in seen:
            seen.add(e)
            result.append(e)
    return result


# ── Convenience: build a generate_fn from a ModelRouter ──────────────────────


def make_generate_fn_from_router(router, role: str = "initial"):
    """
    Create an async generate_fn from a ModelRouter instance.

    Uses the model assigned to the given pipeline role (default: "initial",
    which is typically the fastest / cheapest model).
    """
    from services.model_router import PipelineRole

    pipeline_role = PipelineRole(role)
    role_config = router.pipeline.roles.get(pipeline_role) if router.pipeline else None

    if role_config is None:
        return None

    async def _generate(prompt: str) -> str:
        return await router.generate_single(
            backend_name=role_config.backend_name,
            model=role_config.model,
            prompt=prompt,
            temperature=0.7,
            max_tokens=200,
        )

    return _generate
