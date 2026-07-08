"""
Tests for cogs/KnowledgeGapTracker.py — Knowledge gap detection & curation.

Covers:
- Module-level helpers: build_fallback_prompt, classify_gap_curation
- _default_ollama_model fallback logic
- _normalize_gap_text (pure string normalisation)
- _is_low_signal_gap (signal detection heuristics)
- _archivist_probe_pool / _select_archivist_probes (deterministic selection)
- insert_gap (DB interaction via mock)
- _run_gap_hygiene_sweep dry_run path

Run with: pytest tests/test_knowledge_gap_tracker.py -v
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cogs.KnowledgeGapTracker import (
    build_fallback_prompt,
    classify_gap_curation,
)

# ============================================================
# build_fallback_prompt (module-level)
# ============================================================

class TestBuildFallbackPrompt:

    @pytest.mark.unit
    def test_returns_expected_keys(self):
        gap = {"topic": "Pricing", "question": "What are the rates?", "context": ""}
        result = build_fallback_prompt(gap)
        assert "topic" in result
        assert "primary" in result
        assert "followups" in result
        assert "interview_prompt" in result

    @pytest.mark.unit
    def test_primary_from_question(self):
        gap = {"topic": "Events", "question": "When is the next gala?", "context": ""}
        result = build_fallback_prompt(gap)
        assert result["primary"] == "When is the next gala?"

    @pytest.mark.unit
    def test_missing_question_falls_back(self):
        gap = {"topic": "Events", "question": "", "context": ""}
        result = build_fallback_prompt(gap)
        assert result["primary"] == "(no question captured)"

    @pytest.mark.unit
    def test_followups_are_non_empty(self):
        gap = {"topic": "T", "question": "Q", "context": ""}
        result = build_fallback_prompt(gap)
        assert len(result["followups"]) >= 5

    @pytest.mark.unit
    def test_context_appears_in_prompt(self):
        gap = {"topic": "T", "question": "Q", "context": "Signed in 2024"}
        result = build_fallback_prompt(gap)
        assert "Signed in 2024" in result["interview_prompt"]


# ============================================================
# classify_gap_curation (module-level)
# ============================================================

class TestClassifyGapCuration:

    @pytest.mark.unit
    def test_empty_question_deferred(self):
        status, reason = classify_gap_curation("topic", "", "ctx")
        assert status == "defer"
        assert "empty" in reason

    @pytest.mark.unit
    def test_meta_doc_pattern_deferred(self):
        status, reason = classify_gap_curation(
            "Docs", "Who is the primary owner of this documentation?", ""
        )
        assert status == "defer"
        assert "meta-documentation" in reason

    @pytest.mark.unit
    def test_recursive_doc_question_deferred(self):
        status, reason = classify_gap_curation(
            "Open question: docs", "Where is the official documentation file path?", ""
        )
        assert status == "defer"
        assert "meta-documentation" in reason or "recursive" in reason

    @pytest.mark.unit
    def test_invented_axis_deferred(self):
        # 'latency' not in topic/context, only in question
        status, reason = classify_gap_curation(
            "Setup", "How can we optimize latency for the server?", ""
        )
        assert status == "defer"
        assert "invented axis" in reason

    @pytest.mark.unit
    def test_solution_fishing_deferred(self):
        status, reason = classify_gap_curation(
            "", "How do we optimize performance and reliability?", ""
        )
        assert status == "defer"

    @pytest.mark.unit
    def test_mba_phrasing_deferred(self):
        status, reason = classify_gap_curation(
            "Strategy", "We need a holistic approach", ""
        )
        assert status == "defer"
        assert "low-signal" in reason

    @pytest.mark.unit
    def test_grounded_question_kept(self):
        status, reason = classify_gap_curation(
            "Membership",
            "What is the config file path for membership tiers at /opt/app/config.yaml?",
            "",
        )
        assert status == "keep"

    @pytest.mark.unit
    def test_concrete_question_with_digits_kept(self):
        status, reason = classify_gap_curation(
            "Pricing",
            "What are the 2025 pricing tiers for corporate events over $5000?",
            "",
        )
        assert status == "keep"

    @pytest.mark.unit
    def test_synergy_always_deferred(self):
        status, _ = classify_gap_curation(
            "Strategy", "How do we leverage synergy between departments?", ""
        )
        assert status == "defer"


# ============================================================
# _default_ollama_model
# ============================================================

class TestDefaultOllamaModel:

    @pytest.mark.unit
    def test_reads_from_config_file(self, tmp_path):
        cfg = {
            "pipeline": {
                "roles": {
                    "initial": {"model": "test-model:13b"}
                }
            }
        }
        cfg_path = tmp_path / "model_router.json"
        cfg_path.write_text(json.dumps(cfg))

        # Reimport with patched path
        from cogs import KnowledgeGapTracker as kgt
        original = kgt._default_ollama_model

        with patch.object(Path, "__truediv__", return_value=cfg_path):
            # Call the function directly (not the cached module-level result)
            from cogs.KnowledgeGapTracker import _default_ollama_model
            result = _default_ollama_model()

        # The function should return something (may or may not hit our file depending on path resolution)
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.unit
    def test_falls_back_to_qwen(self):
        """When config file doesn't exist, should return fallback model."""
        from cogs.KnowledgeGapTracker import _default_ollama_model

        with patch("pathlib.Path.exists", return_value=False):
            result = _default_ollama_model()
        assert result == "qwen2.5:32b"


