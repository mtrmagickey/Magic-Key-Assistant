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

from core.operational_continuity_runner import OperationalContinuityScheduler
from core.services.operational_record_service import OperationalRecordService


@pytest.fixture
def auth_client(tmp_path, monkeypatch, event_loop):
    db_path = tmp_path / "operational_continuity_web.db"
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


class TestOperationalContinuityWebMode:
    def test_manual_job_run_and_state_listing_work_without_discord(self, auth_client, event_loop):
        client, db = auth_client

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

        async def seed_record():
            service = OperationalRecordService(db)
            actor = await service.ensure_actor(
                actor_kind="web_user",
                external_ref="ops-owner@example.local",
                display_name="Ops Owner",
            )
            return await service.create_record(
                record_type="action",
                title="Follow up on pool hoist service",
                owner_id=actor["id"],
                created_by_actor_id=actor["id"],
                state="open",
                due_at=(datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat(),
            )

        record = event_loop.run_until_complete(seed_record())

        response = client.post(
            "/api/v1/jobs/operational_continuity_sweep/run",
            headers={"X-CSRF-Protection": "1"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["job"]["skipped"] is False
        assert payload["summary"]["states_by_type"]["overdue"] >= 1

        states = client.get("/api/v1/continuity/operational-states", params={"continuity_state": "overdue"})
        assert states.status_code == 200
        listed = states.json()
        assert listed["success"] is True
        assert any(item["record_id"] == record["id"] for item in listed["states"])

    def test_web_scheduler_run_once_clears_state_after_record_is_fixed(self, auth_client, event_loop):
        _, db = auth_client

        async def seed_and_run():
            record_service = OperationalRecordService(db)
            actor = await record_service.ensure_actor(
                actor_kind="web_user",
                external_ref="fixer@example.local",
                display_name="Fixer",
            )
            record = await record_service.create_record(
                record_type="action",
                title="Confirm spa chemicals order",
                owner_id=actor["id"],
                created_by_actor_id=actor["id"],
                state="open",
                due_at=(datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat(),
            )
            runner = OperationalContinuityScheduler(db, interval_seconds=60)
            first = await runner.run_once(triggered_by="manual-test")
            await record_service.update_record(
                record_id=record["id"],
                actor_id=actor["id"],
                due_at=(datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat(),
            )
            second = await runner.run_once(triggered_by="manual-test-clear")
            return record, first, second

        record, first, second = event_loop.run_until_complete(seed_and_run())

        assert first["result"]["states_by_type"]["overdue"] >= 1
        assert second["result"]["cleared_count"] >= 1

        async def inspect_state():
            async with db.acquire() as conn:
                async with conn.execute(
                    "SELECT status FROM operational_continuity_states WHERE record_id = ? AND continuity_state = 'overdue'",
                    (record["id"],),
                ) as cur:
                    row = await cur.fetchone()
                async with conn.execute(
                    "SELECT status FROM job_runs WHERE job_name = 'operational_continuity_sweep' ORDER BY id DESC LIMIT 1"
                ) as cur:
                    job_row = await cur.fetchone()
            return row[0], job_row[0]

        state_status, job_status = event_loop.run_until_complete(inspect_state())
        assert state_status == "cleared"
        assert job_status == "completed"