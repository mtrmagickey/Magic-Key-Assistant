"""
Tests for automatic knowledge gap detection in the web chat streaming endpoint.

Verifies parity with the Discord /ask auto-detection (LLM.py) and the
standalone _maybe_log_knowledge_gap function in admin/routers/chat.py.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")

from admin.routers.chat import _GAP_INDICATORS, _SPARSE_CONTEXT_THRESHOLD, _maybe_log_knowledge_gap

# ── DB helper ────────────────────────────────────────────────────────────────


def _mock_db(existing_gap=None, lastrowid=42):
    """Build a mock async DB matching the acquire() context-manager pattern.

    aiosqlite's ``conn.execute(...)`` returns an object that is both an
    async context manager (for ``async with conn.execute(...) as cur:``)
    and awaitable (for ``await conn.execute(...)``).  We replicate that
    with a small helper class.
    """
    db = MagicMock()
    conn = MagicMock()

    # Build cursors
    select_cursor = MagicMock()
    select_cursor.fetchone = AsyncMock(return_value=existing_gap)

    insert_cursor = MagicMock()
    insert_cursor.lastrowid = lastrowid

    class _CursorCM:
        """Mimics aiosqlite's awaitable + async-CM cursor wrapper."""
        def __init__(self, cursor):
            self._cursor = cursor
        async def __aenter__(self):
            return self._cursor
        async def __aexit__(self, *args):
            pass
        def __await__(self):
            async def _resolve():
                return self._cursor
            return _resolve().__await__()

    def _execute_side_effect(*args, **kwargs):
        sql = args[0] if args else ""
        if "SELECT" in str(sql).upper():
            return _CursorCM(select_cursor)
        return _CursorCM(insert_cursor)

    # Use a regular MagicMock (not AsyncMock) so the return is immediate,
    # matching aiosqlite's synchronous-return-of-CM behavior.
    conn.execute = MagicMock(side_effect=_execute_side_effect)
    conn.commit = AsyncMock()

    class AcquireCM:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    db.acquire = lambda: AcquireCM()
    return db, conn


# ── Unit tests for _maybe_log_knowledge_gap ──────────────────────────────────


class TestGapIndicatorList:
    """Sanity checks on the indicator list itself."""

    def test_indicators_are_lowercase(self):
        for ind in _GAP_INDICATORS:
            assert ind == ind.lower(), f"Indicator not lowercase: {ind}"

    def test_threshold_is_positive(self):
        assert _SPARSE_CONTEXT_THRESHOLD > 0


