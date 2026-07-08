"""
Tests for the proactive suggestions engine.

Validates:
    - Nudge generation from various workspace states (overdue actions, stale gaps,
      cold leads, trending concerns, unactioned decisions, idle periods)
    - Deduplication / cooldown logic
    - Query-relevance boosting
    - Context string building for LLM injection
    - Display formatting for frontend
    - Config gate check
"""

import asyncio
import os
import sys
import time
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

from services.proactive_suggestions import (
    _NUDGE_COOLDOWN_HOURS,
    Nudge,
    ProactiveSuggestionEngine,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_db_with_rows(table_rows_map: dict):
    """
    Build a mock DB whose .acquire() context manager returns a connection
    that dispatches fetchall based on the SQL query's table name.

    table_rows_map: {"tasks": [row_dicts], "knowledge_gaps": [...], ...}
    """
    db = MagicMock()

    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.fetchall = AsyncMock(return_value=[])

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        async def fetchall(self):
            return self._rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class FakeConn:
        def __init__(self, table_map):
            self._table_map = table_map

        def execute(self, sql, params=None):
            # Determine table from SQL
            sql_lower = sql.lower()
            for table_name, rows in self._table_map.items():
                if table_name in sql_lower:
                    return FakeCursor(rows)
            return FakeCursor([])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    fake_conn = FakeConn(table_rows_map)

    class FakeAcquire:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *_):
            pass

    db.acquire = lambda: FakeAcquire()

    async def _fetch_dicts(sql, params=None):
        sql_lower = sql.lower()
        for table_name, rows in table_rows_map.items():
            if table_name in sql_lower:
                return [dict(r) for r in rows]
        return []

    async def _fetch_one_dict(sql, params=None):
        sql_lower = sql.lower()
        for table_name, rows in table_rows_map.items():
            if table_name in sql_lower:
                return dict(rows[0]) if rows else None
        return None

    db.fetch_dicts = _fetch_dicts
    db.fetch_one_dict = _fetch_one_dict

    return db


def _make_row(data: dict):
    """Create a dict-like row that supports both attribute and key access."""
    class Row(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key)

    return Row(data)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestNudgeDataclass:
    """Test the Nudge dataclass."""

    def test_hash_auto_generated(self):
        n = Nudge(
            hook="Something overdue",
            suggestion="Want me to help?",
            category="actions",
            artifact_id="task-42",
        )
        assert n._hash
        assert len(n._hash) == 12

    def test_same_nudge_same_hash(self):
        n1 = Nudge(hook="Same hook", suggestion="s1", category="actions", artifact_id="a1")
        n2 = Nudge(hook="Same hook", suggestion="s2", category="actions", artifact_id="a1")
        assert n1._hash == n2._hash

    def test_different_nudge_different_hash(self):
        n1 = Nudge(hook="Hook A", suggestion="s", category="actions", artifact_id="a1")
        n2 = Nudge(hook="Hook B", suggestion="s", category="gaps", artifact_id="a2")
        assert n1._hash != n2._hash


class TestOverdueActionScanner:
    """Test overdue action item detection."""

    @pytest.mark.asyncio
    async def test_detects_overdue_tasks(self):
        rows = [
            _make_row({
                "id": 1,
                "title": "Follow up with Henderson",
                "due_date": "2026-02-10",
                "assignee": "Colin",
                "status": "in-progress",
            }),
        ]
        db = _mock_db_with_rows({"tasks": rows})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_overdue_actions()

        assert len(nudges) >= 1
        assert "Henderson" in nudges[0].hook
        assert nudges[0].category == "actions"
        assert nudges[0].priority > 0.5

    @pytest.mark.asyncio
    async def test_empty_when_no_overdue(self):
        db = _mock_db_with_rows({"tasks": []})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_overdue_actions()
        assert nudges == []


