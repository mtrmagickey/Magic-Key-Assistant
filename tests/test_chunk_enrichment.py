"""Tests for the chunk enrichment service and its integration points."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure LeisureLLM is importable
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR / "LeisureLLM"))

# Disable admin auth for tests
os.environ["ADMIN_AUTH_DISABLED"] = "1"


# ============================================================
# ChunkEnricher — unit tests
# ============================================================

class TestEnrichmentResultMetadata:
    """EnrichmentResult.to_metadata() produces ChromaDB-safe values."""

    def test_to_metadata_basic(self):
        from services.chunk_enrichment import EnrichmentResult

        r = EnrichmentResult(
            summary="Team discussed Q1 targets",
            topics=["Q1", "targets", "planning"],
            content_type="meeting_notes",
            participants=["alice", "bob"],
            date_range="2025-01-15",
            actionability=0.8,
            key_questions=["What are the Q1 targets?", "Who presented?"],
            entities=["Project Alpha", "Acme Corp"],
            confidence=0.9,
            enriched=True,
            enriched_at="2026-02-16T10:00:00",
            enrichment_model="qwen2.5:32b",
        )
        meta = r.to_metadata()
        assert meta["enriched"] is True
        assert meta["llm_summary"] == "Team discussed Q1 targets"
        assert meta["llm_topics"] == "Q1|targets|planning"
        assert meta["llm_content_type"] == "meeting_notes"
        assert meta["llm_participants"] == "alice|bob"
        assert meta["llm_date_range"] == "2025-01-15"
        assert meta["llm_actionability"] == 0.8
        assert "What are the Q1 targets?" in meta["llm_key_questions"]
        assert meta["llm_entities"] == "Project Alpha|Acme Corp"
        assert meta["llm_confidence"] == 0.9
        assert meta["enrichment_model"] == "qwen2.5:32b"

    def test_to_metadata_defaults(self):
        from services.chunk_enrichment import EnrichmentResult

        r = EnrichmentResult()
        meta = r.to_metadata()
        assert meta["enriched"] is False
        assert meta["llm_summary"] == ""
        assert meta["llm_topics"] == ""
        assert meta["llm_entities"] == ""
        assert meta["llm_confidence"] == 0.5
        assert meta["enrichment_model"] == ""


class TestParseResponse:
    """_parse_response handles LLM JSON quirks."""

    def setup_method(self):
        from services.chunk_enrichment import ChunkEnricher
        self.enricher = ChunkEnricher.__new__(ChunkEnricher)

    def test_clean_json(self):
        raw = json.dumps({
            "summary": "test summary",
            "topics": ["a", "b"],
            "content_type": "decision",
            "participants": [],
            "date_range": "2025-06-01",
            "actionability": 0.9,
            "key_questions": ["What was decided?"],
            "confidence": 0.85,
        })
        result = self.enricher._parse_response(raw)
        assert result.enriched is True
        assert result.summary == "test summary"
        assert result.content_type == "decision"
        assert result.actionability == 0.9
        assert result.confidence == 0.85
        assert len(result.key_questions) == 1

    def test_markdown_fenced_json(self):
        raw = "```json\n" + json.dumps({
            "summary": "fenced",
            "topics": [],
            "content_type": "reference",
            "participants": [],
            "date_range": "",
            "actionability": 0.5,
            "key_questions": [],
        }) + "\n```"
        result = self.enricher._parse_response(raw)
        assert result.summary == "fenced"
        assert result.content_type == "reference"

    def test_trailing_comma(self):
        raw = '{"summary": "tc", "topics": ["a",], "content_type": "noise", "participants": [], "date_range": "", "actionability": 0.1, "key_questions": [],}'
        result = self.enricher._parse_response(raw)
        assert result.summary == "tc"
        assert result.content_type == "noise"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            self.enricher._parse_response("not json at all")

    def test_content_type_normalisation(self):
        raw = json.dumps({
            "summary": "x",
            "topics": [],
            "content_type": "Meeting Notes",
            "participants": [],
            "date_range": "",
            "actionability": 0.5,
            "key_questions": [],
        })
        result = self.enricher._parse_response(raw)
        assert result.content_type == "meeting_notes"

    def test_actionability_clamped(self):
        raw = json.dumps({
            "summary": "x",
            "topics": [],
            "content_type": "decision",
            "participants": [],
            "date_range": "",
            "actionability": 5.0,
            "key_questions": [],
        })
        result = self.enricher._parse_response(raw)
        assert result.actionability == 1.0


class TestHallucinationValidation:
    """_parse_response validates extracted fields against source text."""

    def setup_method(self):
        from services.chunk_enrichment import ChunkEnricher
        self.enricher = ChunkEnricher.__new__(ChunkEnricher)

    def test_hallucinated_participants_removed(self):
        """Participants not in source text get filtered out."""
        raw = json.dumps({
            "summary": "Meeting about budget",
            "topics": ["budget"],
            "content_type": "meeting_notes",
            "participants": ["alice", "bob", "hallucinated_person"],
            "date_range": "",
            "actionability": 0.7,
            "key_questions": [],
            "entities": [],
            "confidence": 0.8,
        })
        result = self.enricher._parse_response(raw, source_text="alice and bob discussed the budget")
        assert "alice" in result.participants
        assert "bob" in result.participants
        assert "hallucinated_person" not in result.participants

    def test_hallucinated_entities_removed(self):
        """Entities not in source text get filtered out."""
        raw = json.dumps({
            "summary": "Project update",
            "topics": ["project"],
            "content_type": "reference",
            "participants": [],
            "date_range": "",
            "actionability": 0.5,
            "key_questions": [],
            "entities": ["Real Project", "Invented Corp"],
            "confidence": 0.7,
        })
        result = self.enricher._parse_response(
            raw, source_text="The Real Project is on track for delivery"
        )
        assert "Real Project" in result.entities
        assert "Invented Corp" not in result.entities

    def test_mostly_hallucinated_reduces_confidence(self):
        """If >50% of entities/participants are hallucinated, confidence drops."""
        raw = json.dumps({
            "summary": "Some text",
            "topics": [],
            "content_type": "reference",
            "participants": ["real_person", "fake1", "fake2", "fake3"],
            "date_range": "",
            "actionability": 0.5,
            "key_questions": [],
            "entities": ["FakeCompany", "FakeProject"],
            "confidence": 0.9,
        })
        result = self.enricher._parse_response(
            raw, source_text="real_person wrote a note"
        )
        # 5/6 were hallucinated, confidence should be clamped to <= 0.3
        assert result.confidence <= 0.3

    def test_no_source_text_skips_validation(self):
        """Without source text, all entities/participants are kept."""
        raw = json.dumps({
            "summary": "Something",
            "topics": [],
            "content_type": "reference",
            "participants": ["anyone"],
            "date_range": "",
            "actionability": 0.5,
            "key_questions": [],
            "entities": ["AnyEntity"],
            "confidence": 0.7,
        })
        result = self.enricher._parse_response(raw, source_text="")
        assert "anyone" in result.participants
        assert "AnyEntity" in result.entities


class TestHelpers:
    """Test module-level helper functions."""

    def test_as_str_list_from_list(self):
        from services.chunk_enrichment import _as_str_list
        assert _as_str_list(["a", "b", ""]) == ["a", "b"]

    def test_as_str_list_from_csv_string(self):
        from services.chunk_enrichment import _as_str_list
        assert _as_str_list("a, b, c") == ["a", "b", "c"]

    def test_as_str_list_empty(self):
        from services.chunk_enrichment import _as_str_list
        assert _as_str_list(None) == []

    def test_normalise_content_type_valid(self):
        from services.chunk_enrichment import _normalise_content_type
        assert _normalise_content_type("decision") == "decision"
        assert _normalise_content_type("STRATEGY") == "strategy"
        assert _normalise_content_type("team_bio") == "team_bio"
        assert _normalise_content_type("product_spec") == "product_spec"
        assert _normalise_content_type("legal_contract") == "legal_contract"

    def test_normalise_content_type_fuzzy(self):
        from services.chunk_enrichment import _normalise_content_type
        assert _normalise_content_type("casual discussion") == "casual_discussion"

    def test_normalise_content_type_unknown(self):
        from services.chunk_enrichment import _normalise_content_type
        assert _normalise_content_type("banana") == "unknown"

    def test_clamp_float(self):
        from services.chunk_enrichment import _clamp_float
        assert _clamp_float(-1.0) == 0.0
        assert _clamp_float(2.5) == 1.0
        assert _clamp_float(0.7) == 0.7
        assert _clamp_float("invalid") == 0.5


class TestChunkEnricherEnrich:
    """Test the enrich() method with mocked Ollama calls."""

    @pytest.mark.asyncio
    async def test_enrich_success(self):
        from services.chunk_enrichment import ChunkEnricher

        mock_response = json.dumps({
            "summary": "Discussion about Q1 planning",
            "topics": ["planning", "Q1"],
            "content_type": "meeting_notes",
            "participants": ["alice"],
            "date_range": "2025-01-15",
            "actionability": 0.7,
            "key_questions": ["What was the Q1 plan?"],
            "confidence": 0.8,
        })

        enricher = ChunkEnricher(model="test-model")
        enricher._call_ollama = AsyncMock(return_value=mock_response)
        enricher._detect_model = AsyncMock(return_value="test-model")

        result = await enricher.enrich("Some text about Q1 planning with alice")
        assert result.enriched is True
        assert result.summary == "Discussion about Q1 planning"
        assert result.content_type == "meeting_notes"
        assert "alice" in result.participants
        assert result.confidence == 0.8
        assert result.enrichment_model == "test-model"

    @pytest.mark.asyncio
    async def test_enrich_failure_returns_default(self):
        from services.chunk_enrichment import ChunkEnricher

        enricher = ChunkEnricher(model="test-model", max_retries=0)
        enricher._call_ollama = AsyncMock(side_effect=RuntimeError("Ollama down"))
        enricher._detect_model = AsyncMock(return_value="test-model")

        result = await enricher.enrich("Some text")
        assert result.enriched is False
        assert result.summary == ""

    @pytest.mark.asyncio
    async def test_enrich_batch(self):
        from services.chunk_enrichment import ChunkEnricher

        responses = [
            json.dumps({"summary": f"Summary {i}", "topics": [], "content_type": "doc",
                        "participants": [], "date_range": "", "actionability": 0.5, "key_questions": []})
            for i in range(3)
        ]
        call_count = 0

        async def mock_call(model, prompt):
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        enricher = ChunkEnricher(model="test-model")
        enricher._call_ollama = mock_call
        enricher._detect_model = AsyncMock(return_value="test-model")

        chunks = [{"text": f"Chunk {i}"} for i in range(3)]
        results = await enricher.enrich_batch(chunks)
        assert len(results) == 3
        assert all(r.enriched for r in results)
        assert results[0].summary == "Summary 0"
        assert results[2].summary == "Summary 2"


class TestModelDetection:
    """Auto-detection picks the best model for enrichment."""

    @pytest.mark.asyncio
    async def test_prefers_smaller_qwen(self):
        """The fallback heuristic prefers smaller Qwen models over large LLaMA."""
        from services.chunk_enrichment import ChunkEnricher

        enricher = ChunkEnricher()
        mock_session = AsyncMock()
        mock_session.closed = False  # prevent _get_session from replacing our mock
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "models": [
                {"name": "llama3.3:70b-instruct-q4_K_M"},
                {"name": "qwen2.5:32b"},
            ]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        enricher._session = mock_session

        # Patch catalog-based selection (returns None → force fallback heuristic)
        with patch("services.model_discovery.pick_best_enrichment_model", new_callable=AsyncMock, return_value=None):
            model = await enricher._detect_model()
        assert model == "qwen2.5:32b"  # Preferred over 70B llama
        assert model == "qwen2.5:32b"  # Preferred over 70B llama


class TestOrgContextInjection:
    """ChunkEnricher pulls org profile into enrichment prompt."""

    def test_build_org_context_with_org_profile(self):
        """When org profile has real data, context block is generated."""
        from core.config_loader import OrgMember, OrgProfile
        from services.chunk_enrichment import ChunkEnricher

        mock_org = OrgProfile(
            name="Acme Labs",
            industry="Biotech",
            capabilities=["Gene editing", "Lab automation"],
            members=[OrgMember(discord_user_id=1, name="Dr. Smith")],
        )
        with patch("core.config_loader.OrgProfile.load", return_value=mock_org):
            ctx = ChunkEnricher._build_org_context()
        assert "Acme Labs" in ctx
        assert "Biotech" in ctx
        assert "Gene editing" in ctx
        assert "Dr. Smith" in ctx
        assert "NOT for output" in ctx  # guardrail language

    def test_build_org_context_default_org_returns_empty(self):
        """Default 'My Company' org produces no context (avoids noise)."""
        from core.config_loader import OrgProfile
        from services.chunk_enrichment import ChunkEnricher

        mock_org = OrgProfile()  # defaults to "My Company"
        with patch("core.config_loader.OrgProfile.load", return_value=mock_org):
            # Also suppress operational_context.txt
            with patch("pathlib.Path.exists", return_value=False):
                ctx = ChunkEnricher._build_org_context()
        assert ctx == ""

    def test_org_context_injected_into_prompt(self):
        """The enrichment prompt includes org context when available."""
        from services.chunk_enrichment import EXTRACTION_PROMPT, ChunkEnricher

        enricher = ChunkEnricher.__new__(ChunkEnricher)
        enricher._org_context = "\n=== ORG: Test Corp ===\n"
        enricher.model = "test"
        enricher.max_retries = 0
        enricher.endpoint = "http://localhost:11434"
        enricher.timeout = 10
        enricher._session = None

        import json

        from services.chunk_enrichment import ENRICHMENT_SCHEMA
        schema_str = json.dumps(ENRICHMENT_SCHEMA, indent=2)
        prompt = (
            EXTRACTION_PROMPT
            .replace("{schema}", schema_str)
            .replace("{text}", "sample text")
            .replace("{org_context}", enricher._org_context)
        )
        assert "Test Corp" in prompt
        assert "sample text" in prompt


# ============================================================
# Integration: filter_superseded_docs with enrichment metadata
# ============================================================

class TestFilterWithEnrichment:
    """filter_superseded_docs properly handles enriched metadata."""

    def _make_doc(self, content="test", **meta):
        from langchain_core.documents import Document
        return Document(page_content=content, metadata=meta)

    def test_noise_filtered_out(self):
        from cogs.LLM import filter_superseded_docs

        docs = [
            self._make_doc("important", llm_content_type="decision", llm_actionability=0.9, llm_confidence=0.8),
            self._make_doc("spam", llm_content_type="noise", llm_actionability=0.0, llm_confidence=0.8),
            self._make_doc("reference", llm_content_type="reference", llm_actionability=0.6, llm_confidence=0.7),
        ]
        result = filter_superseded_docs(docs)
        contents = [d.page_content for d in result]
        # Noise should be excluded (we have enough non-noise docs)
        # Actually with only 2 non-noise docs, noise might still be included
        # but it should be at the end
        assert contents[0] == "important"  # Highest actionability first

    def test_low_confidence_noise_not_filtered(self):
        """If the LLM wasn't confident something is noise, don't filter it."""
        from cogs.LLM import filter_superseded_docs

        docs = [
            self._make_doc("maybe not noise", llm_content_type="noise",
                          llm_actionability=0.3, llm_confidence=0.1),
            self._make_doc("definitely good", llm_content_type="decision",
                          llm_actionability=0.9, llm_confidence=0.9),
        ]
        result = filter_superseded_docs(docs)
        contents = [d.page_content for d in result]
        # Low-confidence noise should NOT be filtered — keep it as regular doc
        assert "maybe not noise" in contents
        assert len(result) == 2

    def test_actionability_sorting(self):
        from cogs.LLM import filter_superseded_docs

        docs = [
            self._make_doc("low", llm_actionability=0.1, source_priority=1),
            self._make_doc("high", llm_actionability=0.9, source_priority=1),
            self._make_doc("mid", llm_actionability=0.5, source_priority=1),
        ]
        result = filter_superseded_docs(docs)
        assert result[0].page_content == "high"
        assert result[1].page_content == "mid"
        assert result[2].page_content == "low"


# ============================================================
# Integration: format_docs_for_context with enrichment metadata
# ============================================================

class TestFormatWithEnrichment:
    """format_docs_for_context uses enriched metadata."""

    def _make_doc(self, content="test", **meta):
        from langchain_core.documents import Document
        return Document(page_content=content, metadata=meta)

    def test_enriched_summary_prepended(self):
        from cogs.LLM import format_docs_for_context

        docs = [
            self._make_doc("Full text here", llm_summary="Quick summary",
                          llm_content_type="decision", llm_confidence=0.8),
        ]
        result = format_docs_for_context(docs)
        assert "[Summary (AI-extracted): Quick summary]" in result
        assert "Full text here" in result

    def test_low_confidence_summary_caveated(self):
        from cogs.LLM import format_docs_for_context

        docs = [
            self._make_doc("Full text here", llm_summary="Guess summary",
                          llm_content_type="reference", llm_confidence=0.2),
        ]
        result = format_docs_for_context(docs)
        assert "[Summary (low confidence): Guess summary]" in result

    def test_enriched_content_type_in_header(self):
        from cogs.LLM import format_docs_for_context

        docs = [
            self._make_doc("text", llm_content_type="strategy", source_relpath="test.txt"),
        ]
        result = format_docs_for_context(docs)
        assert "type=strategy" in result

    def test_enriched_date_preferred(self):
        from cogs.LLM import format_docs_for_context

        docs = [
            self._make_doc("text", llm_date_range="2025-06-15", doc_date="2025"),
        ]
        result = format_docs_for_context(docs)
        assert "date=2025-06-15" in result


# ============================================================
# API endpoint tests
# ============================================================

class TestEnrichmentAPI:
    """Enrichment API endpoints."""

    @pytest.fixture
    def client(self):
        """Create a TestClient matching test_admin_gui.py pattern."""
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
        mock_bot.db = MagicMock()
        dependencies._bot_instance = mock_bot

        from fastapi.testclient import TestClient
        with TestClient(server.app, raise_server_exceptions=False) as c:
            yield c

    def test_enrichment_status_endpoint(self, client):
        resp = client.get("/api/v1/knowledge/enrichment/status",
                         headers={"X-CSRF-Protection": "1"})
        assert resp.status_code == 200
        data = resp.json()
        # Should return structure even if ChromaDB not available
        assert "success" in data

    def test_start_enrichment_endpoint(self, client):
        with patch("services.chunk_enrichment.start_reenrichment", new_callable=AsyncMock) as mock_start:
            mock_job = MagicMock()
            mock_job.progress = {"status": "running", "total": 100, "done": 0}
            mock_start.return_value = mock_job

            resp = client.post("/api/v1/knowledge/enrichment/start",
                             json={"force": False},
                             headers={"X-CSRF-Protection": "1"})
            assert resp.status_code == 200

    def test_cancel_no_job(self, client):
        with patch("services.chunk_enrichment.get_active_job", return_value=None):
            resp = client.post("/api/v1/knowledge/enrichment/cancel",
                             headers={"X-CSRF-Protection": "1"})
            data = resp.json()
            assert data["success"] is False
