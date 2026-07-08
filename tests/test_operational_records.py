"""Tests for the canonical operational record schema and validation layer."""

from __future__ import annotations

import sys
from pathlib import Path

import aiosqlite
import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

from core.operational_records import OperationalRecordValidationError, validate_transition
from core.services.operational_record_service import OperationalRecordService
from core.services.provenance_service import ProvenanceService


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "operational_records.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")

    for migration_name in (
        "018_canonical_operational_records.sqlite.sql",
        "021_operational_provenance.sqlite.sql",
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


class TestOperationalRecordMigration:
    async def test_migration_creates_schema_and_metadata(self, db):
        async with db.acquire() as conn:
            async with conn.execute("SELECT COUNT(*) FROM operational_record_types") as cur:
                type_count = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(*) FROM operational_record_states WHERE record_type = 'action'"
            ) as cur:
                action_states = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(*) FROM operational_record_transitions WHERE record_type = 'source_link'"
            ) as cur:
                source_transitions = (await cur.fetchone())[0]

        assert type_count >= 4
        assert action_states == 9
        assert source_transitions >= 8


class TestOperationalRecordService:
    @pytest.fixture
    def svc(self, db):
        return OperationalRecordService(db)

    async def test_create_action_record_persists_actor_and_event(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="alice@example.local", display_name="Alice")
        record = await svc.create_record(
            record_type="action",
            title="Schedule the filter inspection",
            summary="Book the quarterly pool filter inspection.",
            state="open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
            source_context_id="request:req-123",
            workspace_scope="main",
            project_scope="ops",
            due_at="2026-04-01",
            review_at="2026-03-25T10:00:00+00:00",
            stale_after_at="2026-03-30T10:00:00+00:00",
            rationale="Required before the Easter peak.",
        )
        events = await svc.list_events(record["id"])

        assert record["stable_id"].startswith("oprec_")
        assert record["record_type"] == "action"
        assert record["state"] == "open"
        assert record["created_by_actor_id"] == actor["id"]
        assert record["updated_by_actor_id"] == actor["id"]
        assert record["source_context_id"] == "request:req-123"
        assert len(events) == 1
        assert events[0]["event_type"] == "created"
        assert events[0]["actor_id"] == actor["id"]

    async def test_action_without_owner_must_be_unowned(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="ownerless@example.local")
        with pytest.raises(OperationalRecordValidationError, match="must use state 'unowned'"):
            await svc.create_record(
                record_type="action",
                title="Unassigned task",
                created_by_actor_id=actor["id"],
                state="open",
            )

    async def test_source_link_requires_source_context(self, svc):
        actor = await svc.ensure_actor(actor_kind="system_job", external_ref="link-checker")
        with pytest.raises(OperationalRecordValidationError, match="require source_context_id"):
            await svc.create_record(
                record_type="source_link",
                title="Link to safety policy",
                created_by_actor_id=actor["id"],
                state="active",
            )

    async def test_due_at_is_rejected_for_decision(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="decisioner@example.local")
        with pytest.raises(OperationalRecordValidationError, match="do not support due_at"):
            await svc.create_record(
                record_type="decision",
                title="Close the old sauna room",
                created_by_actor_id=actor["id"],
                due_at="2026-03-20",
            )

    async def test_valid_action_transition_updates_state_and_event_log(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="casey@example.local")
        record = await svc.create_record(
            record_type="action",
            title="Repair entry gate sensor",
            state="open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )

        record = await svc.transition_record(record_id=record["id"], new_state="in_progress", actor_id=actor["id"])
        record = await svc.transition_record(record_id=record["id"], new_state="done", actor_id=actor["id"])
        events = await svc.list_events(record["id"])

        assert record["state"] == "done"
        assert record["resolved_at"] is not None
        assert [event["event_type"] for event in events] == ["created", "transitioned", "transitioned"]
        assert events[-1]["previous_state"] == "in_progress"
        assert events[-1]["new_state"] == "done"

    async def test_invalid_action_transition_is_rejected(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="devon@example.local")
        record = await svc.create_record(
            record_type="action",
            title="Prepare rota update",
            state="done",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )

        with pytest.raises(ValueError, match="cannot transition"):
            await svc.transition_record(record_id=record["id"], new_state="in_progress", actor_id=actor["id"])

    async def test_invalid_decision_transition_is_rejected(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="erin@example.local")
        record = await svc.create_record(
            record_type="decision",
            title="Change opening hours",
            state="proposed",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )

        with pytest.raises(ValueError, match="cannot transition"):
            await svc.transition_record(record_id=record["id"], new_state="superseded", actor_id=actor["id"])

    async def test_blocker_transition_is_valid(self, svc):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="fran@example.local")
        record = await svc.create_record(
            record_type="blocker",
            title="Awaiting council permit",
            state="open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
            review_at="2026-03-28T09:00:00+00:00",
            stale_after_at="2026-03-30T09:00:00+00:00",
        )
        updated = await svc.transition_record(record_id=record["id"], new_state="mitigated", actor_id=actor["id"])
        assert updated["state"] == "mitigated"

    async def test_source_link_archival_sets_archived_at(self, svc):
        actor = await svc.ensure_actor(actor_kind="system_job", external_ref="link-auditor")
        record = await svc.create_record(
            record_type="source_link",
            title="Archived policy PDF",
            state="active",
            created_by_actor_id=actor["id"],
            source_context_id="url:https://example.local/policy.pdf",
            review_at="2026-03-28T10:00:00+00:00",
            stale_after_at="2026-04-15T10:00:00+00:00",
        )
        updated = await svc.transition_record(record_id=record["id"], new_state="archived", actor_id=actor["id"])
        assert updated["state"] == "archived"
        assert updated["archived_at"] is not None


