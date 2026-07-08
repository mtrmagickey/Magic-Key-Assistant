"""Tests for the FeedbackLearningLoop service.

Covers:
- Prompt refinement from accumulated negative feedback
- Knowledge-gap auto-creation from feedback thresholds
- Enhanced signal aggregation with recommendations
- Chunk quality scoring and decay
- Prompt suffix generation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR / "LeisureLLM"))


# ── Lightweight in-memory DB shim ──────────────────────────────────────────

class _FakeConnection:
    """Minimal async connection backed by an in-memory dict-of-lists."""

    def __init__(self, tables: dict):
        self._tables = tables
        self.rowcount = 0

    async def execute(self, sql: str, params=None):
        # Minimal SQL stub that records inserts and counts
        return self

    async def executescript(self, sql: str):
        pass

    async def commit(self):
        pass

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeDB:
    """Minimal DB shim for FeedbackLearningLoop that tracks calls."""

    def __init__(self):
        self._rows: dict[str, list[dict]] = {}
        self._exec_log: list[tuple[str, tuple]] = []

    class _AcquireCtx:
        def __init__(self, db: "_FakeDB"):
            self.db = db
            self.conn = _FakeConnection(db._rows)

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *args):
            pass

    def acquire(self):
        return self._AcquireCtx(self)

    async def execute(self, sql, *args):
        self._exec_log.append((sql, args))

    async def fetch_dicts(self, sql, *args):
        # Return empty by default — tests override via patching
        return []


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def fake_db():
    return _FakeDB()


@pytest.fixture
def loop(fake_db):
    from services.feedback_learning_loop import FeedbackLearningLoop
    fll = FeedbackLearningLoop(fake_db)
    fll._tables_ensured = True  # skip DDL
    return fll


# ── Tests: Prompt Refinement ─────────────────────────────────

class TestPromptRefinement:
    """Tests for _refine_prompts_from_feedback."""

    @pytest.mark.asyncio
    async def test_no_signals_returns_empty(self, loop):
        """No signals → no refinements."""
        result = await loop._refine_prompts_from_feedback(days=30)
        assert result == []

    @pytest.mark.asyncio
    async def test_below_threshold_no_refinement(self, loop, fake_db):
        """Signals below threshold don't trigger refinement."""
        async def mock_aggregate(days=30):
            return {
                "by_failure_mode": {"factual_error": 2},
                "top_topics": [],
                "chunk_correlation": {},
                "recommendations": [],
            }
        loop._aggregate_signals = mock_aggregate
        result = await loop._refine_prompts_from_feedback(days=30)
        assert result == []

    @pytest.mark.asyncio
    async def test_above_threshold_creates_variant(self, loop, fake_db):
        """Signals above threshold create a prompt variant."""
        async def mock_aggregate(days=30):
            return {
                "by_failure_mode": {"factual_error": 10, "clarity": 7},
                "top_topics": [],
                "chunk_correlation": {},
                "recommendations": [],
            }
        loop._aggregate_signals = mock_aggregate

        # fetch_dicts: return no existing variants for the existence check
        fake_db.fetch_dicts = AsyncMock(return_value=[])
        # register_prompt_variant needs to not error
        loop.register_prompt_variant = AsyncMock()

        result = await loop._refine_prompts_from_feedback(days=30)

        # Should create variants for both factual_error and clarity
        assert len(result) == 2
        modes = {r["failure_mode"] for r in result}
        assert "factual_error" in modes
        assert "clarity" in modes
        assert all(r["action"] == "created" for r in result)

    @pytest.mark.asyncio
    async def test_existing_active_variant_skipped(self, loop, fake_db):
        """Already-active variant is not recreated."""
        async def mock_aggregate(days=30):
            return {
                "by_failure_mode": {"factual_error": 10},
                "top_topics": [],
                "chunk_correlation": {},
                "recommendations": [],
            }
        loop._aggregate_signals = mock_aggregate

        # Simulate existing active variant on the existence-check query
        fake_db.fetch_dicts = AsyncMock(return_value=[{"id": 1, "is_active": 1}])
        loop.register_prompt_variant = AsyncMock()

        result = await loop._refine_prompts_from_feedback(days=30)
        assert len(result) == 1
        assert result[0]["action"] == "already_active"
        loop.register_prompt_variant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_failure_mode_ignored(self, loop):
        """Failure modes without directives are skipped."""
        async def mock_aggregate(days=30):
            return {
                "by_failure_mode": {"some_unknown_mode": 20},
                "top_topics": [],
                "chunk_correlation": {},
                "recommendations": [],
            }
        loop._aggregate_signals = mock_aggregate
        result = await loop._refine_prompts_from_feedback(days=30)
        assert result == []

    @pytest.mark.asyncio
    async def test_all_known_failure_modes_have_directives(self, loop):
        """Every known failure mode in the classifier has a directive."""
        from services.feedback_learning_loop import FeedbackLearningLoop
        known_modes = {"factual_error", "missing_info", "clarity", "too_verbose", "too_brief"}
        assert known_modes.issubset(set(FeedbackLearningLoop._FAILURE_MODE_DIRECTIVES.keys()))


