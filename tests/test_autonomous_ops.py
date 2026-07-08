"""
Tests for cogs/AutonomousOps.py — Autonomous operations & scheduling.

Covers:
- Module-level helpers: _now_utc_iso, _tags_json, _next_thursday_due, _week_start_monday
- AutonomousOps._is_first_tuesday
- AutonomousOps._record_job_run / _job_already_ran
- AutonomousOps._pm_owner_wip (DB query)
- AutonomousOps._pm_get_thread_purpose (cache + DB)

Run with: pytest tests/test_autonomous_ops.py -v
"""

import sys
from datetime import datetime, timedelta
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# AutonomousOps imports 'docsprep' which may not be installed in the test env.
# Pre-seed sys.modules with a stub so the import doesn't fail.
if "docsprep" not in sys.modules:
    sys.modules["docsprep"] = ModuleType("docsprep")

from cogs.AutonomousOps import (
    _next_thursday_due,
    _now_utc_iso,
    _tags_json,
    _week_start_monday,
)

EASTERN = ZoneInfo("America/New_York")


# ============================================================
# Module-level helpers
# ============================================================

class TestNowUtcIso:

    @pytest.mark.unit
    def test_returns_iso_with_z_suffix(self):
        result = _now_utc_iso()
        assert result.endswith("Z")
        assert "T" in result

    @pytest.mark.unit
    def test_is_valid_iso(self):
        result = _now_utc_iso()
        # Should parse without error
        datetime.fromisoformat(result.rstrip("Z"))


class TestTagsJson:

    @pytest.mark.unit
    def test_serialises_list(self):
        assert _tags_json(["a", "b"]) == '["a", "b"]'

    @pytest.mark.unit
    def test_empty_list(self):
        assert _tags_json([]) == "[]"


class TestNextThursdayDue:

    @pytest.mark.unit
    def test_from_monday(self):
        # 2026-01-19 is a Monday
        mon = datetime(2026, 1, 19, 10, 0, tzinfo=EASTERN)
        result = _next_thursday_due(mon)
        assert result == "2026-01-22"  # next Thursday

    @pytest.mark.unit
    def test_from_thursday(self):
        # If today is Thursday, next Thursday is 7 days away
        thu = datetime(2026, 1, 22, 10, 0, tzinfo=EASTERN)
        result = _next_thursday_due(thu)
        assert result == "2026-01-29"

    @pytest.mark.unit
    def test_from_friday(self):
        fri = datetime(2026, 1, 23, 10, 0, tzinfo=EASTERN)
        result = _next_thursday_due(fri)
        assert result == "2026-01-29"


class TestWeekStartMonday:

    @pytest.mark.unit
    def test_monday_returns_self(self):
        mon = datetime(2026, 1, 19, 10, 0, tzinfo=EASTERN)
        assert _week_start_monday(mon) == "2026-01-19"

    @pytest.mark.unit
    def test_wednesday_returns_monday(self):
        wed = datetime(2026, 1, 21, 10, 0, tzinfo=EASTERN)
        assert _week_start_monday(wed) == "2026-01-19"

    @pytest.mark.unit
    def test_sunday_returns_monday(self):
        sun = datetime(2026, 1, 25, 10, 0, tzinfo=EASTERN)
        assert _week_start_monday(sun) == "2026-01-19"


# ============================================================
# AutonomousOps fixture (suppresses all task.start() calls)
# ============================================================

@pytest.fixture
def auto_ops(mock_bot):
    """Create an AutonomousOps instance with all background tasks suppressed."""
    from cogs.AutonomousOps import AutonomousOps

    with patch("discord.ext.tasks.Loop.start"):
        with patch("cogs.AutonomousOps.AutonomousOps.cog_unload"):
            ops = AutonomousOps(mock_bot)
    return ops


# ============================================================
# _is_first_tuesday
# ============================================================

