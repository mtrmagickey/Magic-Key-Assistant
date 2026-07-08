from __future__ import annotations

import sys
from pathlib import Path

import aiosqlite
import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_record_service import OperationalRecordService
from core.services.provenance_service import ProvenanceService
from services.conversation_miner import mine_recent_conversations


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "operational_extraction_proposals.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")

    for migration_name in (
        "018_canonical_operational_records.sqlite.sql",
        "020_operational_audit_trail.sqlite.sql",
        "021_operational_provenance.sqlite.sql",
        "022_operational_extraction_proposals.sqlite.sql",
        "025_add_operational_deliverables.sqlite.sql",
    ):
        sql = (LEISURELLM_DIR / "migrations" / migration_name).read_text(encoding="utf-8")
        await conn.executescript(sql)

    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_active TEXT NOT NULL DEFAULT (datetime('now')),
            turn_count INTEGER DEFAULT 0,
            summary TEXT DEFAULT '',
            topics TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active',
            mined_at TEXT
        );
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
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


class FakeLLM:
    model = "qwen2.5:7b"

    def __init__(self, response: str):
        self.response = response

    async def complete(self, prompt: str, max_tokens: int = 500, temperature: float = 0.1):
        return self.response


class TestExtractionProposalService:
    @pytest.fixture
    def proposal_service(self, db):
        return ExtractionProposalService(db)

    @pytest.fixture
    def record_service(self, db):
        return OperationalRecordService(db)

    async def test_accepting_edited_proposal_creates_canonical_record_and_preserves_original_fields(self, proposal_service, record_service, db):
        reviewer = await record_service.ensure_actor(
            actor_kind="web_user",
            external_ref="reviewer@example.local",
            display_name="Reviewer",
        )
        proposal = await proposal_service.create_proposal(
            record_type="action",
            title="Book annual boiler inspection",
            summary="Schedule the annual boiler inspection before winter.",
            extracted_fields={
                "title": "Book annual boiler inspection",
                "summary": "Schedule the annual boiler inspection before winter.",
                "due_at": "2026-10-01",
            },
            created_by_actor_id=reviewer["id"],
            record_confidence=0.72,
            field_confidences={"title": 0.9, "summary": 0.66, "due_at": 0.52},
            rationale="The user explicitly asked maintenance to schedule it.",
            supporting_snippet="User: Please get the annual boiler inspection booked before October.",
            source_entity_type="conversation_session",
            source_entity_id="sess-accept-1",
            source_context_id="conversation_session:sess-accept-1",
            source_details={"label": "Operations chat", "summary": "Maintenance planning exchange."},
            extraction_metadata={"pipeline": "conversation_miner"},
        )

        accepted = await proposal_service.accept_proposal(
            proposal_id=int(proposal["id"]),
            actor_id=reviewer["id"],
            final_fields={
                "title": "Book the annual boiler inspection",
                "summary": "Schedule the annual boiler inspection with the certified contractor.",
                "due_at": "2026-09-25",
            },
            review_notes="Tightened the title and summary before acceptance.",
        )

        updated_proposal = accepted["proposal"]
        record = accepted["record"]
        provenance = ProvenanceService(db)
        edges = await provenance.list_edges(
            entity_type="conversation_session",
            entity_id="sess-accept-1",
            direction="outgoing",
            relationship="origin",
        )

        assert updated_proposal["status"] == "accepted"
        assert updated_proposal["extracted_fields"]["title"] == "Book annual boiler inspection"
        assert updated_proposal["final_fields"]["title"] == "Book the annual boiler inspection"
        assert record["record_type"] == "action"
        assert record["title"] == "Book the annual boiler inspection"
        assert record["state"] == "unowned"
        assert len(edges) == 1
        assert edges[0]["target"]["label"] == "Book the annual boiler inspection"

    async def test_low_confidence_listing_uses_critical_field_confidence(self, proposal_service, record_service):
        actor = await record_service.ensure_actor(actor_kind="system_job", external_ref="proposal-seed")
        await proposal_service.create_proposal(
            record_type="decision",
            title="Choose resurfacing vendor",
            extracted_fields={
                "title": "Choose resurfacing vendor",
                "decision": "Use BlueTile for the resurfacing project.",
                "summary": "Vendor choice from the maintenance review.",
            },
            created_by_actor_id=actor["id"],
            record_confidence=0.91,
            field_confidences={"title": 0.95, "decision": 0.42, "summary": 0.9},
            source_entity_type="conversation_session",
            source_entity_id="sess-low-1",
            source_context_id="conversation_session:sess-low-1",
        )
        await proposal_service.create_proposal(
            record_type="decision",
            title="Approve locker signage",
            extracted_fields={
                "title": "Approve locker signage",
                "decision": "Approve the final signage layout.",
                "summary": "Signage approved in design review.",
            },
            created_by_actor_id=actor["id"],
            record_confidence=0.88,
            field_confidences={"title": 0.9, "decision": 0.86, "summary": 0.82},
            source_entity_type="conversation_session",
            source_entity_id="sess-low-2",
            source_context_id="conversation_session:sess-low-2",
        )

        low_confidence = await proposal_service.list_low_confidence_proposals(threshold=0.6)

        assert len(low_confidence) == 1
        assert low_confidence[0]["title"] == "Choose resurfacing vendor"
        assert low_confidence[0]["effective_confidence"] == pytest.approx(0.42)
        assert low_confidence[0]["low_confidence_fields"] == ["decision"]
        assert low_confidence[0]["requires_review"] is True

    async def test_conversation_miner_creates_pending_operational_proposals(self, db):
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO conversation_sessions (id, turn_count, summary, status) VALUES (?, ?, ?, ?)",
                ("sess-miner-1", 4, "Operations handoff", "active"),
            )
            await conn.executemany(
                "INSERT INTO conversation_turns (session_id, role, content) VALUES (?, ?, ?)",
                [
                    ("sess-miner-1", "user", "We need to replace the broken lane rope before Friday."),
                    ("sess-miner-1", "assistant", "Understood."),
                    ("sess-miner-1", "user", "Use the vendor guide at https://example.local/lane-rope.pdf as the reference."),
                    ("sess-miner-1", "assistant", "I will mark that for review."),
                ],
            )
            await conn.commit()

        llm = FakeLLM(
            """
            {
              "has_proposals": true,
              "proposals": [
                {
                  "record_type": "action",
                  "title": "Replace the broken lane rope",
                  "summary": "Replace the broken lane rope before Friday.",
                  "fields": {
                    "title": "Replace the broken lane rope",
                    "summary": "Replace the broken lane rope before Friday.",
                    "due_at": "2026-03-20"
                  },
                  "confidence": 0.74,
                  "field_confidence": {"title": 0.92, "summary": 0.8, "due_at": 0.58},
                  "rationale": "A concrete maintenance action was stated.",
                  "supporting_snippet": "We need to replace the broken lane rope before Friday."
                },
                {
                  "record_type": "source_link",
                  "title": "Lane rope vendor guide",
                  "summary": "Reference PDF for the replacement.",
                  "fields": {
                    "title": "Lane rope vendor guide",
                    "summary": "Reference PDF for the replacement.",
                    "url": "https://example.local/lane-rope.pdf"
                  },
                  "confidence": 0.83,
                  "field_confidence": {"title": 0.9, "summary": 0.78, "url": 0.95},
                  "rationale": "A concrete URL was cited as evidence.",
                  "supporting_snippet": "Use the vendor guide at https://example.local/lane-rope.pdf as the reference."
                }
              ]
            }
            """
        )

        result = await mine_recent_conversations(db=db, llm_service=llm, min_turns=2, max_extracts=5)
        proposal_service = ExtractionProposalService(db)
        proposals = await proposal_service.list_proposals(status="pending", limit=10)

        assert result["sessions_scanned"] == 1
        assert result["proposals_created"] == 2
        assert result["extracts_saved"] == 2
        assert {proposal["record_type"] for proposal in proposals} == {"action", "source_link"}
        assert all(proposal["status"] == "pending" for proposal in proposals)
