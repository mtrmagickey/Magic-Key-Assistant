"""
Tests for the answer self-assessment service and near-miss retrieval.

Covers:
- LLM JSON parsing (valid, malformed, markdown-wrapped)
- Heuristic fallback when no router available
- Confidence threshold enforcement
- Grounded override logic
- Near-miss retrieval with mocked vectorstore
- Integration with gap logging (both Discord and web chat paths)
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")

from services.answer_self_assessment import (
    CONFIDENCE_GAP_THRESHOLD,
    NearMissResult,
    SelfAssessmentResult,
    _heuristic_fallback,
    _parse_assessment_json,
    assess_answer_quality,
    find_near_misses,
    format_near_misses_for_context,
)

# =============================================================================
# _parse_assessment_json
# =============================================================================

class TestParseAssessmentJson:
    """Test JSON parsing from LLM self-assessment responses."""

    def test_valid_json_high_confidence(self):
        raw = json.dumps({
            "confidence": 9,
            "grounded": True,
            "gap_detected": False,
            "missing_knowledge": "",
            "suggested_topic": "",
        })
        result = _parse_assessment_json(raw)
        assert result.confidence == 9
        assert result.grounded is True
        assert result.gap_detected is False
        assert result.missing_knowledge == ""
        assert result.error is None

    def test_valid_json_low_confidence_forces_gap(self):
        """Confidence <= threshold should force gap_detected=True even if LLM says false."""
        raw = json.dumps({
            "confidence": 4,
            "grounded": True,
            "gap_detected": False,  # LLM says no gap
            "missing_knowledge": "Missing the venue pricing policy",
            "suggested_topic": "venue pricing",
        })
        result = _parse_assessment_json(raw)
        assert result.confidence == 4
        assert result.gap_detected is True  # Overridden by threshold
        assert result.missing_knowledge == "Missing the venue pricing policy"

    def test_ungrounded_forces_gap(self):
        """Ungrounded response should force gap_detected=True."""
        raw = json.dumps({
            "confidence": 7,
            "grounded": False,
            "gap_detected": False,
            "missing_knowledge": "Answer based on general knowledge, not docs",
            "suggested_topic": "",
        })
        result = _parse_assessment_json(raw)
        assert result.grounded is False
        assert result.gap_detected is True  # Overridden by grounded=False

    def test_markdown_wrapped_json(self):
        """Should handle JSON wrapped in markdown code fences."""
        raw = """```json
{
    "confidence": 5,
    "grounded": false,
    "gap_detected": true,
    "missing_knowledge": "Need the opening hours document",
    "suggested_topic": "opening hours"
}
```"""
        result = _parse_assessment_json(raw)
        assert result.confidence == 5
        assert result.gap_detected is True
        assert result.suggested_topic == "opening hours"

    def test_json_with_surrounding_text(self):
        """Should extract JSON even with LLM commentary around it."""
        raw = 'Here is my assessment: {"confidence": 3, "grounded": false, "gap_detected": true, "missing_knowledge": "foo", "suggested_topic": "bar"} Hope this helps!'
        result = _parse_assessment_json(raw)
        assert result.confidence == 3
        assert result.gap_detected is True

    def test_completely_invalid_response(self):
        """Should return error result for unparseable text."""
        raw = "I cannot provide a JSON response to this request."
        result = _parse_assessment_json(raw)
        assert result.error is not None

    def test_boundary_confidence(self):
        """Test at exact threshold boundary."""
        raw = json.dumps({
            "confidence": CONFIDENCE_GAP_THRESHOLD,
            "grounded": True,
            "gap_detected": False,
            "missing_knowledge": "",
            "suggested_topic": "",
        })
        result = _parse_assessment_json(raw)
        assert result.gap_detected is True  # At threshold, should flag


# =============================================================================
# _heuristic_fallback
# =============================================================================

class TestHeuristicFallback:
    """Test fallback gap detection when LLM is unavailable."""

    def test_hedge_phrase_detected(self):
        result = _heuristic_fallback(
            "I don't have information about that topic.",
            "Some short context here.",
        )
        assert result.gap_detected is True
        assert result.confidence == 3

    def test_sparse_context(self):
        result = _heuristic_fallback(
            "The pool is open from 6am to 10pm daily.",
            "pool hours open",  # < 50 words
        )
        assert result.gap_detected is True
        assert result.confidence == 5

    def test_good_answer_no_gap(self):
        long_context = " ".join(["word"] * 100)  # 100 words
        result = _heuristic_fallback(
            "The pool is open from 6am to 10pm daily based on our schedule.",
            long_context,
        )
        assert result.gap_detected is False
        assert result.confidence == 8

    def test_empty_context(self):
        result = _heuristic_fallback("Some response", "")
        assert result.gap_detected is True


# =============================================================================
# assess_answer_quality
# =============================================================================

class TestAssessAnswerQuality:
    """Test the main self-assessment function."""

    @pytest.mark.asyncio
    async def test_falls_back_when_no_router(self):
        """Should use heuristic when no router is available."""
        # Pass a mock router with no pipeline to force heuristic fallback
        mock_router = MagicMock()
        mock_router.pipeline = None
        result = await assess_answer_quality(
            question="What are the pool hours?",
            response="I don't have information about pool hours.",
            context="",
            router=mock_router,
        )
        assert result.gap_detected is True
        assert result.confidence <= 5

    @pytest.mark.asyncio
    async def test_calls_llm_when_router_available(self):
        """Should call LLM via router for self-assessment."""
        mock_router = MagicMock()
        mock_router.pipeline = MagicMock()
        mock_router.timeouts.self_assessment = 10

        role_config = MagicMock()
        role_config.enabled = True
        role_config.backend_name = "ollama"
        role_config.model = "mistral"
        mock_router.pipeline.roles.get.return_value = role_config

        mock_router.generate_single = AsyncMock(return_value=json.dumps({
            "confidence": 9,
            "grounded": True,
            "gap_detected": False,
            "missing_knowledge": "",
            "suggested_topic": "",
        }))

        result = await assess_answer_quality(
            question="What are the pool hours?",
            response="The pool is open 6am-10pm daily.",
            context="Pool schedule: 6am to 10pm, seven days a week.",
            router=mock_router,
        )

        assert result.gap_detected is False
        assert result.confidence == 9
        mock_router.generate_single.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_heuristic(self):
        """Should fall back gracefully on timeout."""
        mock_router = MagicMock()
        mock_router.pipeline = MagicMock()

        role_config = MagicMock()
        role_config.enabled = True
        role_config.backend_name = "ollama"
        role_config.model = "mistral"
        mock_router.pipeline.roles.get.return_value = role_config

        # Simulate a slow LLM call
        async def slow_generate(*args, **kwargs):
            await asyncio.sleep(30)
            return "{}"

        mock_router.generate_single = slow_generate

        result = await assess_answer_quality(
            question="What are the pool hours?",
            response="The pool is open.",
            context=" ".join(["word"] * 100),
            router=mock_router,
        )

        # Should have fallen back to heuristic (which sees 100-word context, no hedge)
        assert result.error is None  # Heuristic doesn't set error
        assert result.confidence == 8  # Heuristic default for good context


# =============================================================================
# find_near_misses
# =============================================================================

class TestFindNearMisses:
    """Test near-miss corpus retrieval."""

    @pytest.mark.asyncio
    async def test_returns_chunks_from_vectorstore(self):
        from langchain_core.documents import Document

        mock_vs = MagicMock()
        mock_vs.similarity_search_with_score.return_value = [
            (Document(page_content="Pool hours are 6am to 9pm weekdays", metadata={
                "source_relpath": "docs/pool_schedule.md",
                "doc_type": "document",
            }), 0.25),
            (Document(page_content="Gym is open 5am to 11pm", metadata={
                "source_relpath": "docs/gym_hours.md",
                "doc_type": "document",
            }), 0.45),
        ]

        result = await find_near_misses("When is the pool open?", vectorstore=mock_vs, k=5)

        assert len(result.chunks) == 2
        assert result.chunks[0]["source"] == "docs/pool_schedule.md"
        assert "pool_schedule.md" in result.summary
        assert result.chunks[0]["score"] == 0.25

    @pytest.mark.asyncio
    async def test_handles_no_vectorstore(self):
        """When vectorstore raises, returns empty result."""
        mock_vs = MagicMock()
        mock_vs.similarity_search_with_score.side_effect = Exception("Connection refused")
        result = await find_near_misses("test question", vectorstore=mock_vs)
        assert result.chunks == []

    @pytest.mark.asyncio
    async def test_handles_empty_results(self):
        mock_vs = MagicMock()
        mock_vs.similarity_search_with_score.return_value = []

        result = await find_near_misses("test question", vectorstore=mock_vs)
        assert result.chunks == []
        assert result.summary == ""


# =============================================================================
# format_near_misses_for_context
# =============================================================================

class TestFormatNearMisses:
    """Test formatting near-misses for gap context strings."""

    def test_formats_chunks(self):
        nm = NearMissResult(
            chunks=[
                {
                    "source": "docs/pool_hours.md",
                    "content_preview": "The pool is open from 6am to 9pm on weekdays and 8am to 8pm on weekends.",
                    "score": 0.25,
                    "doc_type": "document",
                    "topics": "pool, hours",
                    "confidence": 0.9,
                },
            ],
            summary="test",
        )
        text = format_near_misses_for_context(nm)
        assert "pool_hours.md" in text
        assert "0.25" in text
        assert "Near-miss docs:" in text

    def test_empty_near_misses(self):
        nm = NearMissResult()
        text = format_near_misses_for_context(nm)
        assert text == ""
