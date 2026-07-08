from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")

from core.inbox_recovery_runner import InboxRecoveryScheduler


@pytest.fixture
def auth_client(tmp_path, monkeypatch, event_loop):
    db_path = tmp_path / "inbox_recovery_web.db"
    monkeypatch.setenv("ADMIN_AUTH_DISABLED", "0")
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    import admin.dependencies as dependencies
    import admin.server as server
    from database import Database

    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()
    server._login_attempts.clear()

    db = Database(str(db_path))
    event_loop.run_until_complete(db.connect())

    dependencies._standalone_db = db
    dependencies._bot_instance = None

    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()
    dependencies._model_router = mock_mr

    with patch.object(server, "_ensure_admin_token", return_value="bootstrap-secret"):
        with TestClient(server.app, raise_server_exceptions=False) as client:
            yield client, db

    event_loop.run_until_complete(db.close())


async def _seed_stale_processing_thread(db, *, minutes_ago: int = 30) -> int:
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO inbox_threads (subject, thread_type, status, processing_status, updated_at)
               VALUES (?, 'question', 'processing', ?, ?)""",
            ("Need follow-up on pool closure", "Generating response…", stale_time),
        )
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            thread_id = int((await cur.fetchone())[0])
        await conn.execute(
            "INSERT INTO inbox_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, "What should we tell members about the pool closure?"),
        )
        await conn.commit()
    return thread_id


class TestInboxRecoveryWebMode:
    def test_runner_requeues_stale_processing_thread(self, auth_client, event_loop):
        _, db = auth_client

        def _capture_task(coro):
            coro.close()
            return MagicMock()

        async def seed_and_run():
            thread_id = await _seed_stale_processing_thread(db)
            runner = InboxRecoveryScheduler(db, interval_seconds=60, stale_after_seconds=300, batch_limit=5)
            with patch("admin.routers.inbox.asyncio.create_task", side_effect=_capture_task) as create_task:
                result = await runner.run_once(triggered_by="manual-test")
            async with db.acquire() as conn:
                async with conn.execute(
                    "SELECT status, processing_status FROM inbox_threads WHERE id = ?",
                    (thread_id,),
                ) as cur:
                    thread_row = await cur.fetchone()
                async with conn.execute(
                    "SELECT status FROM job_runs WHERE job_name = 'inbox_stalled_thread_sweep' ORDER BY id DESC LIMIT 1"
                ) as cur:
                    job_row = await cur.fetchone()
            return thread_id, result, thread_row, job_row, create_task

        thread_id, result, thread_row, job_row, create_task = event_loop.run_until_complete(seed_and_run())

        assert result["success"] is True
        assert result["skipped"] is False
        assert result["result"]["requeued_count"] == 1
        assert result["result"]["requeued_thread_ids"] == [thread_id]
        assert thread_row[0] == "processing"
        assert thread_row[1] == "Recovering stalled response…"
        assert job_row[0] == "completed"
        assert create_task.called

    def test_manual_job_run_works_without_discord(self, auth_client, event_loop):
        client, db = auth_client

        def _capture_task(coro):
            coro.close()
            return MagicMock()

        bootstrap = client.post(
            "/api/v1/auth/bootstrap",
            json={
                "bootstrap_token": "bootstrap-secret",
                "username": "owner",
                "password": "OwnerPass123",
                "display_name": "Owner Admin",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert bootstrap.status_code == 200

        event_loop.run_until_complete(_seed_stale_processing_thread(db, minutes_ago=45))

        with patch("admin.routers.inbox.asyncio.create_task", side_effect=_capture_task):
            response = client.post(
                "/api/v1/jobs/inbox_stalled_thread_sweep/run",
                headers={"X-CSRF-Protection": "1"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["job"]["skipped"] is False
        assert payload["summary"]["requeued_count"] >= 1