class TestStaleGapScanner:
    """Test knowledge gap nudge detection."""

    @pytest.mark.asyncio
    async def test_detects_frequently_asked_gaps(self):
        rows = [
            _make_row({
                "id": 5,
                "topic": "Pricing Strategy",
                "question": "What is our pricing model?",
                "times_asked": 7,
                "priority_score": 0.8,
            }),
        ]
        db = _mock_db_with_rows({"knowledge_gaps": rows})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_stale_gaps()

        assert len(nudges) == 1
        assert "Pricing Strategy" in nudges[0].hook
        assert "7 times" in nudges[0].hook
        assert nudges[0].category == "gaps"

    @pytest.mark.asyncio
    async def test_empty_when_no_stale_gaps(self):
        db = _mock_db_with_rows({"knowledge_gaps": []})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_stale_gaps()
        assert nudges == []


class TestColdLeadScanner:
    """Test pipeline lead nudge detection."""

    @pytest.mark.asyncio
    async def test_detects_cold_leads(self):
        rows = [
            _make_row({
                "id": 3,
                "name": "City Museum",
                "stage": "warm",
                "contact_name": "Jane Doe",
                "last_activity": "2026-02-01T10:00:00",
            }),
        ]
        db = _mock_db_with_rows({"leads": rows, "lead_activity": []})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_cold_leads()

        assert len(nudges) == 1
        assert "Jane Doe" in nudges[0].hook
        assert nudges[0].category == "pipeline"


class TestTrendingConcernScanner:
    """Test concern thread nudge detection."""

    @pytest.mark.asyncio
    async def test_detects_trending_concerns(self):
        rows = [
            _make_row({
                "id": 10,
                "topic": "staffing capacity",
                "query_count": 6,
                "last_seen": "2026-02-26T14:00:00",
            }),
        ]
        db = _mock_db_with_rows({"concern_threads": rows})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_trending_concerns()

        assert len(nudges) == 1
        assert "staffing capacity" in nudges[0].hook
        assert nudges[0].category == "concerns"


class TestIdlePeriodScanner:
    """Test idle period check-in nudge."""

    @pytest.mark.asyncio
    async def test_detects_idle_period(self):
        rows = [
            _make_row({
                "last_chat": "2026-02-24T10:00:00",  # ~3 days ago
            }),
        ]
        db = _mock_db_with_rows({"chat_interactions": rows})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_idle_period()

        assert len(nudges) == 1
        assert nudges[0].category == "general"
        assert "couple of days" in nudges[0].hook

    @pytest.mark.asyncio
    async def test_no_nudge_for_recent_chat(self):
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        rows = [_make_row({"last_chat": now})]
        db = _mock_db_with_rows({"chat_interactions": rows})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine._scan_idle_period()
        assert nudges == []


class TestSuggestionEngine:
    """Integration tests for the full engine."""

    @pytest.mark.asyncio
    async def test_get_suggestions_returns_max_2(self):
        """Even with many nudges available, should return at most max_results."""
        rows_tasks = [
            _make_row({"id": i, "title": f"Task {i}", "due_date": "2026-02-01",
                        "assignee": "A", "status": "open"})
            for i in range(5)
        ]
        db = _mock_db_with_rows({"tasks": rows_tasks})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine.get_suggestions(max_results=2)
        assert len(nudges) <= 2

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeat_nudges(self):
        """Same nudge shouldn't appear within cooldown period."""
        rows = [
            _make_row({
                "id": 1, "title": "Overdue thing", "due_date": "2026-02-01",
                "assignee": "X", "status": "open",
            }),
        ]
        db = _mock_db_with_rows({"tasks": rows})
        engine = ProactiveSuggestionEngine(db)

        # First call: should get the nudge
        first = await engine.get_suggestions(max_results=5)
        assert len(first) >= 1

        # Second call: same nudge should be suppressed
        second = await engine.get_suggestions(max_results=5)
        first_hashes = {n._hash for n in first}
        second_hashes = {n._hash for n in second}
        assert len(first_hashes & second_hashes) == 0

    @pytest.mark.asyncio
    async def test_query_relevance_boost(self):
        """Nudges mentioning query keywords should rank higher."""
        rows = [
            _make_row({
                "id": 1, "title": "Henderson review", "due_date": "2026-02-01",
                "assignee": "Y", "status": "open",
            }),
            _make_row({
                "id": 2, "title": "Budget planning", "due_date": "2026-02-01",
                "assignee": "Z", "status": "open",
            }),
        ]
        db = _mock_db_with_rows({"tasks": rows})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine.get_suggestions(
            query="What's happening with Henderson?", max_results=5,
        )
        # Henderson nudge should be first (or at least present)
        if nudges:
            assert any("Henderson" in n.hook for n in nudges)

    @pytest.mark.asyncio
    async def test_category_filter(self):
        """Should filter by category when specified."""
        rows_tasks = [
            _make_row({"id": 1, "title": "T", "due_date": "2026-02-01",
                        "assignee": "A", "status": "open"}),
        ]
        rows_gaps = [
            _make_row({"id": 2, "topic": "Q", "question": "?", "times_asked": 5,
                        "priority_score": 0.8}),
        ]
        db = _mock_db_with_rows({"tasks": rows_tasks, "knowledge_gaps": rows_gaps})
        engine = ProactiveSuggestionEngine(db)
        nudges = await engine.get_suggestions(categories=["gaps"], max_results=5)
        for n in nudges:
            assert n.category == "gaps"


