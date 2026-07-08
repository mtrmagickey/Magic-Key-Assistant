"""
Tests for the continuity layer — obligations, SOPs, rails, feedback,
trust controls, sweep jobs, backup/restore, seed workspace.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Path setup
ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))

import aiosqlite

# ── Test database fixture ─────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    """Create a real in-memory-like SQLite DB with migration 005 applied."""
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")

    # Apply migrations 001-005 (just the tables we need)
    migrations_dir = LEISURELLM_DIR / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sqlite.sql")):
        sql = sql_file.read_text(encoding="utf-8")
        # Split on semicolons and execute each statement
        for stmt in sql.split(";"):
            # Strip leading comment lines so startswith("--") doesn't eat real SQL
            lines = [l for l in stmt.strip().splitlines() if not l.strip().startswith("--")]
            clean = "\n".join(lines).strip()
            if clean:
                try:
                    await conn.execute(clean)
                except Exception:
                    pass  # Some statements may fail in test (ALTER on missing cols)
        await conn.commit()

    # Tables created by Python-based migration (fix_rainmaker_schema_v2.py)
    # that aren't in the .sqlite.sql files — needed for seed_workspace tests
    # Schema matches what LeadService actually uses (name, contact_info, etc.)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            source TEXT,
            source_id TEXT,
            status TEXT NOT NULL DEFAULT 'cold',
            priority TEXT DEFAULT 'medium',
            owner_user_id INTEGER,
            owner_username TEXT,
            contact_name TEXT,
            contact_info TEXT,
            value_estimate TEXT,
            next_action TEXT,
            next_action_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            last_activity TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            activity_type TEXT NOT NULL,
            summary TEXT,
            old_status TEXT,
            new_status TEXT,
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await conn.commit()

    # Wrap in a Database-like object
    class FakeDB:
        def __init__(self, connection):
            self.connection = connection
            self.database_path = db_path

        @staticmethod
        def _normalize_args(args):
            if len(args) != 1:
                return args
            value = args[0]
            if isinstance(value, list):
                return tuple(value)
            if isinstance(value, (tuple, dict)):
                return value
            return args

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def acquire(self):
            yield self.connection

        async def execute(self, query, *args):
            params = self._normalize_args(args)
            async with self.acquire() as conn:
                await conn.execute(query, params)
                await conn.commit()

        async def fetchone(self, query, *args):
            params = self._normalize_args(args)
            async with self.acquire() as conn:
                async with conn.execute(query, params) as cur:
                    return await cur.fetchone()

        async def fetchall(self, query, *args):
            params = self._normalize_args(args)
            async with self.acquire() as conn:
                async with conn.execute(query, params) as cur:
                    return await cur.fetchall()

    fake = FakeDB(conn)
    yield fake
    await conn.close()


# ============================================================
# ObligationService Tests
# ============================================================

class TestObligationService:
    @pytest.fixture
    def svc(self, db):
        from core.services.obligation_service import ObligationService
        return ObligationService(db)

    async def test_create_and_get(self, svc):
        obl_id = await svc.create(
            "Test Obligation",
            frequency="monthly",
            category="compliance",
            next_due="2026-03-01",
        )
        assert obl_id is not None
        assert obl_id > 0

        ob = await svc.get(obl_id)
        assert ob is not None
        assert ob["title"] == "Test Obligation"
        assert ob["frequency"] == "monthly"
        assert ob["category"] == "compliance"
        assert ob["status"] == "active"

    async def test_list_all(self, svc):
        await svc.create("Obl A", frequency="daily")
        await svc.create("Obl B", frequency="weekly", category="financial")
        items = await svc.list_all()
        assert len(items) >= 2

    async def test_list_filtered(self, svc):
        await svc.create("Financial Obl", frequency="monthly", category="financial")
        await svc.create("Compliance Obl", frequency="monthly", category="compliance")
        financial = await svc.list_all(category="financial")
        assert all(o["category"] == "financial" for o in financial)

    async def test_mark_completed(self, svc):
        obl_id = await svc.create("Complete Me", frequency="monthly")
        ok = await svc.mark_completed(obl_id)
        assert ok is True
        ob = await svc.get(obl_id)
        assert ob["status"] == "completed"
        assert ob["last_completed"] is not None

    async def test_update(self, svc):
        obl_id = await svc.create("Update Me", frequency="daily")
        ok = await svc.update(obl_id, frequency="weekly", notes="Updated note")
        assert ok is True
        ob = await svc.get(obl_id)
        assert ob["frequency"] == "weekly"
        assert ob["notes"] == "Updated note"

    async def test_stats(self, svc):
        await svc.create("Stats A")
        await svc.create("Stats B")
        stats = await svc.stats()
        assert "active" in stats
        assert stats["active"] >= 2


# ============================================================
# ActionService Tests
# ============================================================

class TestActionService:
    @pytest.fixture
    def svc(self, db):
        from core.services.action_service import ActionService
        return ActionService(db)

    async def test_create_and_mark_done(self, svc):
        action_id = await svc.create(
            "Ship prototype",
            assigned_to_username="alex",
            due_date="2030-01-01",
            tags=["prototype"],
        )
        action = await svc.get(action_id)
        assert action is not None
        assert action["title"] == "Ship prototype"
        assert action["assigned_to_username"] == "alex"

        ok = await svc.mark_done(action_id)
        assert ok is True
        updated = await svc.get(action_id)
        assert updated["status"] == "done"


# ============================================================
# DecisionService Tests
# ============================================================

class TestDecisionService:
    @pytest.fixture
    def svc(self, db):
        from core.services.decision_service import DecisionService
        return DecisionService(db)

    async def test_create_and_search(self, svc):
        decision_id = await svc.create(
            "Choose kiosk runtime",
            "Use Python for the prototype runtime.",
            rationale="Fastest path to a working prototype",
            decided_by=["Alex"],
            category="technical",
            tags=["runtime"],
        )
        decision = await svc.get(decision_id)
        assert decision is not None
        assert decision["title"] == "Choose kiosk runtime"

        matches = await svc.search("runtime")
        assert any(item["id"] == decision_id for item in matches)


# ============================================================
# LeadService Tests
# ============================================================

class TestLeadService:
    @pytest.fixture
    def svc(self, db):
        from core.services.lead_service import LeadService
        return LeadService(db)

    async def test_stage_changes_and_touchpoints_are_logged(self, svc):
        lead_id = await svc.create(
            "City Museum",
            source="manual",
            owner_username="sam",
            created_by_username="sam",
        )
        lead = await svc.get(lead_id)
        assert lead is not None
        assert lead["status"] == "cold"

        assert await svc.advance_stage(lead_id, "warm", by_username="sam", note="Qualified") is True
        assert await svc.log_touchpoint(lead_id, "follow_up", "Sent recap", by_username="sam") > 0

        updated = await svc.get(lead_id)
        assert updated["status"] == "warm"
        activities = await svc.get_activities(lead_id)
        assert len(activities) >= 2


# ============================================================
# MeetingService Tests
# ============================================================

class TestMeetingService:
    @pytest.fixture
    def svc(self, db):
        from core.services.meeting_service import MeetingService
        return MeetingService(db)

    async def test_schema_aligned_crud_and_linking(self, svc, db):
        from core.services.action_service import ActionService
        from core.services.decision_service import DecisionService

        meeting_id = await svc.create(
            "Launch Sync",
            summary="Discussed launch readiness",
            raw_text="We reviewed launch readiness and assigned follow-ups.",
            meeting_date="2026-03-01",
            recorded_by_username="sam",
        )
        meeting = await svc.get(meeting_id)
        assert meeting is not None
        assert meeting["summary"] == "Discussed launch readiness"

        matches = await svc.search("launch")
        assert any(item["id"] == meeting_id for item in matches)

        action_id = await ActionService(db).create("Send launch recap")
        decision_id = await DecisionService(db).create("Launch Date", "Ship on March 15")

        assert await svc.link_action(meeting_id, action_id) is True
        assert await svc.link_decision(meeting_id, decision_id) is True
        assert any(item["id"] == action_id for item in await svc.get_linked_actions(meeting_id))
        assert any(item["id"] == decision_id for item in await svc.get_linked_decisions(meeting_id))

        link_id = await svc.add_source_link(
            meeting_id,
            url="https://example.com/spec",
            title="Launch spec",
            source_type="web_search",
            added_by_username="sam",
        )
        links = await svc.get_source_links(meeting_id)
        assert any(item["id"] == link_id for item in links)
        metadata = json.loads(links[0]["metadata"])
        assert metadata["title"] == "Launch spec"


# ============================================================
# SOPService Tests — DEPRECATED (removed per product direction 2026-07)
# Tables preserved in migrations for backward compat.
# ============================================================


# ============================================================
# RailsService Tests
# Still worth covering because seed_workspace and rail maps rely on
# this state-machine behavior remaining coherent.
# ============================================================

class TestRailsService:
    @pytest.fixture
    def svc(self, db):
        from core.services.rails_service import RailsService
        return RailsService(db)

    async def test_create_rail_builds_ordered_stages_and_sets_current_stage(self, svc):
        rail_id = await svc.create_rail(
            "Weekly Operations Reset",
            "validate",
            description="A seeded operating-rhythm rail",
            use_default_stages=True,
        )

        rail = await svc.get_rail(rail_id)
        assert rail is not None
        assert rail["name"] == "Weekly Operations Reset"
        assert rail["current_stage_id"] is not None
        assert len(rail["stages"]) == 5
        assert rail["stages"][0]["id"] == rail["current_stage_id"]
        assert rail["stages"][0]["name"] == "Problem Definition"

    async def test_complete_then_advance_stage_updates_pointer_and_status(self, svc):
        rail_id = await svc.create_rail("Launch Flow", "launch", use_default_stages=True)
        rail = await svc.get_rail(rail_id)
        first_stage = rail["stages"][0]
        second_stage = rail["stages"][1]

        await svc.update_stage(first_stage["id"], status="in_progress")
        completed = await svc.complete_stage(first_stage["id"], actual_outputs=["Working MVP"])
        assert completed["success"] is True

        advanced = await svc.advance_stage(rail_id)
        assert advanced["success"] is True
        assert advanced["new_stage"] == second_stage["name"]

        updated_rail = await svc.get_rail(rail_id)
        updated_second_stage = next(stage for stage in updated_rail["stages"] if stage["id"] == second_stage["id"])
        assert updated_rail["current_stage_id"] == second_stage["id"]
        assert updated_second_stage["status"] == "in_progress"
        assert updated_second_stage["entered_at"] is not None

    async def test_create_from_map_uses_configured_stage_layout(self, svc):
        rail_id = await svc.create_from_map("stabilize")
        rail = await svc.get_rail(rail_id)

        assert rail is not None
        assert rail["rail_type"] == "operate"
        assert [stage["name"] for stage in rail["stages"]] == [
            "Intake",
            "Cadence",
            "Continuity",
        ]


# ============================================================
# FeedbackService Tests
# ============================================================

class TestFeedbackService:
    @pytest.fixture
    def svc(self, db):
        from core.services.feedback_service import FeedbackService
        return FeedbackService(db)

    async def test_create_and_get(self, svc):
        fb_id = await svc.create(
            "Something is broken",
            category="bug",
            severity="high",
        )
        assert fb_id > 0
        fb = await svc.get(fb_id)
        assert fb["summary"] == "Something is broken"
        assert fb["category"] == "bug"
        assert fb["status"] == "new"
        # Should have auto-snapshot
        assert fb["environment_snapshot"] is not None
        snap = json.loads(fb["environment_snapshot"])
        assert "python" in snap

    async def test_resolve(self, svc):
        fb_id = await svc.create("Fix me", category="bug")
        ok = await svc.resolve(fb_id, "Fixed in v2")
        assert ok is True
        fb = await svc.get(fb_id)
        assert fb["status"] == "resolved"
        assert fb["resolution"] == "Fixed in v2"


# ============================================================
# TrustGate Tests
# ============================================================

class TestTrustGate:
    @pytest.fixture
    def gate(self, db):
        from core.trust_controls import TrustGate
        return TrustGate(db, trust_config={
            "quiet_hours_enabled": False,  # Disable for predictable tests
            "require_change": True,
            "posts_per_job_per_day": 2,
        })

    async def test_allows_with_changes(self, gate):
        verdict = await gate.should_post("test_job", changes_summary="3 items changed")
        assert verdict.suppressed is False

    async def test_suppresses_no_change(self, gate):
        verdict = await gate.should_post("test_job", changes_summary=None)
        assert verdict.suppressed is True
        assert verdict.reason == "no_change"

    async def test_force_bypasses_all(self, gate):
        verdict = await gate.should_post("test_job", changes_summary=None, force=True)
        assert verdict.suppressed is False

    async def test_noise_budget(self, gate):
        # Post twice (within budget)
        for i in range(2):
            await gate.log_post("budget_job", changes_summary=f"change {i}")
        # Third should be suppressed
        verdict = await gate.should_post("budget_job", changes_summary="change 3")
        assert verdict.suppressed is True
        assert verdict.reason == "noise_budget"


# ============================================================
# Backup/Restore Tests
# ============================================================

class TestBackupRestore:
    def test_backup_database(self, tmp_path):
        import sqlite3

        from core.backup_restore import _BACKUP_DIR, backup_database

        # Create a test DB
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        # Monkey-patch backup dir
        import core.backup_restore as br
        original_dir = br._BACKUP_DIR
        br._BACKUP_DIR = tmp_path / "backups"

        try:
            dest = backup_database(db_path)
            assert dest.exists()
            assert dest.stat().st_size > 0

            # Verify backup is valid SQLite
            conn2 = sqlite3.connect(str(dest))
            row = conn2.execute("SELECT * FROM t").fetchone()
            assert row[0] == 1
            conn2.close()
        finally:
            br._BACKUP_DIR = original_dir

    def test_list_backups(self, tmp_path):
        import core.backup_restore as br
        from core.backup_restore import list_backups
        original_dir = br._BACKUP_DIR
        br._BACKUP_DIR = tmp_path / "backups"
        try:
            (tmp_path / "backups").mkdir()
            (tmp_path / "backups" / "assistant_test_20260101_000000.db").write_bytes(b"x" * 100)
            backups = list_backups()
            assert len(backups) == 1
            assert "assistant" in backups[0]["filename"]
        finally:
            br._BACKUP_DIR = original_dir

    def test_support_bundle(self, tmp_path):
        import sqlite3

        import core.backup_restore as br
        from core.backup_restore import create_support_bundle
        original_dir = br._BACKUP_DIR
        br._BACKUP_DIR = tmp_path / "backups"

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        try:
            bundle = create_support_bundle(db_path, base_dir=tmp_path)
            assert bundle.exists()
            assert bundle.suffix == ".zip"
        finally:
            br._BACKUP_DIR = original_dir


# ============================================================
# Sweep Jobs Tests
# ============================================================

class TestSweepJobs:
    async def test_obligation_sweep(self, db):
        from core.services.obligation_service import ObligationService
        from core.sweep_jobs import obligation_sweep

        svc = ObligationService(db)
        # Create an overdue obligation
        await svc.create("Overdue Thing", frequency="monthly", next_due="2020-01-01")
        # Create a future obligation
        await svc.create("Future Thing", frequency="monthly", next_due="2030-01-01")

        result = await obligation_sweep(db, upcoming_days=14)
        assert result.items_checked >= 2
        assert result.items_flagged >= 1
        assert "overdue" in result.summary.lower() or "on track" in result.summary.lower()

    async def test_sop_drift(self, db):
        from core.services.sop_service import SOPService
        from core.sweep_jobs import sop_drift_check

        svc = SOPService(db)
        # Create an SOP with no exercise/review dates (should be stale)
        await svc.create("Never Exercised", body="steps")

        result = await sop_drift_check(db, stale_days=0)  # 0 days = everything is stale
        assert result.items_flagged >= 1

    async def test_run_all_sweeps(self, db):
        from core.sweep_jobs import run_all_sweeps
        results = await run_all_sweeps(db)
        assert "obligation_sweep" in results
        assert "sop_drift_check" in results
        assert "rail_escalation_check" in results


# ============================================================
# Seed Workspace Tests
# ============================================================

class TestSeedWorkspace:
    async def test_seed_creates_records(self, db, tmp_path):
        from core.seed_workspace import is_seeded, seed_workspace
        assert not is_seeded(tmp_path)

        result = await seed_workspace(db, base_dir=tmp_path)
        assert "actions" in result
        assert len(result["actions"]) == 3
        assert len(result["obligations"]) == 4
        assert len(result["sops"]) == 2
        assert len(result["rails"]) == 1
        assert is_seeded(tmp_path)

    async def test_seed_idempotent(self, db, tmp_path):
        from core.seed_workspace import seed_workspace

        result1 = await seed_workspace(db, base_dir=tmp_path)
        assert "actions" in result1

        result2 = await seed_workspace(db, base_dir=tmp_path)
        assert result2.get("skipped") is True

    async def test_seed_force_does_not_duplicate_named_starter_records(self, db, tmp_path):
        from core.seed_workspace import seed_workspace

        await seed_workspace(db, base_dir=tmp_path)

        flag = tmp_path / ".seed_complete"
        if flag.exists():
            flag.unlink()

        result = await seed_workspace(db, base_dir=tmp_path, force=True)
        assert result["actions"] == []
        assert result["decisions"] == []
        assert result["leads"] == []
        assert result["obligations"] == []
        assert result["sops"] == []
        assert result["rails"] == []

        async with db.connection.execute("SELECT COUNT(*) FROM tasks") as cur:
            tasks = (await cur.fetchone())[0]
        async with db.connection.execute("SELECT COUNT(*) FROM decisions") as cur:
            decisions = (await cur.fetchone())[0]
        async with db.connection.execute("SELECT COUNT(*) FROM leads") as cur:
            leads = (await cur.fetchone())[0]
        async with db.connection.execute("SELECT COUNT(*) FROM obligations") as cur:
            obligations = (await cur.fetchone())[0]
        async with db.connection.execute("SELECT COUNT(*) FROM sops") as cur:
            sops = (await cur.fetchone())[0]
        async with db.connection.execute("SELECT COUNT(*) FROM rails") as cur:
            rails = (await cur.fetchone())[0]

        assert tasks == 3
        assert decisions == 2
        assert leads == 2
        assert obligations == 4
        assert sops == 2
        assert rails == 1
