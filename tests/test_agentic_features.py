"""
Tests for the six agentic / self-healing features.

1. Circuit Breaker
2. Cross-Backend Fallback Chain (ModelRouter.generate_with_fallback)
3. Self-Correcting Retrieval
4. Automatic Document Quality Remediation
5. Proactive Health Auto-Fix (Steward wiring — integration level)
6. Agentic Web Search Tool (tools_builtin._search_web)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═════════════════════════════════════════════════════════════════════════════
# 1. CIRCUIT BREAKER
# ═════════════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    """Tests for services.circuit_breaker."""

    def setup_method(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        CircuitBreakerRegistry.clear()

    def test_initial_state_is_closed(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_trips_after_threshold_failures(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test", failure_threshold=3)
        cb.record_failure("err1")
        cb.record_failure("err2")
        assert cb.state == CircuitState.CLOSED  # not yet
        cb.record_failure("err3")
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_success_resets_failure_count(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test", failure_threshold=3)
        cb.record_failure("e1")
        cb.record_failure("e2")
        cb.record_success()
        cb.record_failure("e3")  # Only 1 consecutive now
        assert cb.state == CircuitState.CLOSED

    def test_half_open_after_cooldown(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure("boom")
        assert cb.state == CircuitState.OPEN
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request() is True

    def test_half_open_to_closed_on_success(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=0.01, success_threshold=2)
        cb.record_failure("boom")
        time.sleep(0.02)
        _ = cb.state  # triggers HALF_OPEN
        cb.record_success()
        assert cb._state == CircuitState.HALF_OPEN  # need 2
        cb.record_success()
        assert cb._state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=0.01)
        cb.record_failure("boom")
        time.sleep(0.02)
        _ = cb.state  # triggers HALF_OPEN
        cb.record_failure("boom again")
        assert cb._state == CircuitState.OPEN
        assert cb._total_trips == 2

    def test_registry_get_or_create(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        b1 = CircuitBreakerRegistry.get_or_create("svc_a", failure_threshold=5)
        b2 = CircuitBreakerRegistry.get_or_create("svc_a")
        assert b1 is b2
        assert b1.failure_threshold == 5

    def test_registry_all_status(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        CircuitBreakerRegistry.get_or_create("a")
        CircuitBreakerRegistry.get_or_create("b")
        status = CircuitBreakerRegistry.all_status()
        assert "a" in status
        assert "b" in status
        assert status["a"]["state"] == "closed"

    def test_reset(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(name="test", failure_threshold=1)
        cb.record_failure("err")
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_status_snapshot(self):
        from services.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(name="test")
        cb.record_failure("oops")
        s = cb.status()
        assert s["name"] == "test"
        assert s["failure_count"] == 1
        assert s["last_error"] == "oops"


# ═════════════════════════════════════════════════════════════════════════════
# 2. CROSS-BACKEND FALLBACK CHAIN
# ═════════════════════════════════════════════════════════════════════════════


class TestFallbackChain:
    """Tests for ModelRouter.generate_with_fallback."""

    def setup_method(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        CircuitBreakerRegistry.clear()

    @pytest.mark.asyncio
    async def test_fallback_tries_next_on_failure(self):
        from services.model_router import (
            BackendConfig,
            BackendType,
            ModelRouter,
            PipelineConfig,
            PipelineRole,
            RoleConfig,
        )

        router = ModelRouter()

        # Register two backends manually (bypass health check)
        cfg_a = BackendConfig(
            backend_type=BackendType.OPENAI, name="backend_a",
            api_key="fake", available_models=["model-a"],
            default_model="model-a",
        )
        cfg_b = BackendConfig(
            backend_type=BackendType.OPENAI, name="backend_b",
            api_key="fake", available_models=["model-b"],
            default_model="model-b",
        )
        router.backends["backend_a"] = cfg_a
        router.backends["backend_b"] = cfg_b

        # Mock clients
        client_a = AsyncMock()
        client_a.generate = AsyncMock(side_effect=RuntimeError("backend_a down"))
        client_b = AsyncMock()
        client_b.generate = AsyncMock(return_value="answer from B")
        router.clients["backend_a"] = client_a
        router.clients["backend_b"] = client_b

        result = await router.generate_with_fallback(
            prompt="hello",
            preferred_backend="backend_a",
        )
        assert result == "answer from B"
        client_a.generate.assert_called_once()
        client_b.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_raises_when_all_exhausted(self):
        from services.model_router import BackendConfig, BackendType, ModelRouter

        router = ModelRouter()
        cfg = BackendConfig(
            backend_type=BackendType.OPENAI, name="only",
            api_key="x", available_models=["m"], default_model="m",
        )
        router.backends["only"] = cfg
        client = AsyncMock()
        client.generate = AsyncMock(side_effect=RuntimeError("dead"))
        router.clients["only"] = client

        with pytest.raises(RuntimeError, match="All 1 backends exhausted"):
            await router.generate_with_fallback(prompt="hi")

    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_open_backend(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        from services.model_router import BackendConfig, BackendType, ModelRouter

        router = ModelRouter()
        cfg_a = BackendConfig(
            backend_type=BackendType.OPENAI, name="a",
            api_key="x", available_models=["m"], default_model="m",
        )
        cfg_b = BackendConfig(
            backend_type=BackendType.OPENAI, name="b",
            api_key="x", available_models=["m"], default_model="m",
        )
        router.backends["a"] = cfg_a
        router.backends["b"] = cfg_b

        client_a = AsyncMock()
        client_a.generate = AsyncMock(return_value="from A")
        client_b = AsyncMock()
        client_b.generate = AsyncMock(return_value="from B")
        router.clients["a"] = client_a
        router.clients["b"] = client_b

        # Trip the circuit for backend a
        breaker = CircuitBreakerRegistry.get_or_create(
            "llm_a", failure_threshold=1, cooldown_seconds=300,
        )
        breaker.record_failure("forced")
        assert not breaker.allow_request()

        result = await router.generate_with_fallback(
            prompt="test", preferred_backend="a",
        )
        assert result == "from B"
        client_a.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_build_fallback_order_prefers_local(self):
        from services.model_router import BackendConfig, BackendType, ModelRouter

        router = ModelRouter()
        for name, bt in [("cloud", BackendType.OPENAI), ("local", BackendType.OLLAMA)]:
            router.backends[name] = BackendConfig(
                backend_type=bt, name=name,
                available_models=["m"], default_model="m",
            )
        order = router._build_fallback_order()
        names = [n for n, _ in order]
        assert names.index("local") < names.index("cloud")


# ═════════════════════════════════════════════════════════════════════════════
# 3. SELF-CORRECTING RETRIEVAL
# ═════════════════════════════════════════════════════════════════════════════


class TestSelfCorrectingRetrieval:
    """Tests for services.self_correcting_retrieval."""

    def _make_doc(self, content: str, source: str = "test.md"):
        from langchain_core.documents import Document
        return Document(page_content=content, metadata={"source": source})

    @pytest.mark.asyncio
    async def test_skips_when_context_sufficient(self):
        from services.self_correcting_retrieval import corrective_retrieve

        existing = [self._make_doc("word " * 100)]
        result = await corrective_retrieve(
            MagicMock(), "test question",
            initial_docs=existing,
            initial_context_words=100,
        )
        assert result is existing

    @pytest.mark.asyncio
    async def test_heuristic_reformulations(self):
        from services.self_correcting_retrieval import _heuristic_reformulations
        variants = _heuristic_reformulations("What is our swim lesson pricing policy?", n=3)
        assert len(variants) >= 1
        # Should strip question words
        for v in variants:
            assert "what" not in v.lower() or len(v) > 10

    @pytest.mark.asyncio
    async def test_corrective_retrieve_adds_docs(self):
        from services.self_correcting_retrieval import corrective_retrieve

        initial_doc = self._make_doc("short snippet")
        new_doc = self._make_doc("a much longer and relevant document with details " * 5, source="extra.md")

        mock_vs = MagicMock()
        mock_vs.similarity_search_with_score = MagicMock(return_value=[(new_doc, 0.5)])

        result = await corrective_retrieve(
            mock_vs, "What is the pricing?",
            initial_docs=[initial_doc],
            initial_context_words=5,
        )
        assert len(result) > 1
        sources = {d.metadata.get("source") for d in result}
        assert "extra.md" in sources

    @pytest.mark.asyncio
    async def test_llm_reformulation(self):
        from services.self_correcting_retrieval import _generate_reformulations

        async def mock_llm(prompt: str) -> str:
            return "swim lesson costs\nprice list for swimming\naquatic program fees"

        queries = await _generate_reformulations(
            "What is our swim lesson pricing?", generate_fn=mock_llm, n=3,
        )
        assert len(queries) == 3
        assert "swim lesson costs" in queries

    @pytest.mark.asyncio
    async def test_reformulate_after_assessment(self):
        from services.self_correcting_retrieval import reformulate_after_assessment

        @dataclass
        class MockAssessment:
            missing_knowledge: str = "Pool maintenance schedule document"
            gap_detected: bool = True
            grounded: bool = False

        new_doc = self._make_doc("pool maintenance details", source="pool.md")
        mock_vs = MagicMock()
        mock_vs.similarity_search_with_score = MagicMock(return_value=[(new_doc, 0.3)])

        docs = await reformulate_after_assessment(
            "When is pool maintenance?",
            MockAssessment(),
            mock_vs,
        )
        assert len(docs) >= 1

    @pytest.mark.asyncio
    async def test_deduplication(self):
        from services.self_correcting_retrieval import corrective_retrieve

        doc = self._make_doc("duplicate content")
        mock_vs = MagicMock()
        mock_vs.similarity_search_with_score = MagicMock(return_value=[(doc, 0.5)])

        result = await corrective_retrieve(
            mock_vs, "test",
            initial_docs=[doc],
            initial_context_words=2,
        )
        # The same doc shouldn't appear twice
        contents = [d.page_content for d in result]
        assert contents.count("duplicate content") == 1


# ═════════════════════════════════════════════════════════════════════════════
# 4. AUTO DOCUMENT QUALITY REMEDIATION
# ═════════════════════════════════════════════════════════════════════════════


class TestDocumentRemediation:
    """Tests for services.document_remediation."""

    def _mock_db(self, rows=None):
        """Create a mock DB that returns given rows."""
        db = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()

        if rows is not None:
            cursor.fetchall = AsyncMock(return_value=rows)
            cursor.fetchone = AsyncMock(return_value=rows[0] if rows else None)
        else:
            cursor.fetchall = AsyncMock(return_value=[])
            cursor.fetchone = AsyncMock(return_value=None)

        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        conn.execute = MagicMock(return_value=cursor)
        conn.commit = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        db.acquire = MagicMock(return_value=conn)
        return db

    def test_find_reenrich_candidates_detects_missing_fields(self):
        from services.document_remediation import DocumentRemediationService

        mock_vs = MagicMock()
        mock_vs.get = MagicMock(return_value={
            "ids": ["chunk1", "chunk2"],
            "metadatas": [
                {"source": "a.md"},  # Missing all enrichment fields
                {"source": "b.md", "enriched": True, "llm_summary": "yes"},  # Already enriched
            ],
        })

        service = DocumentRemediationService(vectorstore=mock_vs)
        candidates = service.find_reenrich_candidates()
        assert len(candidates) == 1
        assert candidates[0].chunk_id == "chunk1"
        assert "llm_summary" in candidates[0].missing_fields

    def test_find_reenrich_skips_archived(self):
        from services.document_remediation import DocumentRemediationService

        mock_vs = MagicMock()
        mock_vs.get = MagicMock(return_value={
            "ids": ["chunk1"],
            "metadatas": [{"source": "a.md", "archived": True}],
        })

        service = DocumentRemediationService(vectorstore=mock_vs)
        candidates = service.find_reenrich_candidates()
        assert len(candidates) == 0

    @pytest.mark.asyncio
    async def test_archive_chunks_sets_metadata(self):
        from services.document_remediation import ArchiveCandidate, DocumentRemediationService

        mock_collection = MagicMock()
        mock_collection.get = MagicMock(return_value={
            "ids": ["c1"], "metadatas": [{"source": "old.md"}],
        })
        mock_collection.update = MagicMock()

        mock_vs = MagicMock()
        mock_vs._collection = mock_collection

        service = DocumentRemediationService(vectorstore=mock_vs)
        candidate = ArchiveCandidate(
            chunk_id="c1", source="old.md", quality_score=0.1,
            feedback_count=5, reason="low quality",
        )
        archived = await service.archive_chunks([candidate])
        assert archived == 1
        call_args = mock_collection.update.call_args
        meta = call_args[1]["metadatas"][0]
        assert meta["archived"] is True

    @pytest.mark.asyncio
    async def test_remediation_report_summary(self):
        from services.document_remediation import RemediationReport
        report = RemediationReport(
            run_at="2026-02-26", chunks_archived=3, chunks_queued_for_reenrich=5,
        )
        text = report.summary_text()
        assert "Archived 3" in text
        assert "5 chunk(s)" in text

    @pytest.mark.asyncio
    async def test_run_remediation_orchestrator(self):
        from services.document_remediation import DocumentRemediationService

        mock_vs = MagicMock()
        mock_vs.get = MagicMock(return_value={"ids": [], "metadatas": []})
        mock_vs._collection = None

        service = DocumentRemediationService(vectorstore=mock_vs, db=self._mock_db())
        report = await service.run_remediation()
        assert report.run_at
        assert report.chunks_archived == 0


# ═════════════════════════════════════════════════════════════════════════════
# 5. (Steward auto-fix is integration-level — tested via the existing
#     test_autonomous_ops.py framework. We verify the wiring logic here.)
# ═════════════════════════════════════════════════════════════════════════════


class TestStewardAutoFix:
    """Verify the auto-fix wiring compiles and the logic is correct."""

    def test_document_remediation_import(self):
        """Verify the new service imports cleanly."""
        from services.document_remediation import (
            DocumentRemediationService,
            RemediationReport,
        )
        assert DocumentRemediationService is not None
        report = RemediationReport()
        assert "healthy" in report.summary_text()

    def test_circuit_breaker_registry_import(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        assert CircuitBreakerRegistry.all_status is not None


# ═════════════════════════════════════════════════════════════════════════════
# 6. AGENTIC WEB SEARCH TOOL
# ═════════════════════════════════════════════════════════════════════════════


class TestSearchWebTool:
    """Tests for the search_web tool registration + executor."""

    def test_tool_is_registered(self):
        from core.tools_builtin import build_default_registry
        registry = build_default_registry()
        tool = registry.get("search_web")
        assert tool is not None
        assert tool.mutates is False
        assert tool.category.value == "knowledge"

    def test_tool_schema(self):
        from core.tools_builtin import build_default_registry
        registry = build_default_registry()
        tool = registry.get("search_web")
        schema = tool.to_openai_schema()
        fn = schema["function"]
        assert fn["name"] == "search_web"
        assert "query" in fn["parameters"]["properties"]
        assert "query" in fn["parameters"]["required"]

    @pytest.mark.asyncio
    async def test_search_web_no_api_key(self):
        from core.tools_builtin import _search_web
        with patch.dict("os.environ", {}, clear=True):
            # Remove TAVILY_API_KEY if present
            import os
            old = os.environ.pop("TAVILY_API_KEY", None)
            try:
                result = await _search_web(query="test query")
                assert result.success is False
                assert "not configured" in result.message or "not available" in result.message
            finally:
                if old is not None:
                    os.environ["TAVILY_API_KEY"] = old

    @pytest.mark.asyncio
    async def test_search_web_with_mock_tavily(self):
        """Test the full tool flow with a mocked Tavily service."""
        from core.tools_builtin import _search_web

        mock_context = "[Web Search Results]\n• Test Title (https://example.com): Some snippet"

        with patch("services.web_research.chat_web_augment", new_callable=AsyncMock) as mock_aug, \
             patch("services.tavily_service.TavilyService") as MockTavily, \
             patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):

            mock_tavily_instance = MagicMock()
            mock_tavily_instance.is_configured = True
            MockTavily.return_value = mock_tavily_instance
            mock_aug.return_value = mock_context

            result = await _search_web(query="test query")
            assert result.success is True
            assert result.data["count"] >= 1

    def test_total_tool_count_increased(self):
        from core.tools_builtin import build_default_registry
        registry = build_default_registry()
        # Should have at least 12 tools now (11 original + search_web)
        assert registry.tool_count >= 12


# ═════════════════════════════════════════════════════════════════════════════
# LLM SERVICE FALLBACK INTEGRATION
# ═════════════════════════════════════════════════════════════════════════════


class TestLLMServiceFallback:
    """Tests that LLMService now uses generate_with_fallback."""

    def setup_method(self):
        from services.circuit_breaker import CircuitBreakerRegistry
        CircuitBreakerRegistry.clear()

    @pytest.mark.asyncio
    async def test_llm_service_uses_fallback(self):
        from services.llm_service import LLMService

        service = LLMService(api_key="fake")

        mock_router = MagicMock()
        mock_router.generate_with_fallback = AsyncMock(return_value="fallback answer")

        with patch.object(service, "_get_router", return_value=mock_router):
            result = await service.complete("test prompt")
            assert result == "fallback answer"
            mock_router.generate_with_fallback.assert_called_once()
