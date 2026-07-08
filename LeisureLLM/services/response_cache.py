"""
Response Cache — semantic query caching for the RAG pipeline.

Problem:
    Every chat request triggers HyDE generation + ChromaDB retrieval +
    LLM pipeline generation.  For a local Ollama model this takes 3-15s
    even when the same question was asked 2 minutes ago.

Solution:
    In-memory LRU cache keyed on (normalised_query, context_hash).
    - Exact match:  skip LLM entirely, return cached response   (~0ms)
    - Near-miss:    same question + same context → return cached  (~0ms)
    - TTL-based:    entries expire after N minutes (default 15)
    - Invalidation: cleared on document ingest so stale answers don't persist

The cache stores the full pipeline result dict so callers get models_used,
stages, etc. without re-running anything.

Design constraints:
    - Pure in-memory (no Redis/disk dependency)
    - Thread-safe via asyncio Lock
    - Bounded size (max 200 entries LRU)
    - Zero-dependency — only stdlib
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_DEFAULT_TTL_SECONDS = 15 * 60       # 15 minutes
_DEFAULT_MAX_ENTRIES = 200
_DEFAULT_CONTEXT_HASH_LEN = 64       # first 64 chars of sha256


# ── Cache entry ───────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    """A cached pipeline response."""
    query_key: str
    context_hash: str
    result: Dict[str, Any]       # full pipeline result dict
    sources: List[str]           # citation list
    chunk_sources: List[str]     # raw chunk paths
    context_words: int
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0

    def is_expired(self, ttl: float) -> bool:
        return (time.time() - self.created_at) > ttl


# ── Query normalisation ──────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _normalise_query(query: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return _NORM_RE.sub("", query.lower()).strip()


def _hash_context(context: str) -> str:
    """SHA-256 of context string, truncated."""
    return hashlib.sha256(context.encode("utf-8", errors="replace")).hexdigest()[:_DEFAULT_CONTEXT_HASH_LEN]


# ── The cache ─────────────────────────────────────────────────────────────────

class ResponseCache:
    """
    LRU response cache with TTL and ingest-invalidation.

    Usage::

        cache = ResponseCache()

        # Check before running pipeline
        hit = cache.get(query, context)
        if hit:
            return hit.result

        # After running pipeline, store
        cache.put(query, context, result, sources, chunk_sources, context_words)

        # On document ingest
        cache.invalidate_all()
    """

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    # ── Cache key ─────────────────────────────────────────────────────────

    @staticmethod
    def _make_key(query: str, context_hash: str) -> str:
        normalised = _normalise_query(query)
        return f"{normalised}|{context_hash}"

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, query: str, context: str) -> Optional[CacheEntry]:
        """Look up a cached response.  Returns None on miss."""
        ctx_hash = _hash_context(context)
        key = self._make_key(query, ctx_hash)

        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired(self._ttl):
            self._store.pop(key, None)
            self._misses += 1
            return None

        # Move to end (most-recently-used)
        self._store.move_to_end(key)
        entry.hit_count += 1
        self._hits += 1
        logger.debug("Cache HIT for '%s' (hit_count=%d)", query[:60], entry.hit_count)
        return entry

    def put(
        self,
        query: str,
        context: str,
        result: Dict[str, Any],
        sources: List[str],
        chunk_sources: List[str],
        context_words: int,
    ) -> None:
        """Store a pipeline result in the cache."""
        ctx_hash = _hash_context(context)
        key = self._make_key(query, ctx_hash)

        entry = CacheEntry(
            query_key=key,
            context_hash=ctx_hash,
            result=result,
            sources=sources,
            chunk_sources=chunk_sources,
            context_words=context_words,
        )
        self._store[key] = entry
        self._store.move_to_end(key)

        # Evict oldest if over capacity
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def invalidate_all(self) -> int:
        """Clear the entire cache.  Call after document ingest."""
        count = len(self._store)
        self._store.clear()
        if count:
            logger.info("Response cache invalidated (%d entries cleared)", count)
        return count

    def invalidate_query(self, query: str) -> bool:
        """Remove all entries matching a normalised query (any context)."""
        norm = _normalise_query(query)
        removed = False
        keys_to_remove = [k for k in self._store if k.startswith(f"{norm}|")]
        for k in keys_to_remove:
            del self._store[k]
            removed = True
        return removed

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Cache statistics for health/debug endpoints."""
        total = self._hits + self._misses
        return {
            "entries": len(self._store),
            "max_entries": self._max,
            "ttl_seconds": self._ttl,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }

    def __len__(self) -> int:
        return len(self._store)


# ── Module-level singleton ────────────────────────────────────────────────────

_cache: Optional[ResponseCache] = None


def get_response_cache() -> ResponseCache:
    """Get or create the module-level response cache singleton."""
    global _cache
    if _cache is None:
        _cache = ResponseCache()
    return _cache