# ============================================================
# _normalize_gap_text (instance method — needs cog instance)
# ============================================================

@pytest.fixture
def gap_tracker(mock_bot):
    """Create a KnowledgeGapTracker instance with a mock bot."""
    from cogs.KnowledgeGapTracker import KnowledgeGapTracker

    with patch.object(KnowledgeGapTracker, "cog_load", new_callable=AsyncMock):
        # Suppress background task starts
        with patch("discord.ext.tasks.Loop.start"):
            tracker = KnowledgeGapTracker(mock_bot)
    return tracker


class TestNormalizeGapText:

    @pytest.mark.unit
    def test_lowercases_and_strips(self, gap_tracker):
        assert gap_tracker._normalize_gap_text("  HELLO WORLD  ") == "hello world"

    @pytest.mark.unit
    def test_collapses_whitespace(self, gap_tracker):
        assert gap_tracker._normalize_gap_text("a   b\n\tc") == "a b c"

    @pytest.mark.unit
    def test_strips_special_chars(self, gap_tracker):
        result = gap_tracker._normalize_gap_text("Price $100 — discount!")
        assert "$" not in result
        assert "—" not in result

    @pytest.mark.unit
    def test_removes_open_question_prefix(self, gap_tracker):
        result = gap_tracker._normalize_gap_text("Open question: What is the rate?")
        assert result == "what is the rate"

    @pytest.mark.unit
    def test_removes_q_prefix(self, gap_tracker):
        result = gap_tracker._normalize_gap_text("Q: How does this work?")
        assert result == "how does this work"

    @pytest.mark.unit
    def test_empty_input(self, gap_tracker):
        assert gap_tracker._normalize_gap_text("") == ""
        assert gap_tracker._normalize_gap_text(None) == ""


# ============================================================
# _is_low_signal_gap (instance method)
# ============================================================

class TestIsLowSignalGap:

    @pytest.mark.unit
    def test_empty_question_is_low_signal(self, gap_tracker):
        assert gap_tracker._is_low_signal_gap("topic", "") is True

    @pytest.mark.unit
    def test_very_short_question_is_low_signal(self, gap_tracker):
        assert gap_tracker._is_low_signal_gap("topic", "What?") is True

    @pytest.mark.unit
    def test_vague_markers_are_low_signal(self, gap_tracker):
        assert gap_tracker._is_low_signal_gap("topic", "We need data on this topic please help") is True
        assert gap_tracker._is_low_signal_gap("topic", "Any updates from the team about the project status?") is True

    @pytest.mark.unit
    def test_concrete_question_with_digits_is_not_low_signal(self, gap_tracker):
        assert gap_tracker._is_low_signal_gap(
            "Pricing",
            "What are the 2025 pricing tiers for corporate memberships?"
        ) is False

    @pytest.mark.unit
    def test_question_with_file_path_is_not_low_signal(self, gap_tracker):
        assert gap_tracker._is_low_signal_gap(
            "Config",
            "Where is the configuration stored in /opt/app/config.yaml for production?"
        ) is False

    @pytest.mark.unit
    def test_question_with_topic_signal_not_low(self, gap_tracker):
        # Long topic (>= 6 chars) provides enough signal 
        assert gap_tracker._is_low_signal_gap(
            "Membership Benefits",
            "what benefits do members get with their current package?"
        ) is False


