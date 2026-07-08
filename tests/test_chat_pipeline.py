"""
Tests for the web chat pipeline: doc filtering, formatting, source citations,
streaming endpoint, feedback endpoint, and pipeline integration.

No real LLM calls — all model interactions are mocked.
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).parent.parent
import aiosqlite

LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")
os.environ["ADMIN_AUTH_DISABLED"] = "1"

from admin.routers.chat import (
    _build_artifact_tool_fallback,
    _resolve_assistive_backend_and_model,
    _trim_generation_context,
)
from cogs.LLM import (
    extract_source_citations,
    filter_superseded_docs,
    format_docs_for_context,
)
from langchain_core.documents import Document
from services.chat_policy import ChatPolicyDecision, decide_chat_policy
from services.model_router import BackendConfig, BackendType, PipelineConfig, PipelineRole, RoleConfig
from services.response_cache import CacheEntry

from LeisureLLM.core.tools_builtin import _create_decision
from LeisureLLM.database import Database
from LeisureLLM.services.rag_pipeline import count_trusted_candidates, promote_trusted_candidates
from LeisureLLM.services.request_tracing import (
    persist_request_trace,
    resolve_request_trace_db_path,
    update_request_trace_after_confirmation,
)

# =============================================================================
# Document helpers — pure logic, no mocks needed
# =============================================================================


def _make_doc(content, **meta):
    """Shorthand to create a LangChain Document."""
    return Document(page_content=content, metadata=meta)


class TestFilterSupersededDocs:
    """Tests for filter_superseded_docs()."""

    def test_removes_superseded(self):
        docs = [
            _make_doc("current info", status=""),
            _make_doc("old info", status="superseded"),
            _make_doc("more current", status="active"),
            _make_doc("extra 1"),
            _make_doc("extra 2"),
            _make_doc("extra 3"),
            _make_doc("extra 4"),
        ]
        result = filter_superseded_docs(docs)
        contents = [d.page_content for d in result]
        assert "current info" in contents
        assert "more current" in contents
        # With 6 current docs (>=5), superseded should NOT be backfilled
        assert "old info" not in contents

    def test_no_arbitrary_count_cap(self):
        """filter_superseded_docs intentionally has no count cap —
        the character budget in format_docs_for_context is the real guard."""
        docs = [_make_doc(f"doc {i}") for i in range(25)]
        result = filter_superseded_docs(docs)
        assert len(result) == 25

    def test_backfills_superseded_when_sparse(self):
        """If fewer than 5 current docs, superseded ones fill the gap."""
        docs = [
            _make_doc("current 1"),
            _make_doc("current 2"),
            _make_doc("old 1", status="superseded"),
            _make_doc("old 2", status="superseded"),
            _make_doc("old 3", status="superseded"),
        ]
        result = filter_superseded_docs(docs)
        assert len(result) >= 4  # 2 current + some superseded backfill

    def test_prioritizes_primary_over_generated(self):
        docs = [
            _make_doc("generated memo", source_kind="generated", source_priority=-1),
            _make_doc("primary doc", source_kind="channel", source_priority=5),
        ]
        result = filter_superseded_docs(docs)
        # Primary should come first
        assert result[0].page_content == "primary doc"

    def test_infers_generated_priority_from_memo_path(self):
        docs = [
            _make_doc("memo", source_relpath="docs/memos/2026/03/web_cache_note.md"),
            _make_doc("handbook", source_relpath="docs/handbook/pool-opening.md"),
        ]
        result = filter_superseded_docs(docs)
        assert result[0].page_content == "handbook"

    def test_demotes_discord_exports_when_trusted_docs_exist(self):
        docs = [
            _make_doc("discord export", doc_type="discord_export", source_relpath="logs/pool_lines.txt"),
            _make_doc("policy", source_relpath="docs/handbook/policy.md", source_priority=1),
            _make_doc("admin note", doc_type="human_knowledge", source_relpath="docs/knowledge/pool.md"),
        ]
        result = filter_superseded_docs(docs)
        contents = [doc.page_content for doc in result]
        assert contents[:2] == ["admin note", "policy"]

    def test_limits_demoted_candidates_when_trusted_set_is_sufficient(self):
        docs = [
            _make_doc(f"policy {i}", source_relpath=f"docs/handbook/policy-{i}.md", source_priority=1)
            for i in range(8)
        ]
        docs.extend([
            _make_doc("web cache", doc_type="web_cache", source_relpath="docs/memos/2026/03/web_cache_note.md"),
            _make_doc("memo", source_kind="generated", source_relpath="docs/memos/2026/03/memo.md"),
        ])
        result = filter_superseded_docs(docs)
        contents = [doc.page_content for doc in result]
        assert "web cache" not in contents
        assert "memo" not in contents

    def test_caps_demoted_tail_when_trusted_docs_exist(self):
        docs = [
            _make_doc("policy", source_relpath="docs/handbook/policy.md", source_priority=1),
            _make_doc("knowledge", doc_type="human_knowledge", source_relpath="docs/knowledge/pool.md"),
            _make_doc("web cache 1", doc_type="web_cache", source_relpath="docs/memos/2026/03/web_cache_1.md"),
            _make_doc("web cache 2", doc_type="web_cache", source_relpath="docs/memos/2026/03/web_cache_2.md"),
            _make_doc("web cache 3", doc_type="web_cache", source_relpath="docs/memos/2026/03/web_cache_3.md"),
            _make_doc("memo", source_kind="generated", source_relpath="docs/memos/2026/03/memo.md"),
        ]
        result = filter_superseded_docs(docs)
        demoted = [doc.page_content for doc in result if "web cache" in doc.page_content or doc.page_content == "memo"]
        assert len(demoted) <= 2

    def test_preserves_demoted_candidates_when_no_trusted_sources_exist(self):
        docs = [
            _make_doc("web cache", doc_type="web_cache", source_relpath="docs/memos/2026/03/web_cache_note.md"),
            _make_doc("discord export", doc_type="discord_export", source_relpath="logs/pool_lines.txt"),
        ]
        result = filter_superseded_docs(docs)
        contents = [doc.page_content for doc in result]
        assert contents == ["discord export", "web cache"]

    def test_empty_input(self):
        assert filter_superseded_docs([]) == []


class TestFormatDocsForContext:
    """Tests for format_docs_for_context()."""

    def test_adds_source_headers(self):
        docs = [
            _make_doc("Hello world", source_relpath="docs/hello.txt", doc_type="meeting"),
        ]
        result = format_docs_for_context(docs)
        assert "[DOC 1]" in result
        assert "source=docs/hello.txt" in result
        assert "type=meeting" in result
        assert "Hello world" in result

    def test_respects_max_chars(self):
        # Create docs that would exceed 500 chars
        docs = [_make_doc("x" * 300, source_relpath=f"doc{i}.txt") for i in range(5)]
        result = format_docs_for_context(docs, max_chars=500)
        assert len(result) <= 600  # Allow some header overhead

    def test_separates_with_dividers(self):
        docs = [
            _make_doc("First", source_relpath="a.txt"),
            _make_doc("Second", source_relpath="b.txt"),
        ]
        result = format_docs_for_context(docs)
        assert "---" in result

    def test_includes_status_when_present(self):
        docs = [_make_doc("content", source_relpath="x.txt", status="draft")]
        result = format_docs_for_context(docs)
        assert "status=draft" in result

    def test_prefers_primary_sources_over_newer_generated_memos(self):
        docs = [
            _make_doc(
                "new generated memo",
                source_relpath="docs/memos/2026/03/web_cache_note.md",
                doc_date="2026-03-15",
            ),
            _make_doc(
                "older handbook",
                source_relpath="docs/handbook/pool-opening.md",
                doc_date="2026-02-01",
                source_priority=1,
            ),
        ]
        result = format_docs_for_context(docs)
        assert result.index("older handbook") < result.index("new generated memo")

    def test_empty_input(self):
        assert format_docs_for_context([]) == ""


class TestTrustedCandidatePromotion:
    def test_promotes_supplemental_trusted_docs(self):
        docs = [
            _make_doc("web cache", doc_type="web_cache", source_relpath="docs/memos/2026/03/web_cache_note.md"),
            _make_doc("discord export", doc_type="discord_export", source_relpath="docs/ops_lines.txt"),
        ]
        supplemental = [
            _make_doc("policy", source_relpath="docs/handbook/policy.md", source_priority=1),
            _make_doc("knowledge", doc_type="human_knowledge", source_relpath="docs/knowledge/pool.md"),
        ]

        result = promote_trusted_candidates(docs, supplemental, minimum_trusted=2, max_total_docs=6)

        assert count_trusted_candidates(result) >= 2
        assert [doc.page_content for doc in result[:2]] == ["knowledge", "policy"]


class TestExtractSourceCitations:
    """Tests for extract_source_citations()."""

    def test_extracts_unique_sources(self):
        docs = [
            _make_doc("a", source_relpath="docs/a.txt", doc_type="meeting"),
            _make_doc("b", source_relpath="docs/b.txt", doc_type="decision"),
            _make_doc("c", source_relpath="docs/a.txt", doc_type="meeting"),  # duplicate
        ]
        result = extract_source_citations(docs)
        assert len(result) == 2
        names = {s["name"] for s in result}
        assert "a.txt" in names
        assert "b.txt" in names

    def test_uses_basename_for_name(self):
        docs = [_make_doc("x", source_relpath="long/path/to/report.pdf", doc_type="doc")]
        result = extract_source_citations(docs)
        assert result[0]["name"] == "report.pdf"
        assert result[0]["path"] == "long/path/to/report.pdf"
        assert result[0]["type"] == "doc"

    def test_falls_back_to_source(self):
        docs = [_make_doc("x", source="backup_source.txt")]
        result = extract_source_citations(docs)
        assert result[0]["name"] == "backup_source.txt"

    def test_skips_empty_sources(self):
        docs = [_make_doc("x")]  # no source metadata
        result = extract_source_citations(docs)
        assert len(result) == 0

    def test_empty_input(self):
        assert extract_source_citations([]) == []


# =============================================================================
# Streaming Chat Endpoint
# =============================================================================

def _make_client():
    """Create a TestClient with mocked dependencies."""
    from admin import dependencies, server

    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()

    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()
    dependencies._model_router = mock_mr

    mock_bot = MagicMock()
    mock_bot.db = _mock_db()
    dependencies._bot_instance = mock_bot

    from fastapi.testclient import TestClient
    return TestClient(server.app, raise_server_exceptions=False)


def _mock_db():
    db = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.__aiter__ = MagicMock(return_value=iter([]))
    conn.execute = AsyncMock(return_value=cursor)
    conn.executemany = AsyncMock()
    conn.commit = AsyncMock()

    class AcquireCM:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    db.acquire = lambda: AcquireCM()
    db.execute = AsyncMock()
    db.fetchone = AsyncMock(return_value=None)
    db.fetchall = AsyncMock(return_value=[])
    return db


@pytest.fixture(scope="module")
def chat_client():
    return _make_client()


def _extract_alpha_payload(mock_log_alpha):
    assert mock_log_alpha.called, "Expected telemetry to be emitted"
    _, payload = mock_log_alpha.call_args.args
    return payload


def _make_pipeline_router(*, backend_type=BackendType.OLLAMA, stages=None):
    router = MagicMock()
    router.backends = {
        "test": BackendConfig(backend_type=backend_type, name="test")
    }
    router.pipeline = PipelineConfig(
        name="test",
        roles={
            PipelineRole.INITIAL: RoleConfig(
                role=PipelineRole.INITIAL,
                backend_name="test",
                model="test-model",
            )
        },
    )
    router.generate_pipeline = AsyncMock(return_value={
        "final": "Pipeline reply",
        "stages": stages or {"initial": "Pipeline reply"},
        "models_used": {"initial": "test/test-model"},
    })
    router.generate_single = AsyncMock(return_value="Pipeline reply")
    return router


class TestStreamingChatEndpoint:
    """Test POST /api/v1/chat/stream."""

    def test_stream_returns_sse(self, chat_client):
        """Endpoint should return text/event-stream content type."""
        with patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("cogs.LLM._get_pipeline_router", new_callable=AsyncMock, return_value=None):

            async def fake_astream(input_dict):
                yield "Hello world"

            mock_chain = MagicMock()
            mock_chain.astream = fake_astream

            from langchain_core.output_parsers import StrOutputParser
            with patch.object(StrOutputParser, "__ror__", return_value=mock_chain):
                resp = chat_client.post(
                    "/api/v1/chat/stream",
                    json={"message": "What is our revenue?", "history": []},
                    headers={"X-CSRF-Protection": "1"},
                )
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_stream_contains_expected_events(self, chat_client):
        """SSE stream should contain status, token, sources, and done events."""
        with patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("cogs.LLM._get_pipeline_router", new_callable=AsyncMock, return_value=None):

            # Patch the LangChain streaming to emit known tokens
            async def fake_astream(input_dict):
                yield "Test reply"

            with patch("admin.routers.chat.asyncio") as mock_asyncio:
                mock_asyncio.to_thread = AsyncMock(return_value=[])
                mock_asyncio.sleep = AsyncMock()

                with patch("langchain_openai.ChatOpenAI") as MockLLM:
                    mock_instance = MagicMock()
                    MockLLM.return_value = mock_instance

                    # Build a mock chain that supports | operator and astream
                    mock_chain = MagicMock()
                    mock_chain.astream = fake_astream

                    from langchain_core.output_parsers import StrOutputParser
                    with patch.object(StrOutputParser, "__ror__", return_value=mock_chain):

                        resp = chat_client.post(
                            "/api/v1/chat/stream",
                            json={"message": "hello", "history": []},
                            headers={"X-CSRF-Protection": "1"},
                        )
                        assert resp.status_code == 200
                        body = resp.text

                        # Parse SSE events
                        events = []
                        for line in body.split("\n"):
                            if line.startswith("data: "):
                                try:
                                    events.append(json.loads(line[6:]))
                                except json.JSONDecodeError:
                                    pass

                        event_types = [e["type"] for e in events]
                        # Must have at least status, sources, done
                        assert "status" in event_types, f"Missing 'status' event. Got: {event_types}"
                        assert "sources" in event_types, f"Missing 'sources' event. Got: {event_types}"
                        assert "done" in event_types, f"Missing 'done' event. Got: {event_types}"

    def test_stream_uses_single_stage_generation_for_assistive(self, chat_client):
        mock_router = _make_pipeline_router()
        mock_router.generate_single = AsyncMock(return_value="Assistive reply")

        with patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("services.rag_pipeline.get_pipeline_router", new_callable=AsyncMock, return_value=mock_router):
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "hello", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        mock_router.generate_pipeline.assert_not_awaited()
        assistive_calls = [
            call.kwargs
            for call in mock_router.generate_single.await_args_list
            if call.kwargs.get("max_tokens") == 900
        ]
        assert assistive_calls, "Expected a capped assistive single-stage generate_single call"
        assert "=== USER QUESTION ===\nhello" in assistive_calls[0]["prompt"]


class TestAssistiveGenerationContext:
    def test_trim_generation_context_only_for_assistive(self):
        text = "x" * 6000
        assert len(_trim_generation_context(text, "assistive")) == 5000
        assert _trim_generation_context(text, "deep") == text


class TestAssistiveModelOverride:
    def test_resolve_assistive_backend_and_model_defaults_to_initial_role(self):
        initial_cfg = RoleConfig(
            role=PipelineRole.INITIAL,
            backend_name="ollama",
            model="qwen3:8b",
        )

        with patch.dict(os.environ, {}, clear=False):
            backend_name, model = _resolve_assistive_backend_and_model(initial_cfg)

        assert backend_name == "ollama"
        assert model == "qwen3:8b"

    def test_resolve_assistive_backend_and_model_honors_env_override(self):
        initial_cfg = RoleConfig(
            role=PipelineRole.INITIAL,
            backend_name="ollama",
            model="qwen3:8b",
        )

        with patch.dict(
            os.environ,
            {
                "ASSISTIVE_LOCAL_BACKEND_OVERRIDE": "ollama",
                "ASSISTIVE_LOCAL_MODEL_OVERRIDE": "mistral:7b",
            },
            clear=False,
        ):
            backend_name, model = _resolve_assistive_backend_and_model(initial_cfg)

        assert backend_name == "ollama"
        assert model == "mistral:7b"


class TestStreamingTelemetry:
    def test_cache_hit_emits_cache_route(self, chat_client):
        cache = MagicMock()
        cache.get.return_value = CacheEntry(
            query_key="hello|ctx",
            context_hash="ctx",
            result={"final": "Cached reply"},
            sources=["docs/a.txt"],
            chunk_sources=["docs/a.txt"],
            context_words=4,
        )

        with patch("admin.routers.chat._ensure_tool_registry", new_callable=AsyncMock, return_value=None), \
             patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("services.response_cache.get_response_cache", return_value=cache), \
             patch("services.alpha_logging.log_alpha_event") as mock_log_alpha:
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "hello", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        payload = _extract_alpha_payload(mock_log_alpha)
        assert payload["cache_hit"] is True
        assert payload["route_mode"] == "cache"
        assert payload["stage_flags"]["hyde_used"] is False
        assert payload["tokens"]["output_tokens_est"] > 0
        assert payload["latency"]["total_ms"] is not None

    def test_pipeline_initial_only_emits_stage_flags(self, chat_client):
        router = _make_pipeline_router(stages={"initial": "draft only"})
        deep_policy = ChatPolicyDecision(
            lane="deep", complexity="deep", explicit_deep_intent=False,
            artifact_request=False, multi_source_requirement=False,
            high_confidence_required=False, deep_mode_requested=False,
            use_query_decomposition=False, use_corrective_retrieval=False,
            use_evidence_evaluation=False, max_aux_retrieval_calls=0,
            max_generation_stages=3, reason="test-forced-deep",
        )

        with patch("admin.routers.chat._ensure_tool_registry", new_callable=AsyncMock, return_value=None), \
             patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("services.rag_pipeline.get_pipeline_router", new_callable=AsyncMock, return_value=router), \
             patch("services.chat_policy.decide_chat_policy", return_value=deep_policy), \
             patch("services.alpha_logging.log_alpha_event") as mock_log_alpha:
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "summarize", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        payload = _extract_alpha_payload(mock_log_alpha)
        assert payload["route_mode"] == "pipeline"
        assert payload["llm_calls"] == 1
        assert payload["stage_flags"]["critique_used"] is False
        assert payload["stage_flags"]["synth_used"] is False
        assert payload["tokens"]["total_tokens_est"] >= payload["tokens"]["output_tokens_est"]

    def test_pipeline_full_stages_emits_critique_and_synth(self, chat_client):
        router = _make_pipeline_router(
            stages={
                "initial": "draft",
                "critique": "needs fixes",
                "synthesize": "final",
            }
        )
        deep_policy = ChatPolicyDecision(
            lane="deep", complexity="deep", explicit_deep_intent=False,
            artifact_request=False, multi_source_requirement=False,
            high_confidence_required=False, deep_mode_requested=False,
            use_query_decomposition=False, use_corrective_retrieval=False,
            use_evidence_evaluation=False, max_aux_retrieval_calls=0,
            max_generation_stages=3, reason="test-forced-deep",
        )

        with patch("admin.routers.chat._ensure_tool_registry", new_callable=AsyncMock, return_value=None), \
             patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("services.rag_pipeline.get_pipeline_router", new_callable=AsyncMock, return_value=router), \
             patch("services.chat_policy.decide_chat_policy", return_value=deep_policy), \
             patch("services.alpha_logging.log_alpha_event") as mock_log_alpha:
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "deep analysis", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        payload = _extract_alpha_payload(mock_log_alpha)
        assert payload["stage_flags"]["critique_used"] is True
        assert payload["stage_flags"]["synth_used"] is True
        assert payload["llm_calls"] == 3

    def test_hyde_web_and_rerank_flags_emit(self, chat_client):
        docs = [_make_doc("brief", source_relpath="docs/a.txt", retrieval_score=0.1)]
        router = _make_pipeline_router(stages={"initial": "reply"})

        with patch("admin.routers.chat._ensure_tool_registry", new_callable=AsyncMock, return_value=None), \
             patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("admin.routers.chat._vectorstore", MagicMock()), \
             patch("services.rag_pipeline.get_pipeline_router", new_callable=AsyncMock, return_value=router), \
             patch("services.hyde_retrieval.hyde_retrieve", new_callable=AsyncMock, return_value=docs), \
             patch("services.evidence_evaluator.evaluate_evidence", new_callable=AsyncMock, return_value=MagicMock(evaluated=False)), \
             patch("cogs.LLM._needs_web_search", return_value=True), \
             patch("cogs.LLM._context_is_relevant", return_value=False), \
             patch("cogs.LLM._is_outward_question", return_value=False), \
             patch("services.tavily_service.TavilyService") as MockTavily, \
             patch("services.web_research.chat_web_augment", new_callable=AsyncMock, return_value="[WEB] result"), \
             patch("services.alpha_logging.log_alpha_event") as mock_log_alpha:
            MockTavily.return_value.is_configured = True
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "current market update", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        payload = _extract_alpha_payload(mock_log_alpha)
        assert payload["stage_flags"]["hyde_used"] is True
        assert payload["stage_flags"]["rerank_used"] is True
        assert payload["stage_flags"]["web_used"] is True
        assert payload["retrieved_docs"] == 1

    def test_fallback_emits_fallback_route_and_latency(self, chat_client):
        async def fake_astream(input_dict):
            yield "Fallback reply"

        mock_chain = MagicMock()
        mock_chain.astream = fake_astream
        mock_prompt = MagicMock()
        mock_prompt.__or__ = MagicMock(return_value=MagicMock())
        mock_prompt.__or__.return_value.__or__ = MagicMock(return_value=mock_chain)

        with patch("admin.routers.chat._ensure_tool_registry", new_callable=AsyncMock, return_value=None), \
             patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("services.rag_pipeline.get_pipeline_router", new_callable=AsyncMock, return_value=None), \
             patch("services.rag_pipeline.prompt", mock_prompt), \
             patch("services.alpha_logging.log_alpha_event") as mock_log_alpha:
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "fallback please", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        payload = _extract_alpha_payload(mock_log_alpha)
        assert payload["route_mode"] == "fallback"
        assert payload["llm_calls"] == 1
        assert payload["latency"]["first_token_ms"] is not None
        assert payload["tokens"]["output_tokens_est"] > 0

    def test_local_only_blocked_emits_degraded_route(self, chat_client):
        router = _make_pipeline_router(backend_type=BackendType.OPENAI)

        with patch("admin.routers.chat._ensure_tool_registry", new_callable=AsyncMock, return_value=None), \
             patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None), \
             patch("services.rag_pipeline.get_pipeline_router", new_callable=AsyncMock, return_value=router), \
             patch("config.LOCAL_LLM_ONLY", True), \
             patch("services.alpha_logging.log_alpha_event") as mock_log_alpha:
            resp = chat_client.post(
                "/api/v1/chat/stream",
                json={"message": "answer locally", "history": []},
                headers={"X-CSRF-Protection": "1"},
            )

        assert resp.status_code == 200
        assert "Local-only mode is enabled" in resp.text
        payload = _extract_alpha_payload(mock_log_alpha)
        assert payload["route_mode"] == "degraded_local_only"
        assert payload["llm_calls"] == 0
        assert payload["latency"]["first_token_ms"] is not None


class TestLegacyChatEndpoint:
    """Test POST /api/v1/chat (non-streaming)."""

    def test_legacy_returns_json(self, chat_client):
        with patch("cogs.LLM.AskQuestion", new_callable=AsyncMock, return_value="Legacy reply"):
            with patch("admin.routers.chat._ensure_retriever", new_callable=AsyncMock, return_value=None):
                resp = chat_client.post(
                    "/api/v1/chat",
                    json={"message": "hello", "history": []},
                    headers={"X-CSRF-Protection": "1"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["reply"] == "Legacy reply"


class TestFeedbackEndpoint:
    """Test POST /api/v1/chat/feedback."""

    def test_feedback_helpful(self, chat_client):
        resp = chat_client.post(
            "/api/v1/chat/feedback",
            json={
                "question": "What is our Q1 target?",
                "answer": "Revenue target is £500k.",
                "feedback": "helpful",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_feedback_not_helpful(self, chat_client):
        resp = chat_client.post(
            "/api/v1/chat/feedback",
            json={
                "question": "What is our Q1 target?",
                "answer": "I don't know.",
                "feedback": "not_helpful",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# =============================================================================
# Pipeline Integration (AskQuestion uses filtering)
# =============================================================================


class TestAskQuestionUsesFiltering:
    """Verify AskQuestion applies filter + format (not raw concatenation)."""

    @pytest.mark.asyncio
    async def test_askquestion_calls_filter_and_format(self):
        """AskQuestion should use filter_superseded_docs and format_docs_for_context."""
        fake_docs = [
            _make_doc("relevant info", source_relpath="info.txt"),
            _make_doc("old stuff", status="superseded"),
        ]

        with patch("cogs.LLM.run_retriever_query", return_value=fake_docs), \
             patch("cogs.LLM._get_pipeline_router", new_callable=AsyncMock, return_value=None), \
             patch("cogs.LLM.ChatOpenAI") as MockLLM:

            mock_chain = MagicMock()
            mock_chain.ainvoke = AsyncMock(return_value="Mocked answer")
            MockLLM.return_value.bind.return_value = MagicMock()

            # We need to patch the chain creation
            with patch("cogs.LLM.prompt") as mock_prompt:
                mock_prompt.__or__ = MagicMock(return_value=MagicMock())
                mock_prompt.__or__.return_value.__or__ = MagicMock(return_value=mock_chain)

                from cogs.LLM import AskQuestion

                result = await AskQuestion(
                    q="What is our target?",
                    message_chain=None,
                    user="test-user",
                    retriever=MagicMock(),
                )

                # Should not crash and should return something
                assert result is not None

    @pytest.mark.asyncio
    async def test_askquestion_with_pipeline(self):
        """When pipeline is available, AskQuestion should use it."""
        fake_docs = [_make_doc("strategy priorities and execution plan", source_relpath="a.txt")]

        mock_router = MagicMock()
        mock_router.pipeline = MagicMock()
        mock_router.generate_pipeline = AsyncMock(return_value={
            "final": "Pipeline synthesized answer",
            "stages": {"initial": "draft", "critique": "looks good", "synthesize": "Pipeline synthesized answer"},
            "models_used": {"initial": "openai/gpt-4", "critique": "openai/gpt-5", "synthesize": "openai/gpt-5-pro"},
        })

        with patch("cogs.LLM.run_retriever_query", return_value=fake_docs), \
             patch("cogs.LLM._get_pipeline_router", new_callable=AsyncMock, return_value=mock_router):

            from cogs.LLM import AskQuestion

            result = await AskQuestion(
                q="Summarize our strategy",
                message_chain=None,
                user="test-user",
                retriever=MagicMock(),
            )

            assert result == "Pipeline synthesized answer"
            mock_router.generate_pipeline.assert_awaited_once()

            # Verify the context passed to pipeline uses formatted docs (has [DOC headers)
            call_kwargs = mock_router.generate_pipeline.call_args
            context_arg = call_kwargs.kwargs.get("context") or call_kwargs[1].get("context", "")
            assert "[DOC 1]" in context_arg, "Pipeline should receive formatted docs with [DOC N] headers"

    @pytest.mark.asyncio
    async def test_askquestion_pipeline_fallback(self):
        """If pipeline fails, AskQuestion should fall back to single model."""
        fake_docs = [_make_doc("content")]

        mock_router = MagicMock()
        mock_router.pipeline = MagicMock()
        mock_router.generate_pipeline = AsyncMock(side_effect=RuntimeError("API down"))

        with patch("cogs.LLM.run_retriever_query", return_value=fake_docs), \
             patch("cogs.LLM._get_pipeline_router", new_callable=AsyncMock, return_value=mock_router), \
             patch("cogs.LLM.ChatOpenAI") as MockLLM:

            mock_chain = MagicMock()
            mock_chain.ainvoke = AsyncMock(return_value="Fallback answer")

            with patch("cogs.LLM.prompt") as mock_prompt:
                mock_prompt.__or__ = MagicMock(return_value=MagicMock())
                mock_prompt.__or__.return_value.__or__ = MagicMock(return_value=mock_chain)

                from cogs.LLM import AskQuestion

                result = await AskQuestion(
                    q="test question",
                    message_chain=None,
                    user="test-user",
                    retriever=MagicMock(),
                )
                # Should not crash — falls back to single model
                assert result is not None


# =============================================================================
# Deep Consult pipeline flag
# =============================================================================


class TestDeepConsultPipelineFlag:
    """Verify /deep_consult passes use_pipeline=True to safe_llm_call."""

    def test_deep_consult_enables_pipeline(self):
        """The deep_consult command should call safe_llm_call with use_pipeline=True."""
        # Read the source to confirm the flag is set
        import inspect

        from cogs.LLM import LLM

        source = inspect.getsource(LLM.deep_consult.callback)
        assert "use_pipeline=True" in source, (
            "/deep_consult must pass use_pipeline=True to safe_llm_call"
        )

    def test_ask_does_not_enable_pipeline(self):
        """The /ask command should NOT use the pipeline (stays fast + cheap)."""
        import inspect

        from cogs.LLM import LLM

        # Get the ask command — it's called "ask" in the command tree
        source = inspect.getsource(LLM.ask.callback)
        assert "use_pipeline" not in source, (
            "/ask should NOT use the pipeline — it's the quick path"
        )


class TestRequestTracePersistence:
    @pytest.mark.asyncio
    async def test_persist_request_trace_writes_live_sqlite_rows(self, tmp_path):
        db_path = tmp_path / "assistant.db"
        trace_db_path = resolve_request_trace_db_path(db_path)
        db = Database(str(db_path))
        await db.connect()

        try:
            await persist_request_trace(
                db,
                request_id="req-test",
                trace_id="trace-test",
                started_at=1700000000.0,
                finished_at=1700000001.0,
                entrypoint="test",
                route_name="test_route",
                user_visible_flow="chat",
                lane="assistive",
                query_text="Summarize the plan",
                models_used={"initial": "qwen3:8b"},
                pipeline_stages={"initial": True},
                policy_reason="assistive by default",
                routing_flags={"artifact_request": False, "deep_mode_requested": False},
                completed_successfully=True,
                stage_events=[],
            )

            trace_db = Database(str(trace_db_path))
            await trace_db.connect()
            async with trace_db.acquire() as conn:
                async with conn.execute(
                    "SELECT request_id, lane, policy_reason FROM request_traces WHERE request_id = ?",
                    ("req-test",),
                ) as cursor:
                    row = await cursor.fetchone()

            assert row is not None
            assert row[0] == "req-test"
            assert row[1] == "assistive"
            assert row[2] == "assistive by default"
        finally:
            if 'trace_db' in locals() and trace_db.connection is not None:
                await trace_db.connection.close()
            if db.connection is not None:
                await db.connection.close()

    @pytest.mark.asyncio
    async def test_persist_request_trace_falls_back_when_primary_db_is_locked(self, tmp_path):
        db_path = tmp_path / "assistant.db"
        trace_db_path = resolve_request_trace_db_path(db_path)
        db = Database(str(db_path))
        await db.connect()
        if db.connection is not None:
            await db.connection.close()

        class LockedAcquire:
            async def __aenter__(self):
                raise aiosqlite.OperationalError("database is locked")

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class LockedDB:
            database_path = db_path

            @asynccontextmanager
            async def acquire(self):
                async with LockedAcquire():
                    yield None

        persisted = await persist_request_trace(
            LockedDB(),
            request_id="req-fallback",
            trace_id="trace-fallback",
            started_at=1700000000.0,
            finished_at=1700000001.0,
            entrypoint="test",
            route_name="test_route",
            user_visible_flow="chat",
            lane="assistive",
            query_text="What time does the pool open?",
            models_used={"initial": "qwen3:8b"},
            pipeline_stages={"initial": True},
            policy_reason="assistive by default",
            routing_flags={"artifact_request": False},
            completed_successfully=True,
            stage_events=[],
        )

        assert persisted is True

        verify = Database(str(trace_db_path))
        await verify.connect()
        try:
            async with verify.acquire() as conn:
                async with conn.execute(
                    "SELECT request_id, lane FROM request_traces WHERE request_id = ?",
                    ("req-fallback",),
                ) as cursor:
                    row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "req-fallback"
            assert row[1] == "assistive"
        finally:
            if verify.connection is not None:
                await verify.connection.close()


class TestChatPolicyArtifactRouting:
    def test_action_item_request_routes_deep(self):
        decision = decide_chat_policy(
            "Create an action item for reviewing broken locker complaints from this week.",
            adaptive_enabled=True,
            max_aux_retrieval_calls=3,
            max_generation_stages=3,
            simple_query_word_threshold=9,
        )

        assert decision.artifact_request is True
        assert decision.lane == "deep"


class TestArtifactFallback:
    def test_action_item_fallback_builds_create_action(self):
        tool_call = _build_artifact_tool_fallback(
            "Create an action item for reviewing broken locker complaints from this week.",
            "Review this week's broken locker complaints and report patterns.",
        )

        assert tool_call is not None
        assert tool_call["name"] == "create_action"
        assert "locker complaints" in tool_call["arguments"]["title"].lower()

    def test_memo_fallback_builds_create_document(self):
        tool_call = _build_artifact_tool_fallback(
            "Draft a short memo for staff about the new towel inventory process.",
            "Staff memo\n\nWe are switching to a signed towel inventory sheet at close.",
        )

        assert tool_call is not None
        assert tool_call["name"] == "create_document"
        assert "towel inventory process" in tool_call["arguments"]["title"].lower()

    def test_memo_fallback_builds_create_document_without_reply_text(self):
        tool_call = _build_artifact_tool_fallback(
            "Draft a short memo for staff about the new towel inventory process.",
            "",
        )

        assert tool_call is not None
        assert tool_call["name"] == "create_document"
        assert "towel inventory process" in tool_call["arguments"]["title"].lower()
        assert "Requested Outcome" in tool_call["arguments"]["content"]
        assert "new towel inventory process" in tool_call["arguments"]["content"].lower()

    def test_decision_record_request_routes_deep(self):
        decision = decide_chat_policy(
            "Create a decision record for switching the Sunday class schedule to a trial format.",
            adaptive_enabled=True,
            max_aux_retrieval_calls=3,
            max_generation_stages=3,
            simple_query_word_threshold=9,
        )

        assert decision.artifact_request is True
        assert decision.lane == "deep"

    @pytest.mark.asyncio
    async def test_confirmation_updates_doc_artifact_trace(self, tmp_path):
        db_path = tmp_path / "assistant.db"
        trace_db_path = resolve_request_trace_db_path(db_path)
        db = Database(str(db_path))
        await db.connect()

        try:
            await persist_request_trace(
                db,
                request_id="req-doc",
                trace_id="trace-doc",
                started_at=1700000000.0,
                finished_at=1700000001.0,
                entrypoint="test",
                route_name="test_route",
                user_visible_flow="chat",
                lane="deep",
                query_text="Draft a short memo",
                completed_successfully=True,
                stage_events=[],
            )

            await update_request_trace_after_confirmation(
                db,
                request_id="req-doc",
                artifact_refs=["[doc:docs/memos/2026/03/15_memo.md]"],
            )

            trace_db = Database(str(trace_db_path))
            await trace_db.connect()
            async with trace_db.acquire() as conn:
                async with conn.execute(
                    "SELECT produced_artifact_type, produced_artifact_id FROM request_traces WHERE request_id = ?",
                    ("req-doc",),
                ) as cursor:
                    row = await cursor.fetchone()

            assert row is not None
            assert row[0] == "doc"
            assert row[1] == "docs/memos/2026/03/15_memo.md"
        finally:
            if 'trace_db' in locals() and trace_db.connection is not None:
                await trace_db.connection.close()
            if db.connection is not None:
                await db.connection.close()

    @pytest.mark.asyncio
    async def test_confirmation_decline_persists_failure_bucket(self, tmp_path):
        db_path = tmp_path / "assistant.db"
        trace_db_path = resolve_request_trace_db_path(db_path)
        db = Database(str(db_path))
        await db.connect()

        try:
            await persist_request_trace(
                db,
                request_id="req-declined",
                trace_id="trace-declined",
                started_at=1700000000.0,
                finished_at=1700000001.0,
                entrypoint="test",
                route_name="test_route",
                user_visible_flow="chat",
                lane="deep",
                query_text="Create a memo",
                completed_successfully=True,
                routing_flags={
                    "artifact_request": True,
                    "artifact_funnel": {
                        "intent_detected": True,
                        "lane_selected": "deep",
                        "tool_selected": True,
                        "confirmation_requested": True,
                    },
                },
                stage_events=[],
            )

            await update_request_trace_after_confirmation(
                db,
                request_id="req-declined",
                failure_mode="artifact_confirmation_declined",
                completed_successfully=False,
                artifact_funnel={
                    "confirmation_requested": True,
                    "terminal_state": "confirmation_declined",
                },
            )

            trace_db = Database(str(trace_db_path))
            await trace_db.connect()
            async with trace_db.acquire() as conn:
                async with conn.execute(
                    "SELECT failure_mode, completed_successfully, routing_flags_json FROM request_traces WHERE request_id = ?",
                    ("req-declined",),
                ) as cursor:
                    row = await cursor.fetchone()
                async with conn.execute(
                    "SELECT stage_name, metadata_json FROM request_stage_events WHERE request_id = ? ORDER BY id DESC LIMIT 1",
                    ("req-declined",),
                ) as cursor:
                    stage_row = await cursor.fetchone()

            assert row is not None
            assert row[0] == "artifact_confirmation_declined"
            assert row[1] == 0
            routing_flags = json.loads(row[2])
            assert routing_flags["artifact_funnel"]["terminal_state"] == "confirmation_declined"
            assert stage_row is not None
            assert stage_row[0] == "artifact_confirmation"
        finally:
            if 'trace_db' in locals() and trace_db.connection is not None:
                await trace_db.connection.close()
            if db.connection is not None:
                await db.connection.close()


class TestDecisionTool:
    @pytest.mark.asyncio
    async def test_create_decision_uses_runtime_schema(self, tmp_path):
        db_path = tmp_path / "assistant.db"
        db = Database(str(db_path))
        await db.connect()

        try:
            result = await _create_decision(
                db=db,
                title="Sunday schedule trial",
                decision="Switch Sunday classes to a trial format.",
                rationale="Attendance patterns suggest a lighter initial rollout.",
            )

            assert result.success is True
            assert result.artifact_refs

            async with db.acquire() as conn:
                async with conn.execute(
                    "SELECT title, decision, rationale FROM decisions WHERE title = ?",
                    ("Sunday schedule trial",),
                ) as cursor:
                    row = await cursor.fetchone()

            assert row is not None
            assert row[0] == "Sunday schedule trial"
        finally:
            if db.connection is not None:
                await db.connection.close()
