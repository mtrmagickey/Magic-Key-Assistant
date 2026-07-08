import importlib
import os
import sys
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


@pytest.fixture
def auth_client(tmp_path, monkeypatch, event_loop):
    db_path = tmp_path / "web_identity.db"
    monkeypatch.setenv("ADMIN_AUTH_DISABLED", "0")
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "1")
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


async def _fetchone(db, query, params=()):
    async with db.acquire() as conn:
        async with conn.execute(query, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _fetchall(db, query, params=()):
    async with db.acquire() as conn:
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def _insert_and_commit(db, query, params=()):
    async with db.acquire() as conn:
        async with conn.execute(query, params) as cur:
            lastrowid = cur.lastrowid
        await conn.commit()
    return lastrowid


class TestWebIdentityAndAttribution:
    def test_bootstrap_login_and_role_enforcement(self, auth_client, event_loop):
        client, db = auth_client

        status = client.get("/api/v1/auth/status")
        assert status.status_code == 200
        status_data = status.json()
        assert status_data["authenticated"] is False
        assert status_data["bootstrap_required"] is True

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
        assert bootstrap.json()["success"] is True
        assert "mka_session" in bootstrap.cookies

        create_user = client.post(
            "/api/v1/admin/users",
            json={
                "username": "manager1",
                "password": "ManagerPass123",
                "display_name": "Manager One",
                "role": "manager",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_user.status_code == 200
        user_data = create_user.json()
        assert user_data["success"] is True
        assert user_data["user"]["role"] == "manager"

        logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Protection": "1"})
        assert logout.status_code == 200

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "manager1", "password": "ManagerPass123"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "manager"

        forbidden = client.post(
            "/api/v1/admin/users",
            json={
                "username": "member1",
                "password": "MemberPass123",
                "display_name": "Member One",
                "role": "member",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert forbidden.status_code == 403

        accounts = event_loop.run_until_complete(
            _fetchall(db, "SELECT username, role FROM web_accounts ORDER BY username_normalized ASC")
        )
        assert [row["username"] for row in accounts] == ["manager1", "owner"]

    def test_two_web_users_leave_distinct_mutation_actors(self, auth_client, event_loop):
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

        create_user = client.post(
            "/api/v1/admin/users",
            json={
                "username": "member1",
                "password": "MemberPass123",
                "display_name": "Member One",
                "role": "member",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_user.status_code == 200

        client.post("/api/v1/auth/logout", headers={"X-CSRF-Protection": "1"})

        login_member = client.post(
            "/api/v1/auth/login",
            json={"username": "member1", "password": "MemberPass123"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert login_member.status_code == 200

        create_action = client.post(
            "/api/v1/actions",
            json={
                "title": "Follow up on locker repair",
                "description": "Call the vendor and confirm ETA.",
                "priority": "high",
                "assigned_to_username": "member1",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_action.status_code == 200
        action_id = create_action.json()["id"]

        client.post("/api/v1/auth/logout", headers={"X-CSRF-Protection": "1"})

        login_admin = client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": "OwnerPass123"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert login_admin.status_code == 200

        update_action = client.patch(
            f"/api/v1/actions/{action_id}",
            json={"status": "done", "title": "Follow up on locker repair and close loop"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert update_action.status_code == 200
        assert update_action.json()["success"] is True

        task_row = event_loop.run_until_complete(
            _fetchone(
                db,
                "SELECT id, created_by_user_id, created_by_username, status FROM tasks WHERE id = ?",
                (action_id,),
            )
        )
        assert task_row["status"] == "done"
        assert task_row["created_by_username"] == "Member One"

        record_row = event_loop.run_until_complete(
            _fetchone(
                db,
                """
                SELECT r.id, r.created_by_actor_id, r.updated_by_actor_id, r.state
                FROM operational_record_legacy_links l
                JOIN operational_records r ON r.id = l.record_id
                WHERE l.legacy_table = 'tasks' AND l.legacy_id = ?
                """,
                (action_id,),
            )
        )
        assert record_row["state"] == "done"
        assert record_row["created_by_actor_id"] != record_row["updated_by_actor_id"]

        actor_names = {
            row["id"]: row["display_name"]
            for row in event_loop.run_until_complete(
                _fetchall(db, "SELECT id, display_name FROM operational_actors")
            )
        }
        assert actor_names[record_row["created_by_actor_id"]] == "Member One"
        assert actor_names[record_row["updated_by_actor_id"]] == "Owner Admin"

        event_rows = event_loop.run_until_complete(
            _fetchall(
                db,
                """
                SELECT event_type, actor_id, summary
                FROM operational_record_events
                WHERE record_id = ?
                ORDER BY id ASC
                """,
                (record_row["id"],),
            )
        )
        assert event_rows[0]["event_type"] == "created"
        assert any(row["actor_id"] == record_row["created_by_actor_id"] for row in event_rows)
        assert any(row["actor_id"] == record_row["updated_by_actor_id"] for row in event_rows)

    def test_web_actor_is_attributed_in_continuity_feedback_and_gaps(self, auth_client, event_loop):
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

        create_user = client.post(
            "/api/v1/admin/users",
            json={
                "username": "member1",
                "password": "MemberPass123",
                "display_name": "Member One",
                "role": "member",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_user.status_code == 200

        create_obligation = client.post(
            "/api/v1/obligations",
            json={"title": "Weekly chlorine check", "frequency": "weekly"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_obligation.status_code == 200
        obligation_id = create_obligation.json()["id"]

        create_feedback = client.post(
            "/api/v1/feedback",
            json={"summary": "Kiosk keyboard focus jumps unexpectedly", "category": "ux"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_feedback.status_code == 200
        feedback_id = create_feedback.json()["id"]

        inbox_feedback = client.post(
            "/api/v1/inbox/feedback",
            json={
                "question": "Pool hours?",
                "answer": "The pool is open 6am-9pm.",
                "feedback": "helpful",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert inbox_feedback.status_code == 200

        chat_feedback = client.post(
            "/api/v1/chat/feedback",
            json={
                "question": "When is the sauna inspection?",
                "answer": "It is scheduled for next Tuesday.",
                "feedback": "helpful",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert chat_feedback.status_code == 200

        gap_id = event_loop.run_until_complete(
            _insert_and_commit(
                db,
                "INSERT INTO knowledge_gaps (topic, question, context) VALUES (?, ?, ?)",
                ("Sauna", "What is the emergency shutdown process?", "Created by test"),
            )
        )

        gap_update = client.patch(
            f"/api/v1/gaps/{gap_id}",
            json={"curation_status": "keep", "notes": "Needs a formal memo."},
            headers={"X-CSRF-Protection": "1"},
        )
        assert gap_update.status_code == 200
        assert gap_update.json()["success"] is True

        obligation_row = event_loop.run_until_complete(
            _fetchone(db, "SELECT notes FROM obligations WHERE id = ?", (obligation_id,))
        )
        feedback_row = event_loop.run_until_complete(
            _fetchone(db, "SELECT submitted_by FROM feedback WHERE id = ?", (feedback_id,))
        )
        response_feedback_rows = event_loop.run_until_complete(
            _fetchall(
                db,
                "SELECT username FROM response_feedback ORDER BY id ASC",
            )
        )
        gap_row = event_loop.run_until_complete(
            _fetchone(
                db,
                "SELECT curated_by_username, notes FROM knowledge_gaps WHERE id = ?",
                (gap_id,),
            )
        )

        assert "Owner Admin" in obligation_row["notes"]
        assert feedback_row["submitted_by"] == "Owner Admin"
        assert [row["username"] for row in response_feedback_rows[-2:]] == ["Owner Admin", "Owner Admin"]
        assert gap_row["curated_by_username"] == "Owner Admin"
        assert "Owner Admin" in gap_row["notes"]