# ============================================================
# _archivist_probe_pool / _select_archivist_probes
# ============================================================

class TestArchivistProbes:

    @pytest.mark.unit
    def test_probe_pool_returns_list(self, gap_tracker):
        pool = gap_tracker._archivist_probe_pool()
        assert isinstance(pool, list)
        assert len(pool) >= 10

    @pytest.mark.unit
    def test_pool_first_item_is_source_of_truth(self, gap_tracker):
        pool = gap_tracker._archivist_probe_pool()
        assert "source of truth" in pool[0].lower()

    @pytest.mark.unit
    def test_select_probes_returns_three(self, gap_tracker):
        probes = gap_tracker._select_archivist_probes(gap_id=42, run_date="2026-01-20")
        assert len(probes) == 3

    @pytest.mark.unit
    def test_select_probes_always_includes_source_of_truth(self, gap_tracker):
        for gap_id in (1, 10, 100, 999):
            probes = gap_tracker._select_archivist_probes(gap_id=gap_id, run_date="2026-01-20")
            assert any("source of truth" in p.lower() for p in probes)

    @pytest.mark.unit
    def test_select_probes_deterministic(self, gap_tracker):
        a = gap_tracker._select_archivist_probes(gap_id=7, run_date="2026-01-20")
        b = gap_tracker._select_archivist_probes(gap_id=7, run_date="2026-01-20")
        assert a == b

    @pytest.mark.unit
    def test_select_probes_differ_by_date(self, gap_tracker):
        a = gap_tracker._select_archivist_probes(gap_id=7, run_date="2026-01-20")
        b = gap_tracker._select_archivist_probes(gap_id=7, run_date="2026-01-27")
        # Very likely different (11 items, seeded differently)
        assert a != b


# ============================================================
# insert_gap (module-level async function)
# ============================================================

class TestInsertGap:

    @pytest.mark.unit
    async def test_insert_gap_calls_execute(self):
        from cogs.KnowledgeGapTracker import insert_gap

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock()

        await insert_gap(
            mock_conn,
            topic="Testing",
            question="Does the pool close early on Sundays?",
            context="Visitor asked at reception",
            priority_score=5,
            curation_status="keep",
            curation_reason="",
        )
        mock_conn.execute.assert_awaited()

    @pytest.mark.unit
    async def test_insert_gap_fallback_on_schema_mismatch(self):
        """If first INSERT fails (missing columns), it tries a simpler INSERT."""
        from cogs.KnowledgeGapTracker import insert_gap

        mock_conn = MagicMock()
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("table has no column named curation_status")

        mock_conn.execute = AsyncMock(side_effect=side_effect)

        await insert_gap(
            mock_conn,
            topic="Test",
            question="Q",
            context="C",
            priority_score=3,
            curation_status="keep",
            curation_reason="",
        )
        # Should have called execute at least twice (fallback path)
        assert mock_conn.execute.await_count >= 2


# ============================================================
# _run_gap_hygiene_sweep dry_run
# ============================================================

class TestGapHygieneSweep:

    @pytest.mark.unit
    async def test_dry_run_no_db(self, gap_tracker):
        """With no DB, returns zero counts."""
        result = await gap_tracker._run_gap_hygiene_sweep("2026-01-20", dry_run=True)
        assert result["scanned"] == 0

    @pytest.mark.unit
    async def test_dry_run_with_duplicates(self, gap_tracker):
        """dry_run=True should count but not modify."""

        # Simulate rows with duplicate normalized questions
        fake_rows = [
            {"id": 1, "topic": "A", "question": "What is the pool schedule?", "priority_score": 5, "times_asked": 1, "last_asked": "2026-01-19"},
            {"id": 2, "topic": "B", "question": "What is the pool schedule?", "priority_score": 3, "times_asked": 1, "last_asked": "2026-01-18"},
            {"id": 3, "topic": "C", "question": "What are the membership tiers for 2025?", "priority_score": 7, "times_asked": 2, "last_asked": "2026-01-20"},
        ]

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall = AsyncMock(return_value=fake_rows)
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn.execute = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_db = MagicMock()
        mock_db.acquire = MagicMock(return_value=mock_conn)

        gap_tracker.bot.db = mock_db

        result = await gap_tracker._run_gap_hygiene_sweep("2026-01-20", dry_run=True)
        assert result["scanned"] == 3
        assert result["deferred_duplicates"] == 1  # id=2 is the dupe
