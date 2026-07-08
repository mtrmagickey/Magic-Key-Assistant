"""KB Search router — raw vector-store search for corpus / retrieval debugging."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from admin.dependencies import require_admin

router = APIRouter(tags=["kb_search"], dependencies=[Depends(require_admin)])

# ── Lazy vectorstore singleton ───────────────────────────────────────────────
_vectorstore = None
_vs_lock = asyncio.Lock()


async def _ensure_vectorstore():
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore
    async with _vs_lock:
        if _vectorstore is not None:
            return _vectorstore
        from core.chroma_factory import get_vectorstore
        _vectorstore = get_vectorstore()
    return _vectorstore


# ── Page route ───────────────────────────────────────────────────────────────

@router.get("/kb-search")
async def kb_search_page():
    return RedirectResponse(url="/knowledge?tab=search", status_code=302)


# ── API routes ───────────────────────────────────────────────────────────────

@router.get("/api/v1/kb/search")
async def api_kb_search(
    q: str = Query(..., min_length=1, description="Search query"),
    k: int = Query(10, ge=1, le=50, description="Number of results"),
    method: str = Query("similarity", description="similarity | keyword"),
):
    """
    Search the knowledge base directly — bypasses the LLM entirely.

    Two modes:
    - **similarity** — vector similarity search (same embeddings the bot uses).
      Returns chunks ranked by L2 distance (lower = closer match).
    - **keyword** — brute-force substring match against stored documents.
      Useful when you want to check whether a phrase exists in the corpus at all.
    """
    vs = await _ensure_vectorstore()

    if method == "keyword":
        return await _keyword_search(vs, q, k)
    return await _similarity_search(vs, q, k)


async def _similarity_search(vs, query: str, k: int):
    """Vector similarity search — returns docs + L2 distance scores."""
    results = await asyncio.to_thread(
        vs.similarity_search_with_score, query, k=k,
    )
    items = []
    for doc, score in results:
        items.append({
            "content": doc.page_content,
            "metadata": doc.metadata,
            "score": round(float(score), 4),
        })
    return {"success": True, "method": "similarity", "query": query, "count": len(items), "results": items}


async def _keyword_search(vs, query: str, k: int):
    """Brute-force substring search across all stored documents."""
    raw = await asyncio.to_thread(
        vs.get, include=["documents", "metadatas"],
    )
    docs = raw.get("documents") or []
    metas = raw.get("metadatas") or []
    ids = raw.get("ids") or []

    query_lower = query.lower()
    hits = []
    for i, (doc_text, meta, doc_id) in enumerate(zip(docs, metas, ids)):
        if doc_text and query_lower in doc_text.lower():
            # Find a snippet around the match
            idx = doc_text.lower().index(query_lower)
            start = max(0, idx - 120)
            end = min(len(doc_text), idx + len(query) + 120)
            snippet = doc_text[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(doc_text):
                snippet = snippet + "…"

            hits.append({
                "content": doc_text,
                "snippet": snippet,
                "metadata": meta or {},
                "id": doc_id,
            })
            if len(hits) >= k:
                break

    return {"success": True, "method": "keyword", "query": query, "count": len(hits), "total_docs": len(docs), "results": hits}


@router.get("/api/v1/kb/stats")
async def api_kb_stats():
    """Return basic corpus statistics — total chunks, sources, etc."""
    vs = await _ensure_vectorstore()
    raw = await asyncio.to_thread(vs.get, include=["metadatas"])
    metas = raw.get("metadatas") or []
    ids = raw.get("ids") or []

    sources: dict[str, int] = {}
    for meta in metas:
        src = (meta or {}).get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

    sorted_sources = sorted(sources.items(), key=lambda x: x[1], reverse=True)

    return {
        "success": True,
        "total_chunks": len(ids),
        "unique_sources": len(sources),
        "sources": [{"name": s, "chunks": c} for s, c in sorted_sources],
    }