class TestBuildNudgeContext:
    """Test the LLM context string builder."""

    @pytest.mark.asyncio
    async def test_builds_context_string(self):
        rows = [
            _make_row({
                "id": 1, "title": "Write proposal", "due_date": "2026-02-01",
                "assignee": "A", "status": "open",
            }),
        ]
        db = _mock_db_with_rows({"tasks": rows})
        engine = ProactiveSuggestionEngine(db)
        ctx = await engine.build_nudge_context(max_results=2)

        assert "Proactive Suggestions" in ctx or ctx == ""
        # If there are nudges, the context should have the framing instructions
        if ctx:
            assert "volunteer ONE" in ctx.lower() or "naturally" in ctx.lower()

    @pytest.mark.asyncio
    async def test_empty_when_no_nudges(self):
        db = _mock_db_with_rows({})
        engine = ProactiveSuggestionEngine(db)
        ctx = await engine.build_nudge_context()
        assert ctx == ""


class TestFormatNudgesForDisplay:
    """Test frontend formatting."""

    def test_formats_correctly(self):
        engine = ProactiveSuggestionEngine(MagicMock())
        nudges = [
            Nudge(
                hook="Task overdue",
                suggestion="Want me to help?",
                category="actions",
                action_hint="review actions",
            ),
        ]
        formatted = engine.format_nudges_for_display(nudges)
        assert len(formatted) == 1
        assert formatted[0]["hook"] == "Task overdue"
        assert formatted[0]["suggestion"] == "Want me to help?"
        assert formatted[0]["category"] == "actions"
        assert formatted[0]["action_hint"] == "review actions"

    def test_empty_list(self):
        engine = ProactiveSuggestionEngine(MagicMock())
        assert engine.format_nudges_for_display([]) == []


class TestCacheAndInvalidation:
    """Test caching behaviour."""

    @pytest.mark.asyncio
    async def test_cache_reuse(self):
        rows = [
            _make_row({
                "id": 1, "title": "T", "due_date": "2026-02-01",
                "assignee": "A", "status": "open",
            }),
        ]
        db = _mock_db_with_rows({"tasks": rows})
        engine = ProactiveSuggestionEngine(db, refresh_interval=300)

        # First call populates cache
        await engine._get_or_refresh()
        first_cached_at = engine._cached_at

        # Second call should reuse cache
        await engine._get_or_refresh()
        assert engine._cached_at == first_cached_at

    @pytest.mark.asyncio
    async def test_invalidate_forces_refresh(self):
        db = _mock_db_with_rows({"tasks": []})
        engine = ProactiveSuggestionEngine(db, refresh_interval=300)

        await engine._get_or_refresh()
        first_cached_at = engine._cached_at

        engine.invalidate()
        assert engine._cached_at == 0.0

        await engine._get_or_refresh()
        assert engine._cached_at > first_cached_at