class TestIsFirstTuesday:

    @pytest.mark.unit
    def test_first_tuesday_jan_2026(self, auto_ops):
        # 2026-01-06 is the first Tuesday of January 2026
        dt = datetime(2026, 1, 6, 10, 0, tzinfo=EASTERN)
        assert auto_ops._is_first_tuesday(dt) is True

    @pytest.mark.unit
    def test_second_tuesday_returns_false(self, auto_ops):
        dt = datetime(2026, 1, 13, 10, 0, tzinfo=EASTERN)
        assert auto_ops._is_first_tuesday(dt) is False

    @pytest.mark.unit
    def test_monday_returns_false(self, auto_ops):
        dt = datetime(2026, 1, 5, 10, 0, tzinfo=EASTERN)
        assert auto_ops._is_first_tuesday(dt) is False

    @pytest.mark.unit
    def test_first_tuesday_feb_2026(self, auto_ops):
        # 2026-02-03 is the first Tuesday of February
        dt = datetime(2026, 2, 3, 10, 0, tzinfo=EASTERN)
        assert auto_ops._is_first_tuesday(dt) is True


# ============================================================
# _record_job_run / _job_already_ran
# ============================================================

class TestJobIdempotency:

    @pytest.mark.unit
    async def test_record_job_run_no_db(self, auto_ops):
        """No DB → silent no-op."""
        auto_ops.bot.db = None
        await auto_ops._record_job_run("test_job", "2026-01-20")
        # Should not raise

    @pytest.mark.unit
    async def test_record_job_run_calls_db(self, auto_ops):
        mock_db = MagicMock()
        mock_db.complete_job_run = AsyncMock()
        auto_ops.bot.db = mock_db

        await auto_ops._record_job_run("daily_digest", "2026-01-20")
        mock_db.complete_job_run.assert_awaited_once_with("daily_digest", "2026-01-20")

    @pytest.mark.unit
    async def test_job_already_ran_no_db(self, auto_ops):
        auto_ops.bot.db = None
        result = await auto_ops._job_already_ran("test_job", "2026-01-20")
        assert result is False

    @pytest.mark.unit
    async def test_job_already_ran_returns_true_when_exists(self, auto_ops):
        """record_job_run returns False (already exists) → _job_already_ran returns True."""
        mock_db = MagicMock()
        mock_db.record_job_run = AsyncMock(return_value=False)
        auto_ops.bot.db = mock_db

        result = await auto_ops._job_already_ran("daily_digest", "2026-01-20")
        assert result is True

    @pytest.mark.unit
    async def test_job_already_ran_returns_false_for_new(self, auto_ops):
        """record_job_run returns True (new job recorded) → _job_already_ran returns False."""
        mock_db = MagicMock()
        mock_db.record_job_run = AsyncMock(return_value=True)
        auto_ops.bot.db = mock_db

        result = await auto_ops._job_already_ran("daily_digest", "2026-01-20")
        assert result is False


# ============================================================
# _pm_owner_wip
# ============================================================

class TestPmOwnerWip:

    @pytest.mark.unit
    async def test_no_db_returns_zero(self, auto_ops):
        auto_ops.bot.db = None
        assert await auto_ops._pm_owner_wip(12345) == 0

    @pytest.mark.unit
    async def test_returns_count_from_db(self, auto_ops):
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value=(3,))
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_db = MagicMock()
        mock_db.acquire = MagicMock(return_value=mock_conn)
        auto_ops.bot.db = mock_db

        result = await auto_ops._pm_owner_wip(12345)
        assert result == 3


# ============================================================
# _pm_get_thread_purpose
# ============================================================

class TestPmGetThreadPurpose:

    @pytest.mark.unit
    async def test_returns_cached_value(self, auto_ops):
        auto_ops._pm_thread_purpose[999] = "weekly-standup"
        result = await auto_ops._pm_get_thread_purpose(999)
        assert result == "weekly-standup"

    @pytest.mark.unit
    async def test_no_db_returns_none(self, auto_ops):
        auto_ops.bot.db = None
        result = await auto_ops._pm_get_thread_purpose(999)
        assert result is None

    @pytest.mark.unit
    async def test_fetches_from_db_and_caches(self, auto_ops):
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value=("sprint-review",))
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_db = MagicMock()
        mock_db.acquire = MagicMock(return_value=mock_conn)
        auto_ops.bot.db = mock_db

        result = await auto_ops._pm_get_thread_purpose(888)
        assert result == "sprint-review"
        # Should cache it
        assert auto_ops._pm_thread_purpose.get(888) == "sprint-review"
