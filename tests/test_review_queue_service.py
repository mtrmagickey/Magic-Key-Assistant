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

from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_continuity_service import ContinuityPolicy, OperationalContinuityService
from core.services.operational_record_service import OperationalRecordService
from core.services.review_queue_service import ReviewQueueService


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "review_queue_service.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")

    for migration_name in (
        "018_canonical_operational_records.sqlite.sql",
        "020_operational_audit_trail.sqlite.sql",
        "021_operational_provenance.sqlite.sql",
        "022_operational_extraction_proposals.sqlite.sql",
        "023_operational_continuity_states.sqlite.sql",
        "024_operational_review_queue.sqlite.sql",
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


class TestReviewQueueService:
    @pytest.fixture
    async def services(self, db):
        record_service = OperationalRecordService(db)
        proposal_service = ExtractionProposalService(db)
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
        review_queue = ReviewQueueService(db)
        reviewer = await record_service.ensure_actor(
            actor_kind="web_user",
            external_ref="reviewer@example.local",
            display_name="Reviewer",
        )
        teammate = await record_service.ensure_actor(
            actor_kind="web_user",
            external_ref="teammate@example.local",
            display_name="Teammate",
        )
        return record_service, proposal_service, continuity_service, review_queue, reviewer, teammate

    async def test_aggregates_mixed_queue_filters_and_deduplicates_blocker_states(self, services):
        record_service, proposal_service, continuity_service, review_queue, reviewer, teammate = services
        now = datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc)

        await proposal_service.create_proposal(
            record_type="action",
            title="Escalate plant room contractor",
            summary="Low-confidence action proposal from imported notes.",
            extracted_fields={
                "title": "Escalate plant room contractor",
                "summary": "Low-confidence action proposal from imported notes.",
                "workspace_scope": "ops",
                "project_scope": "boiler",
            },
            created_by_actor_id=reviewer["id"],
            record_confidence=0.71,
            field_confidences={"title": 0.93, "summary": 0.41},
            rationale="Imported note suggested an action but summary confidence is low.",
            source_entity_type="knowledge_note",
            source_entity_id="handoff-001.md",
            source_context_id="knowledge_note:handoff-001.md",
            source_details={"label": "Friday handoff", "workspace_scope": "ops", "project_scope": "boiler"},
        )
        await proposal_service.create_proposal(
            record_type="decision",
            title="Approve toddler timetable refresh",
            summary="Higher-confidence decision proposal awaiting review.",
            extracted_fields={"title": "Approve toddler timetable refresh", "owner_id": reviewer["id"]},
            created_by_actor_id=reviewer["id"],
            record_confidence=0.92,
            field_confidences={"title": 0.95, "summary": 0.91},
            rationale="Meeting summary contained a tentative approval decision.",
            source_entity_type="meeting_note",
            source_entity_id="44",
            source_context_id="meeting:44",
            source_details={"label": "Programming meeting"},
        )

        await record_service.create_record(
            record_type="action",
            title="Chase overdue boiler contractor quote",
            owner_id=reviewer["id"],
            created_by_actor_id=reviewer["id"],
            workspace_scope="ops",
            project_scope="boiler",
            due_at=(now - timedelta(days=4)).date().isoformat(),
            state="open",
        )
        await record_service.create_record(
            record_type="action",
            title="Assign summer rota owner",
            created_by_actor_id=reviewer["id"],
            workspace_scope="aquatics",
            project_scope="summer-rota",
            state="unowned",
        )
        await record_service.create_record(
            record_type="decision",
            title="Confirm Easter pool hours",
            owner_id=reviewer["id"],
            created_by_actor_id=reviewer["id"],
            workspace_scope="aquatics",
            project_scope="easter",
            review_at=(now - timedelta(days=3)).isoformat(),
            state="proposed",
        )
        blocker = await record_service.create_record(
            record_type="blocker",
            title="Replacement boiler part unavailable",
            owner_id=teammate["id"],
            created_by_actor_id=reviewer["id"],
            workspace_scope="ops",
            project_scope="boiler",
            stale_after_at=(now - timedelta(days=4)).isoformat(),
            state="open",
        )

        await continuity_service.run_sweep(now=now)
        items = await review_queue.list_items(current_actor_id=reviewer["id"], limit=50)
        item_types = {item["item_type"] for item in items}

        assert item_types == {
            "extraction_proposal_low_confidence",
            "extraction_proposal_pending_human_review",
            "action_overdue",
            "action_unowned",
            "decision_unresolved",
            "blocker_escalated_or_stale",
        }

        blocker_items = [item for item in items if item["item_type"] == "blocker_escalated_or_stale"]
        assert len(blocker_items) == 1
        assert sorted(blocker_items[0]["continuity_states"]) == ["escalated", "stale"]
        assert blocker_items[0]["operational_record_id"] == blocker["id"]

        ops_items = await review_queue.list_items(workspace_scope="ops", current_actor_id=reviewer["id"], limit=50)
        assert all(item["workspace_scope"] == "ops" for item in ops_items)

        mine_items = await review_queue.list_items(scope="mine", current_actor_id=reviewer["id"], limit=50)
        assert any(item["item_type"] == "action_overdue" for item in mine_items)
        assert all(item["owner_id"] in {None, reviewer["id"]} or item["created_by_actor_id"] == reviewer["id"] for item in mine_items)

        high_items = await review_queue.list_items(severity="high", current_actor_id=reviewer["id"], limit=50)
        assert {item["item_type"] for item in high_items} == {"action_overdue"}

        critical_items = await review_queue.list_items(severity="critical", current_actor_id=reviewer["id"], limit=50)
        assert {item["item_type"] for item in critical_items} == {"blocker_escalated_or_stale"}

    async def test_repeated_deferrals_raise_severity_and_force_weekly_review_visibility(self, services):
        record_service, _, continuity_service, review_queue, reviewer, _ = services
        now = datetime.now(tz=timezone.utc)
        decision = await record_service.create_record(
            record_type="decision",
            title="Approve revised swim school pricing",
            owner_id=reviewer["id"],
            created_by_actor_id=reviewer["id"],
            review_at=(now - timedelta(days=2)).isoformat(),
            workspace_scope="aquatics",
            project_scope="pricing",
            state="proposed",
        )
        await continuity_service.run_sweep(now=now)

        items = await review_queue.list_items(current_actor_id=reviewer["id"], limit=20)
        unresolved = next(item for item in items if item["operational_record_id"] == decision["id"])
        assert unresolved["severity"] == "medium"

        first_until = (now + timedelta(days=5)).isoformat()
        second_until = (now + timedelta(days=6)).isoformat()
        third_until = (now + timedelta(days=6)).isoformat()

        await review_queue.apply_action(item_id=unresolved["id"], action="defer", actor_id=reviewer["id"], defer_until=first_until, rationale="Need finance input first.")
        after_first = await review_queue.get_item(unresolved["id"], include_deferred=True)
        assert after_first["severity"] == "medium"
        assert after_first["defer_count"] == 1

        await review_queue.apply_action(item_id=unresolved["id"], action="defer", actor_id=reviewer["id"], defer_until=second_until, rationale="Still waiting on pricing assumptions.")
        after_second = await review_queue.get_item(unresolved["id"], include_deferred=True)
        assert after_second["severity"] == "high"
        assert after_second["defer_count"] == 2
        assert after_second["must_surface_in_weekly_review"] is True

        await review_queue.apply_action(item_id=unresolved["id"], action="defer", actor_id=reviewer["id"], defer_until=third_until, rationale="Steering group has not met yet.")
        after_third = await review_queue.get_item(unresolved["id"], include_deferred=True)
        assert after_third["severity"] == "critical"
        assert after_third["defer_count"] == 3
        assert after_third["escalation_destination"]["route"] == "weekly_review"

        weekly_session = await review_queue.generate_review_session(cadence="weekly", actor_id=reviewer["id"], scope="all")
        snapshot_item_ids = {item["review_item_id"] for item in weekly_session["items"]}
        assert unresolved["id"] in snapshot_item_ids

        completed = await review_queue.complete_review_session(
            session_id=weekly_session["session_id"],
            actor_id=reviewer["id"],
            completion_notes="Weekly review complete.",
        )
        assert completed["completed_at"] is not None
        assert completed["completed_by_actor_id"] == reviewer["id"]

    async def test_overdue_actions_cannot_be_deferred_beyond_policy_limit(self, services):
        record_service, _, continuity_service, review_queue, reviewer, _ = services
        now = datetime.now(tz=timezone.utc)
        action = await record_service.create_record(
            record_type="action",
            title="Clear overdue poolside signage fix",
            owner_id=reviewer["id"],
            created_by_actor_id=reviewer["id"],
            due_at=(now - timedelta(days=5)).date().isoformat(),
            state="open",
        )
        await continuity_service.run_sweep(now=now)

        overdue_item = next(
            item for item in await review_queue.list_items(current_actor_id=reviewer["id"], limit=20)
            if item["operational_record_id"] == action["id"] and item["item_type"] == "action_overdue"
        )

        with pytest.raises(ValueError, match="can only be deferred up to 7 days"):
            await review_queue.apply_action(
                item_id=overdue_item["id"],
                action="defer",
                actor_id=reviewer["id"],
                defer_until=(now + timedelta(days=30)).isoformat(),
                rationale="Trying to push this out too far.",
            )