class TestPromptSuffix:
    """Tests for get_active_prompt_suffix."""

    @pytest.mark.asyncio
    async def test_no_variants_returns_none(self, loop, fake_db):
        fake_db.fetch_dicts = AsyncMock(return_value=[])
        result = await loop.get_active_prompt_suffix()
        assert result is None

    @pytest.mark.asyncio
    async def test_active_variants_combined(self, loop, fake_db):
        fake_db.fetch_dicts = AsyncMock(return_value=[
            {"prompt_text": "Be concise."},
            {"prompt_text": "Cite sources."},
        ])
        result = await loop.get_active_prompt_suffix()
        assert "Be concise." in result
        assert "Cite sources." in result


# ── Tests: Gap Auto-Creation ─────────────────────────────────

class TestGapAutoCreation:
    """Tests for _check_feedback_gap_threshold."""

    @pytest.mark.asyncio
    async def test_below_threshold_no_gap(self, loop):
        """Under threshold → no gap created."""
        result = await loop._check_feedback_gap_threshold("test query", threshold=3)
        assert result is False

    @pytest.mark.asyncio
    async def test_batch_scan_returns_zero_when_empty(self, loop, fake_db):
        """Empty signals table → no gaps."""
        fake_db.fetch_dicts = AsyncMock(return_value=[])
        count = await loop._scan_for_feedback_gaps()
        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_scan_creates_gaps(self, loop, fake_db):
        """Signals above threshold create gaps during batch scan."""
        call_count = 0

        async def mock_fetch(sql, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: topic hash aggregation
                return [
                    {
                        "signal_key": "abc123def456",
                        "cnt": 5,
                        "sample_data": json.dumps({"failure_mode": "missing_info"}),
                    }
                ]
            # Second call: check existing gaps
            return []

        fake_db.fetch_dicts = mock_fetch
        count = await loop._scan_for_feedback_gaps(threshold=3)
        assert count == 1


# ── Tests: Signal Aggregation ────────────────────────────────

class TestSignalAggregation:
    """Tests for the enhanced _aggregate_signals."""

    @pytest.mark.asyncio
    async def test_empty_signals(self, loop, fake_db):
        """No data → empty report with structure intact."""
        fake_db.fetch_dicts = AsyncMock(return_value=[])
        result = await loop._aggregate_signals(days=30)
        assert "by_failure_mode" in result
        assert "top_topics" in result
        assert "chunk_correlation" in result
        assert "recommendations" in result

    @pytest.mark.asyncio
    async def test_recommendations_generated(self, loop, fake_db):
        """Dominant failure mode produces a recommendation."""
        call_count = 0

        async def mock_fetch(sql, *args):
            nonlocal call_count
            call_count += 1
            if "signal_type" in sql and "GROUP BY" in sql:
                return [
                    {"signal_type": "factual_error", "count": 12},
                    {"signal_type": "clarity", "count": 3},
                ]
            if "signal_key" in sql and "GROUP BY" in sql:
                return [
                    {"signal_key": "hash1", "count": 8},
                    {"signal_key": "hash2", "count": 4},
                    {"signal_key": "hash3", "count": 3},
                    {"signal_key": "hash4", "count": 3},
                    {"signal_key": "hash5", "count": 2},
                    {"signal_key": "hash6", "count": 2},
                ]
            return []

        fake_db.fetch_dicts = mock_fetch
        result = await loop._aggregate_signals(days=30)

        assert result["by_failure_mode"]["factual_error"] == 12
        assert len(result["top_topics"]) == 6
        assert any("factual" in r.lower() for r in result["recommendations"])

    @pytest.mark.asyncio
    async def test_aggregate_signals_returns_dict(self, loop, fake_db):
        """Return type is a dict, not a flat mode→count mapping."""
        fake_db.fetch_dicts = AsyncMock(return_value=[])
        result = await loop._aggregate_signals()
        assert isinstance(result, dict)
        assert "by_failure_mode" in result


# ── Tests: Run Learning Cycle ────────────────────────────────

class TestRunLearningCycle:
    """Tests for the full run_learning_cycle orchestration."""

    @pytest.mark.asyncio
    async def test_cycle_returns_all_keys(self, loop, fake_db):
        """Learning cycle returns expected result keys."""
        # Stub all sub-methods
        loop.retire_underperforming_variants = AsyncMock(return_value=[])
        loop._refine_prompts_from_feedback = AsyncMock(return_value=[])
        loop.get_low_quality_chunks = AsyncMock(return_value=[])
        loop._scan_for_feedback_gaps = AsyncMock(return_value=0)
        loop._aggregate_signals = AsyncMock(return_value={
            "by_failure_mode": {},
            "top_topics": [],
            "chunk_correlation": {},
            "recommendations": [],
        })
        loop._apply_quality_decay = AsyncMock(return_value=0)

        result = await loop.run_learning_cycle()

        assert "retired_variants" in result
        assert "prompt_refinements" in result
        assert "low_quality_chunks" in result
        assert "gaps_created" in result
        assert "improvement_signals" in result
        assert "scores_decayed" in result

    @pytest.mark.asyncio
    async def test_cycle_calls_refinement(self, loop, fake_db):
        """Learning cycle invokes prompt refinement."""
        mock_refine = AsyncMock(return_value=[{"failure_mode": "clarity", "action": "created"}])
        loop.retire_underperforming_variants = AsyncMock(return_value=[])
        loop._refine_prompts_from_feedback = mock_refine
        loop.get_low_quality_chunks = AsyncMock(return_value=[])
        loop._scan_for_feedback_gaps = AsyncMock(return_value=0)
        loop._aggregate_signals = AsyncMock(return_value={
            "by_failure_mode": {},
            "top_topics": [],
            "chunk_correlation": {},
            "recommendations": [],
        })
        loop._apply_quality_decay = AsyncMock(return_value=0)

        result = await loop.run_learning_cycle()
        mock_refine.assert_awaited_once()
        assert len(result["prompt_refinements"]) == 1

    @pytest.mark.asyncio
    async def test_cycle_calls_gap_scan(self, loop, fake_db):
        """Learning cycle invokes batch gap scan."""
        mock_scan = AsyncMock(return_value=3)
        loop.retire_underperforming_variants = AsyncMock(return_value=[])
        loop._refine_prompts_from_feedback = AsyncMock(return_value=[])
        loop.get_low_quality_chunks = AsyncMock(return_value=[])
        loop._scan_for_feedback_gaps = mock_scan
        loop._aggregate_signals = AsyncMock(return_value={
            "by_failure_mode": {},
            "top_topics": [],
            "chunk_correlation": {},
            "recommendations": [],
        })
        loop._apply_quality_decay = AsyncMock(return_value=0)

        result = await loop.run_learning_cycle()
        mock_scan.assert_awaited_once()
        assert result["gaps_created"] == 3


# ── Tests: Export Compatibility ──────────────────────────────

class TestExportAnonymised:
    """Verify export_anonymised_signals still works with enriched aggregation."""

    @pytest.mark.asyncio
    async def test_export_contains_failure_modes(self, loop, fake_db):
        """Export includes failure_mode_distribution from enriched signals."""
        async def mock_aggregate(days=30):
            return {
                "by_failure_mode": {"factual_error": 5},
                "top_topics": [],
                "chunk_correlation": {},
                "recommendations": [],
            }
        loop._aggregate_signals = mock_aggregate
        result = await loop.export_anonymised_signals(days=30)
        assert result["failure_mode_distribution"] == {"factual_error": 5}
        assert result["anonymised"] is True
        assert result["contains_pii"] is False
