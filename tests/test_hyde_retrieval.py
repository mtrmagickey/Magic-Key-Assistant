"""Tests for HyDE (Hypothetical Document Embeddings) retrieval service."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR / "LeisureLLM"))

from langchain_core.documents import Document
from services.hyde_retrieval import _chunk_key, hyde_retrieve, make_generate_fn_from_router

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_doc(text: str, source: str = "test.txt") -> Document:
    return Document(page_content=text, metadata={"source": source})


def _fake_vectorstore(standard_results, hypothesis_results=None):
    """Return a mock vectorstore whose similarity_search_with_score responds
    differently depending on the query."""
    vs = MagicMock()
    call_count = {"n": 0}

    def _search(query, k=20):
        call_count["n"] += 1
        # First call = original question, second call = hypothesis
        if call_count["n"] == 1:
            return standard_results
        return hypothesis_results if hypothesis_results is not None else []

    vs.similarity_search_with_score = _search
    return vs


# ── Tests ────────────────────────────────────────────────────────────────────


class TestHydeRetrieve:
    """Core hyde_retrieve function tests."""

    @pytest.mark.asyncio
    async def test_standard_retrieval_without_generate_fn(self):
        """Without a generate_fn, HyDE falls back to standard retrieval."""
        doc = _make_doc("swim lesson pricing guide")
        vs = MagicMock()
        vs.similarity_search_with_score = MagicMock(return_value=[(doc, 0.3)])

        results = await hyde_retrieve(vs, "swim lessons price", generate_fn=None, k=5)

        assert len(results) == 1
        assert results[0].page_content == "swim lesson pricing guide"
        vs.similarity_search_with_score.assert_called_once_with("swim lessons price", k=5)

    @pytest.mark.asyncio
    async def test_hyde_merges_results(self):
        """HyDE merges original + hypothesis results and deduplicates."""
        doc_a = _make_doc("document A — pricing guide")
        doc_b = _make_doc("document B — schedule info")
        doc_c = _make_doc("document C — unique to hypothesis")

        # Original search returns A and B
        standard = [(doc_a, 0.3), (doc_b, 0.5)]
        # Hypothesis search returns B (duplicate) and C (new)
        hypothesis = [(doc_b, 0.4), (doc_c, 0.2)]

        vs = _fake_vectorstore(standard, hypothesis)

        async def fake_generate(prompt: str) -> str:
            return "Our swim lessons cost £10 per session for members."

        results = await hyde_retrieve(vs, "swim prices", generate_fn=fake_generate, k=5)

        texts = [d.page_content for d in results]
        assert len(results) == 3
        # C has lowest score (0.2) so should be first
        assert texts[0] == "document C — unique to hypothesis"
        # A has 0.3
        assert texts[1] == "document A — pricing guide"
        # B appears in both; best score is 0.4 (from hypothesis)
        assert texts[2] == "document B — schedule info"

    @pytest.mark.asyncio
    async def test_dedup_keeps_best_score(self):
        """When a doc appears in both queries, the better score wins."""
        doc = _make_doc("shared document")
        standard = [(doc, 0.8)]
        hypothesis = [(doc, 0.2)]

        vs = _fake_vectorstore(standard, hypothesis)

        async def fake_generate(prompt):
            return "A plausible hypothesis about shared document."

        results = await hyde_retrieve(vs, "test query", generate_fn=fake_generate, k=5)

        assert len(results) == 1
        assert results[0].page_content == "shared document"

    @pytest.mark.asyncio
    async def test_generate_fn_failure_falls_back(self):
        """If hypothesis generation fails, standard retrieval still works."""
        doc = _make_doc("fallback doc")
        vs = MagicMock()
        vs.similarity_search_with_score = MagicMock(return_value=[(doc, 0.4)])

        async def failing_generate(prompt):
            raise RuntimeError("LLM is down")

        results = await hyde_retrieve(vs, "question", generate_fn=failing_generate, k=5)

        assert len(results) == 1
        assert results[0].page_content == "fallback doc"

    @pytest.mark.asyncio
    async def test_short_hypothesis_discarded(self):
        """A hypothesis shorter than 20 chars is treated as degenerate."""
        doc = _make_doc("only standard")
        vs = MagicMock()
        vs.similarity_search_with_score = MagicMock(return_value=[(doc, 0.5)])

        async def short_generate(prompt):
            return "No idea."  # 8 chars

        results = await hyde_retrieve(vs, "test", generate_fn=short_generate, k=5)

        # Should only have called search once (no hypothesis search)
        assert vs.similarity_search_with_score.call_count == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_empty_vectorstore(self):
        """Handles empty search results gracefully."""
        vs = MagicMock()
        vs.similarity_search_with_score = MagicMock(return_value=[])

        results = await hyde_retrieve(vs, "anything", generate_fn=None, k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_vectorstore_search_failure(self):
        """If the vectorstore itself fails, returns empty list."""
        vs = MagicMock()
        vs.similarity_search_with_score = MagicMock(side_effect=RuntimeError("DB error"))

        results = await hyde_retrieve(vs, "question", generate_fn=None, k=5)
        assert results == []


class TestMakeGenerateFn:
    """Tests for make_generate_fn_from_router."""

    def test_returns_none_without_pipeline(self):
        """No pipeline configured → returns None."""
        router = MagicMock()
        router.pipeline = None
        fn = make_generate_fn_from_router(router)
        assert fn is None

    def test_returns_none_without_role(self):
        """Pipeline exists but the requested role is missing → returns None."""
        from services.model_router import PipelineRole

        router = MagicMock()
        router.pipeline = MagicMock()
        router.pipeline.roles = {}  # no roles
        fn = make_generate_fn_from_router(router, role="initial")
        assert fn is None

    def test_returns_callable_with_valid_role(self):
        """Pipeline with the requested role → returns an async callable."""
        from services.model_router import PipelineRole, RoleConfig

        role_config = RoleConfig(
            role=PipelineRole.INITIAL,
            backend_name="ollama",
            model="llama3.3:70b-instruct-q4_K_M",
            temperature=0.4,
            max_tokens=4000,
            enabled=True,
        )
        router = MagicMock()
        router.pipeline = MagicMock()
        router.pipeline.roles = {PipelineRole.INITIAL: role_config}

        fn = make_generate_fn_from_router(router, role="initial")
        assert fn is not None
        assert asyncio.iscoroutinefunction(fn)

    @pytest.mark.asyncio
    async def test_generate_fn_calls_router(self):
        """The returned function calls router.generate_single correctly."""
        from services.model_router import PipelineRole, RoleConfig

        role_config = RoleConfig(
            role=PipelineRole.INITIAL,
            backend_name="ollama",
            model="test-model",
            temperature=0.4,
            max_tokens=4000,
            enabled=True,
        )
        router = MagicMock()
        router.pipeline = MagicMock()
        router.pipeline.roles = {PipelineRole.INITIAL: role_config}
        router.generate_single = AsyncMock(return_value="Hypothetical answer text")

        fn = make_generate_fn_from_router(router, role="initial")
        result = await fn("some prompt")

        assert result == "Hypothetical answer text"
        router.generate_single.assert_called_once_with(
            backend_name="ollama",
            model="test-model",
            prompt="some prompt",
            temperature=0.7,
            max_tokens=200,
        )


class TestChunkKey:
    """Tests for the _chunk_key dedup helper."""

    def test_same_content_same_key(self):
        d1 = _make_doc("identical content", "a.txt")
        d2 = _make_doc("identical content", "b.txt")
        assert _chunk_key(d1) == _chunk_key(d2)

    def test_different_content_different_key(self):
        d1 = _make_doc("content A")
        d2 = _make_doc("content B")
        assert _chunk_key(d1) != _chunk_key(d2)
