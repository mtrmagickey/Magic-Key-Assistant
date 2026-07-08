"""Tests for the minimum viable work packet kernel."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")
os.environ.setdefault("ADMIN_AUTH_DISABLED", "1")


async def _apply_sql_migrations(conn: aiosqlite.Connection) -> None:
    migrations_dir = LEISURELLM_DIR / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sqlite.sql")):
        sql = sql_file.read_text(encoding="utf-8")
        for stmt in sql.split(";"):
            lines = [line for line in stmt.strip().splitlines() if not line.strip().startswith("--")]
            clean = "\n".join(lines).strip()
            if clean:
                try:
                    await conn.execute(clean)
                except Exception:
                    pass
        await conn.commit()


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "work-packets.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await _apply_sql_migrations(conn)

    class FakeDB:
        def __init__(self, connection):
            self.connection = connection
            self.database_path = db_path

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


class TestWorkPacketMigration:
    async def test_migration_creates_work_packet_tables(self, db):
        async with db.acquire() as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('work_packets', 'packet_links', 'packet_events')"
            ) as cur:
                rows = await cur.fetchall()

        names = {row[0] for row in rows}
        assert names == {"work_packets", "packet_links", "packet_events"}


class TestInboxWorkPacketFlow:
    @pytest.mark.asyncio
    async def test_inbox_flow_creates_links_and_completes_after_review(self, db):
        from admin.routers.inbox import (
            CreateThreadRequest,
            PatchThreadRequest,
            SaveResponseRequest,
            api_create_thread,
            api_patch_thread,
            api_save_response,
        )
        from core.services.work_packet_service import WorkPacketService

        create_result = await api_create_thread(
            CreateThreadRequest(message="Need a plan for next week", stream=True),
            db=db,
        )
        thread_id = create_result["thread_id"]

        packet_svc = WorkPacketService(db)
        packet = await packet_svc.get_by_key(f"inbox-thread:{thread_id}")
        assert packet is not None
        assert packet["packet_type"] == "inbox_followup"
        assert packet["status"] == "active"
        assert packet["lane"] == "assistive"
        assert packet["created_from_type"] == "inbox_thread"
        assert packet["created_from_id"] == str(thread_id)

        async with db.acquire() as conn:
            async with conn.execute(
                "SELECT link_role, target_type, target_id, is_primary FROM packet_links WHERE packet_id = ?",
                (packet["id"],),
            ) as cur:
                links = await cur.fetchall()
        assert len(links) == 1
        assert links[0][0] == "primary_target"
        assert links[0][1] == "inbox_thread"
        assert links[0][2] == str(thread_id)
        assert links[0][3] == 1

        await api_save_response(
            thread_id,
            SaveResponseRequest(response="Here is the proposed plan.", sources=["docs/plan.md"]),
            db=db,
        )
        awaiting = await packet_svc.get(packet["id"])
        assert awaiting["status"] == "awaiting_human"
        assert awaiting["approval_required"] == 1
        assert awaiting["approval_status"] == "pending"

        await api_patch_thread(thread_id, PatchThreadRequest(status="read"), db=db)
        completed = await packet_svc.get(packet["id"])
        assert completed["status"] == "completed"
        assert completed["approval_status"] == "approved"
        assert completed["completed_at"] is not None

        events = await packet_svc.list_events(packet["id"])
        event_types = [event["event_type"] for event in events]
        assert event_types == [
            "packet_created",
            "packet_linked",
            "approval_requested",
            "approval_received",
            "packet_completed",
        ]
        final_status = [event["to_status"] for event in events if event["to_status"]][-1]
        assert final_status == "completed"

    @pytest.mark.asyncio
    async def test_inbox_packet_does_not_duplicate_full_thread_payload(self, db):
        from admin.routers.inbox import CreateThreadRequest, api_create_thread
        from core.services.work_packet_service import WorkPacketService

        message = "This is a very long inbox message that should remain authoritative in inbox_messages rather than being copied into work_packets."
        create_result = await api_create_thread(
            CreateThreadRequest(message=message, stream=True),
            db=db,
        )

        packet = await WorkPacketService(db).get_by_key(f"inbox-thread:{create_result['thread_id']}")
        assert packet is not None
        assert message not in (packet["title"] or "")
        assert message not in (packet["objective"] or "")
        assert message not in (packet["current_summary"] or "")


class TestDeterministicObligationPacketFlow:
    @pytest.mark.asyncio
    async def test_obligation_completion_creates_and_completes_packet(self, db):
        from admin.routers.continuity import api_complete_obligation
        from core.services.obligation_service import ObligationService
        from core.services.work_packet_service import WorkPacketService

        obligation_svc = ObligationService(db)
        obl_id = await obligation_svc.create(
            "Renew pool certification",
            frequency="monthly",
            next_due="2026-03-20",
            category="compliance",
        )

        result = await api_complete_obligation(obl_id, db=db)
        assert result["success"] is True

        packet = await WorkPacketService(db).get_latest_by_source("obligation", obl_id)
        assert packet is not None
        assert packet["packet_type"] == "obligation_followup"
        assert packet["lane"] == "deterministic"
        assert packet["status"] == "completed"
        assert packet["completion_summary"] is not None

        obligation = await obligation_svc.get(obl_id)
        assert obligation["status"] == "completed"

        async with db.acquire() as conn:
            async with conn.execute(
                "SELECT target_type, target_id, is_primary FROM packet_links WHERE packet_id = ?",
                (packet["id"],),
            ) as cur:
                links = await cur.fetchall()
        assert len(links) == 1
        assert links[0][0] == "obligation"
        assert links[0][1] == str(obl_id)
        assert links[0][2] == 1

        events = await WorkPacketService(db).list_events(packet["id"])
        assert [event["event_type"] for event in events] == [
            "packet_created",
            "packet_linked",
            "packet_completed",
        ]