class TestMaybeLogKnowledgeGap:
    """Tests for the _maybe_log_knowledge_gap async function."""

    @pytest.mark.asyncio
    async def test_no_gap_when_reply_is_confident(self):
        """A confident reply with plenty of context should NOT create a gap."""
        db, _ = _mock_db()
        # Provide a realistic context so the heuristic fallback doesn't
        # flag it as sparse when the LLM backend is unavailable.
        context = (
            "The NCMNS maintenance contract covers routine HVAC inspections "
            "and emergency repairs for the North Carolina Museum of Natural "
            "Sciences campus. The contract is valued at $16,000 per year, "
            "billed in two semi-annual invoices of $8,000 each. Coverage "
            "includes quarterly filter replacements, annual coil cleaning, "
            "bi-annual refrigerant checks, and 24-hour emergency call-out "
            "service. The contract was renewed in January 2024 for a "
            "three-year term ending December 2026."
        )
        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What is our NCMNS contract value?",
            reply_text="The NCMNS maintenance contract is $16,000/year, billed in two $8,000 invoices. [source: operational_context.txt]",
            context_word_count=200,
            doc_count=5,
            context=context,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_gap_created_on_indicator_phrase(self):
        """A reply containing a gap indicator should trigger gap creation."""
        db, conn = _mock_db(existing_gap=None, lastrowid=99)
        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What is our pricing for zoo installations?",
            reply_text="I don't have information about zoo-specific pricing in the knowledge base.",
            context_word_count=200,
            doc_count=3,
        )
        assert result == 99
        # Verify INSERT was called (second call after the SELECT)
        assert conn.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_gap_created_on_sparse_context(self):
        """Sparse context (< threshold words) should trigger gap creation regardless of reply text."""
        db, conn = _mock_db(existing_gap=None, lastrowid=50)
        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What is the competitor landscape in Virginia?",
            reply_text="Based on the available information, here are some general observations.",
            context_word_count=10,  # well below threshold
            doc_count=1,
        )
        assert result == 50

    @pytest.mark.asyncio
    async def test_existing_gap_incremented(self):
        """If a matching open gap already exists, times_asked should be incremented."""
        existing = (7, 3)  # (id=7, times_asked=3)
        db, conn = _mock_db(existing_gap=existing)
        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What is the competitor landscape in Virginia?",
            reply_text="I don't have information on Virginia competitors.",
            context_word_count=200,
            doc_count=3,
        )
        assert result == 7
        # Should have UPDATE (increment) rather than INSERT
        calls = conn.execute.call_args_list
        update_calls = [c for c in calls if "UPDATE" in str(c)]
        assert len(update_calls) >= 1

    @pytest.mark.asyncio
    async def test_all_indicators_trigger_detection(self):
        """Each indicator phrase should independently trigger gap detection."""
        for indicator in _GAP_INDICATORS:
            db, _ = _mock_db(existing_gap=None, lastrowid=1)
            result, _assess = await _maybe_log_knowledge_gap(
                db=db,
                question="test question",
                reply_text=f"Well, {indicator} so I cannot help with that.",
                context_word_count=200,  # plenty of context
                doc_count=5,
            )
            assert result is not None, f"Indicator '{indicator}' did not trigger gap detection"

    @pytest.mark.asyncio
    async def test_case_insensitive_detection(self):
        """Indicators should match regardless of case in the reply."""
        db, _ = _mock_db(existing_gap=None, lastrowid=10)
        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What about X?",
            reply_text="I DON'T HAVE INFORMATION about that topic.",
            context_word_count=200,
            doc_count=5,
        )
        assert result == 10

    @pytest.mark.asyncio
    async def test_zero_context_words_triggers_gap(self):
        """Zero context words (empty retrieval) should always trigger."""
        db, _ = _mock_db(existing_gap=None, lastrowid=20)
        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What is the meaning of life?",
            reply_text="That is a philosophical question with many perspectives.",
            context_word_count=0,
            doc_count=0,
        )
        assert result == 20

    @pytest.mark.asyncio
    async def test_db_failure_returns_none(self):
        """Database errors should be caught and return None (non-fatal)."""
        db = MagicMock()
        conn = MagicMock()
        conn.execute = MagicMock(side_effect=Exception("DB locked"))

        class AcquireCM:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *args):
                pass

        db.acquire = lambda: AcquireCM()

        result, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="test",
            reply_text="I don't know the answer.",
            context_word_count=10,
            doc_count=0,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_topic_extraction_from_question(self):
        """Topic should be extracted from the first 8 meaningful words (>3 chars)."""
        db, conn = _mock_db(existing_gap=None, lastrowid=1)
        _, _assess = await _maybe_log_knowledge_gap(
            db=db,
            question="What is the best way to approach museum partnerships in NC?",
            reply_text="I don't have information on that.",
            context_word_count=200,
            doc_count=3,
        )
        # Check INSERT call — topic should contain meaningful words, not "What", "is", "the"
        insert_calls = [c for c in conn.execute.call_args_list if "INSERT" in str(c)]
        assert len(insert_calls) >= 1
        insert_args = insert_calls[0].args[1]  # tuple of parameters
        topic = insert_args[0]
        assert "museum" in topic or "approach" in topic or "partnerships" in topic
        # Words with <=3 chars should be filtered out
        for word in topic.split():
            assert len(word) > 3, f"Short word '{word}' should have been filtered"