class TestOperationalProvenanceService:
    @pytest.fixture
    def svc(self, db):
        return OperationalRecordService(db)

    @pytest.fixture
    def provenance(self, db):
        return ProvenanceService(db)

    async def test_explain_record_origin_supports_multiple_origins_and_evidence(self, svc, provenance):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="trace@example.local", display_name="Trace User")
        action = await svc.create_record(
            record_type="action",
            title="Replace the pool hoist battery",
            state="open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )

        await provenance.record_manual_origin(
            record_id=int(action["id"]),
            actor_id=actor["id"],
            actor_label="Trace User",
            surface="web",
        )
        await provenance.create_edge(
            source_entity_type="meeting",
            source_entity_id="12",
            target_entity_type="operational_record",
            target_entity_id=action["id"],
            relationship="origin",
            actor_id=actor["id"],
            source_details={"label": "Monday Ops Meeting", "summary": "Staff agreed the hoist battery must be replaced."},
        )
        await provenance.create_edge(
            source_entity_type="source_link",
            source_entity_id="vendor-quote-1",
            target_entity_type="operational_record",
            target_entity_id=action["id"],
            relationship="evidence",
            actor_id=actor["id"],
            source_details={
                "label": "Vendor quote",
                "summary": "Battery replacement quote from approved supplier.",
                "url": "https://example.local/quote.pdf",
            },
        )

        explanation = await provenance.explain_record_origin(int(action["id"]))

        assert explanation["record"]["label"] == "Replace the pool hoist battery"
        assert len(explanation["origins"]) == 2
        assert {origin["source"]["entity_type"] for origin in explanation["origins"]} == {"manual_creation", "meeting"}
        assert len(explanation["linked_evidence_objects"]) == 1
        assert explanation["linked_evidence_objects"][0]["label"] == "Vendor quote"
        assert "exists because of" in explanation["summary"]
        assert "evidence link" in explanation["summary"]

    async def test_blocker_links_are_queryable_in_both_directions(self, svc, provenance):
        actor = await svc.ensure_actor(actor_kind="web_user", external_ref="blockers@example.local", display_name="Blockers User")
        blocker = await svc.create_record(
            record_type="blocker",
            title="Council permit not yet approved",
            state="open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )
        action = await svc.create_record(
            record_type="action",
            title="Install accessible changing bench",
            state="open",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )
        decision = await svc.create_record(
            record_type="decision",
            title="Choose the south entrance contractor",
            state="proposed",
            owner_id=actor["id"],
            created_by_actor_id=actor["id"],
        )

        await provenance.create_edge(
            source_entity_type="operational_record",
            source_entity_id=blocker["id"],
            target_entity_type="operational_record",
            target_entity_id=action["id"],
            relationship="blocks",
            actor_id=actor["id"],
        )
        await provenance.create_edge(
            source_entity_type="operational_record",
            source_entity_id=blocker["id"],
            target_entity_type="operational_record",
            target_entity_id=decision["id"],
            relationship="blocks",
            actor_id=actor["id"],
        )

        blocker_edges = await provenance.list_edges(
            entity_type="operational_record",
            entity_id=blocker["id"],
            direction="outgoing",
            relationship="blocks",
        )
        action_edges = await provenance.list_edges(
            entity_type="operational_record",
            entity_id=action["id"],
            direction="incoming",
            relationship="blocks",
        )
        decision_edges = await provenance.list_edges(
            entity_type="operational_record",
            entity_id=decision["id"],
            direction="incoming",
            relationship="blocks",
        )

        assert len(blocker_edges) == 2
        assert {edge["target"]["label"] for edge in blocker_edges} == {
            "Install accessible changing bench",
            "Choose the south entrance contractor",
        }
        assert len(action_edges) == 1
        assert action_edges[0]["source"]["label"] == "Council permit not yet approved"
        assert len(decision_edges) == 1
        assert decision_edges[0]["source"]["label"] == "Council permit not yet approved"


class TestOperationalValidationHelpers:
    def test_action_transition_helper_accepts_valid_transition(self):
        assert validate_transition("action", "open", "in_progress") is True

    def test_action_transition_helper_rejects_invalid_transition(self):
        with pytest.raises(ValueError, match="cannot transition"):
            validate_transition("action", "done", "in_progress")