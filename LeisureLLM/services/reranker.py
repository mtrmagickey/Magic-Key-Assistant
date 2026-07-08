"""
Cross-Encoder Reranker — semantic reranking for RAG quality.

Bi-encoder embeddings (used for retrieval) are fast but coarse: they
encode query and document independently, so nuance-level relevance is
lost.  A cross-encoder scores (query, document) pairs jointly, producing
much more accurate relevance judgments.

This module provides a lightweight reranking layer that:
1. Takes the top-K documents from HyDE/vector retrieval
2. Scores each against the query using a cross-encoder
3. Returns documents sorted by true semantic relevance

The reranker is OPTIONAL — if the model isn't installed, retrieval
falls back to the existing L2-distance ordering with zero degradation.

Usage::

    from services.reranker import rerank_documents
    reranked = await rerank_documents(question, docs, top_n=12)
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# ── Singleton model ──────────────────────────────────────────────────────────

_cross_encoder = None
_init_attempted = False


def _get_cross_encoder():
    """Lazy-load a cross-encoder model. Returns None if unavailable."""
    global _cross_encoder, _init_attempted
    if _init_attempted:
        return _cross_encoder
    _init_attempted = True

    try:
        from sentence_transformers import CrossEncoder
        # ms-marco-MiniLM is small (~80MB), fast (~5ms/doc), and well-suited
        # for passage reranking.  It runs on CPU with negligible latency.
        _cross_encoder = CrossEncoder(
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
            max_length=512,
        )
        logger.info("Cross-encoder reranker loaded: ms-marco-MiniLM-L-6-v2")
    except ImportError:
        logger.info(
            "sentence-transformers not installed — reranking disabled. "
            "Install with: pip install sentence-transformers"
        )
    except Exception as exc:
        logger.warning("Failed to load cross-encoder reranker: %s", exc)

    return _cross_encoder


async def rerank_documents(
    query: str,
    docs: List[Document],
    *,
    top_n: Optional[int] = None,
) -> List[Document]:
    """Rerank documents by cross-encoder relevance to the query.

    Parameters
    ----------
    query  : the user's question
    docs   : retrieved documents (any order)
    top_n  : if set, return only the top N documents after reranking.
             If None, returns all documents in reranked order.

    Returns sorted documents with ``rerank_score`` added to metadata.
    Falls back to the original order if the model is unavailable.
    """
    if not docs or len(docs) <= 1:
        return docs

    encoder = _get_cross_encoder()
    if encoder is None:
        return docs

    # Build (query, passage) pairs for scoring
    pairs = []
    for doc in docs:
        text = (doc.page_content or "")[:512]  # Cross-encoder max_length
        pairs.append((query, text))

    try:
        # Cross-encoder scoring is CPU-bound — run in thread to avoid blocking
        scores = await asyncio.to_thread(encoder.predict, pairs)
    except Exception as exc:
        logger.warning("Reranking failed (using original order): %s", exc)
        return docs

    # Attach scores and sort
    scored = list(zip(docs, scores))
    scored.sort(key=lambda pair: pair[1], reverse=True)  # Higher = more relevant

    result = []
    for doc, score in scored:
        doc.metadata = dict(doc.metadata) if doc.metadata else {}
        doc.metadata["rerank_score"] = round(float(score), 4)
        result.append(doc)

    if top_n and top_n < len(result):
        result = result[:top_n]

    logger.info(
        "Reranked %d docs: best=%.3f, worst=%.3f%s",
        len(result),
        scored[0][1] if scored else 0,
        scored[-1][1] if scored else 0,
        f" (top {top_n})" if top_n else "",
    )

    return result
