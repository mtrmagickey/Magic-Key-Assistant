from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

from core.services.operational_continuity_service import ContinuityPolicy, OperationalContinuityService
from core.services.operational_record_service import OperationalRecordService


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "operational_continuity.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")

    for migration_name in (
        "018_canonical_operational_records.sqlite.sql",
        "020_operational_audit_trail.sqlite.sql",
        "023_operational_continuity_states.sqlite.sql",
        "025_add_operational_deliverables.sqlite.sql",
    ):
        sql = (LEISURELLM_DIR / "migrations" / migration_name).read_text(encoding="utf-8")
        await conn.executescript(sql)
    await conn.commit()

    class FakeDB:
        def __init__(self, connection):
            self.connection = connection
            self.database_path = db_path

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def acquire(self):
            yield self.connection

        async def execute(self, query, *args):
            async with self.acquire() as conn:
                await conn.execute(query, args[0] if len(args) == 1 and isinstance(args[0], (tuple, list, dict)) else args)
                await conn.commit()

        async def fetchone(self, query, *args):
            async with self.acquire() as conn:
                async with conn.execute(query, args[0] if len(args) == 1 and isinstance(args[0], (tuple, list, dict)) else args) as cur:
                    return await cur.fetchone()

        async def fetchall(self, query, *args):
            async with self.acquire() as conn:
                async with conn.execute(query, args[0] if len(args) == 1 and isinstance(args[0], (tuple, list, dict)) else args) as cur:
                    return await cur.fetchall()

    fake = FakeDB(conn)
    yield fake
    await conn.close()


class TestOperationalContinuityService:
    @pytest.fixture
    async def services(self, db):
        record_service = OperationalRecordService(db)
        continuity_service = OperationalContinuityService(
            db,
            policy=ContinuityPolicy(
                stale_action_days=14,
                stale_blocker_days=7,
                unresolved_decision_days=7,
                escalate_overdue_action_days=3,
                escalate_stale_blocker_days=3,
            ),
        )
        actor = await record_service.ensure_actor(
            actor_kind="web_user",
            external_ref="continuity-owner@example.local",
            display_name="Continuity Owner",
        )
        return record_service, continuity_service, actor

    async def test_overdue_action_rule_creates_active_state(self, services):
        record_service, continuity_service, actor = services
        now = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        record = await record_service.create_record(
            record_type="action",
            title="Chase contractor quote",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
            due_at=(now - timedelta(days=2)).date().isoformat(),
            state="open",
        )

        result = await continuity_service.run_sweep(now=now)
        states = await continuity_service.list_states(continuity_state="overdue")

        assert result["states_by_type"]["overdue"] == 1
        assert states[0]["record_id"] == record["id"]
        assert states[0]["details"]["rule"] == "overdue_action"

    async def test_stale_action_rule_uses_stale_after_or_inactivity(self, services):
        record_service, continuity_service, actor = services
        now = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        record = await record_service.create_record(
            record_type="action",
            title="Review swim timetable",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
            state="in_progress",
            stale_after_at=(now - timedelta(days=1)).isoformat(),
        )

        await continuity_service.run_sweep(now=now)
        states = await continuity_service.list_states(continuity_state="stale")

        assert any(state["record_id"] == record["id"] and state["record_type"] == "action" for state in states)

    async def test_unowned_action_rule_surfaces_without_discord_context(self, services):
        record_service, continuity_service, actor = services
        now = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        record = await record_service.create_record(
            record_type="action",
            title="Assign summer rota owner",
            created_by_actor_id=actor["id"],
            state="unowned",
        )

        await continuity_service.run_sweep(now=now)
        states = await continuity_service.list_states(continuity_state="unowned")

        assert states[0]["record_id"] == record["id"]
        assert states[0]["details"]["rule"] == "unowned_action"

    async def test_unresolved_decision_rule_uses_review_window(self, services):
        record_service, continuity_service, actor = services
        now = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        record = await record_service.create_record(
            record_type="decision",
            title="Keep toddler splash slot open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
            state="proposed",
            review_at=(now - timedelta(days=2)).isoformat(),
        )

        await continuity_service.run_sweep(now=now)
        states = await continuity_service.list_states(continuity_state="unresolved")

        assert states[0]["record_id"] == record["id"]
        assert states[0]["details"]["rule"] == "unresolved_decision"

    async def test_stale_blocker_rule_and_escalation_hook_can_coexist(self, services):
        record_service, continuity_service, actor = services
        now = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        record = await record_service.create_record(
            record_type="blocker",
            title="Awaiting replacement boiler part",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
            state="open",
            stale_after_at=(now - timedelta(days=4)).isoformat(),
        )

        result = await continuity_service.run_sweep(now=now)
        stale_states = await continuity_service.list_states(continuity_state="stale")
        escalated_states = await continuity_service.list_states(continuity_state="escalated")

        assert result["states_by_type"]["stale"] >= 1
        assert any(state["record_id"] == record["id"] and state["record_type"] == "blocker" for state in stale_states)
        assert any(state["record_id"] == record["id"] and state["details"].get("policy_hook") == "stale_blocker_age" for state in escalated_states)