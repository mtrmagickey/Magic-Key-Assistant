"""Tests for services.response_cache — LRU cache with TTL & invalidation."""

import time
from unittest.mock import patch

import pytest
from services.response_cache import (
    CacheEntry,
    ResponseCache,
    _hash_context,
    _normalise_query,
    get_response_cache,
)

# ── Normalisation helpers ─────────────────────────────────────────────────────

class TestNormalisation:
    def test_lowercase_and_strip_punctuation(self):
        assert _normalise_query("What's the SCHEDULE??") == "whats the schedule"

    def test_collapse_whitespace(self):
        assert _normalise_query("  hello   world  ") == "hello   world"

    def test_empty_string(self):
        assert _normalise_query("") == ""

    def test_hash_context_deterministic(self):
        h1 = _hash_context("some context")
        h2 = _hash_context("some context")
        assert h1 == h2

    def test_hash_context_differs(self):
        h1 = _hash_context("context A")
        h2 = _hash_context("context B")
        assert h1 != h2


# ── CacheEntry ────────────────────────────────────────────────────────────────

class TestCacheEntry:
    def test_not_expired(self):
        entry = CacheEntry(
            query_key="k", context_hash="h",
            result={"final": "hi"}, sources=[], chunk_sources=[],
            context_words=10,
        )
        assert not entry.is_expired(ttl=60)

    def test_expired(self):
        entry = CacheEntry(
            query_key="k", context_hash="h",
            result={"final": "hi"}, sources=[], chunk_sources=[],
            context_words=10,
            created_at=time.time() - 120,
        )
        assert entry.is_expired(ttl=60)


# ── ResponseCache core ───────────────────────────────────────────────────────

class TestResponseCache:
    def test_put_and_get(self):
        cache = ResponseCache(ttl_seconds=300, max_entries=10)
        cache.put("hello", "ctx", {"final": "world"}, ["s1"], ["cs1"], 5)
        hit = cache.get("hello", "ctx")
        assert hit is not None
        assert hit.result == {"final": "world"}

    def test_get_miss(self):
        cache = ResponseCache()
        assert cache.get("nonexistent", "ctx") is None

    def test_normalisation_matches(self):
        cache = ResponseCache()
        cache.put("What is the SCHEDULE?", "ctx", {"final": "Mon-Fri"}, [], [], 3)
        # Query with different casing / punctuation should still hit
        hit = cache.get("what is the schedule", "ctx")
        assert hit is not None
        assert hit.result["final"] == "Mon-Fri"

    def test_different_context_misses(self):
        cache = ResponseCache()
        cache.put("hello", "context-A", {"final": "a"}, [], [], 1)
        assert cache.get("hello", "context-B") is None

    def test_ttl_expiry(self):
        cache = ResponseCache(ttl_seconds=1)
        cache.put("q", "c", {"final": "r"}, [], [], 0)
        # Simulate time passing
        cache._store[list(cache._store.keys())[0]].created_at = time.time() - 5
        assert cache.get("q", "c") is None

    def test_lru_eviction(self):
        cache = ResponseCache(max_entries=2)
        cache.put("q1", "c", {"final": "r1"}, [], [], 0)
        cache.put("q2", "c", {"final": "r2"}, [], [], 0)
        cache.put("q3", "c", {"final": "r3"}, [], [], 0)
        # q1 should have been evicted
        assert cache.get("q1", "c") is None
        assert cache.get("q2", "c") is not None
        assert cache.get("q3", "c") is not None

    def test_invalidate_all(self):
        cache = ResponseCache()
        cache.put("a", "c", {"final": ""}, [], [], 0)
        cache.put("b", "c", {"final": ""}, [], [], 0)
        removed = cache.invalidate_all()
        assert removed == 2
        assert len(cache) == 0

    def test_invalidate_query(self):
        cache = ResponseCache()
        cache.put("hello world", "ctx1", {"final": "a"}, [], [], 0)
        cache.put("hello world", "ctx2", {"final": "b"}, [], [], 0)
        cache.put("goodbye", "ctx1", {"final": "c"}, [], [], 0)
        removed = cache.invalidate_query("Hello World!")
        assert removed is True
        assert len(cache) == 1  # only 'goodbye' remains

    def test_stats(self):
        cache = ResponseCache()
        cache.put("q", "c", {"final": "r"}, [], [], 0)
        cache.get("q", "c")  # hit
        cache.get("x", "c")  # miss
        s = cache.stats()
        assert s["entries"] == 1
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["hit_rate"] == 0.5

    def test_hit_count_increments(self):
        cache = ResponseCache()
        cache.put("q", "c", {"final": "r"}, [], [], 0)
        cache.get("q", "c")
        cache.get("q", "c")
        entry = cache.get("q", "c")
        assert entry.hit_count == 3


# ── Singleton ─────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_response_cache_returns_same_instance(self):
        # Reset module-level singleton first
        import services.response_cache as mod
        mod._cache = None
        c1 = get_response_cache()
        c2 = get_response_cache()
        assert c1 is c2
        mod._cache = None  # cleanup
