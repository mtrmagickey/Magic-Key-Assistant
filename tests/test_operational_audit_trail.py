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
    db_path = tmp_path / "operational_audit.db"
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


class TestOperationalAuditTrail:
    def test_operational_record_audit_survives_restart_and_keeps_actor_attribution(self, auth_client, event_loop):
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
                "title": "Repair pool hoist",
                "description": "Confirm engineer visit and parts availability.",
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
            json={"status": "done", "due_date": "2026-05-01", "title": "Repair pool hoist and close vendor loop"},
            headers={"X-CSRF-Protection": "1", "X-Request-ID": "req-audit-001"},
        )
        assert update_action.status_code == 200
        assert update_action.json()["success"] is True

        record_row = event_loop.run_until_complete(
            _fetchone(
                db,
                """
                SELECT r.id, r.created_by_actor_id, r.updated_by_actor_id
                FROM operational_record_legacy_links l
                JOIN operational_records r ON r.id = l.record_id
                WHERE l.legacy_table = 'tasks' AND l.legacy_id = ?
                """,
                (action_id,),
            )
        )
        assert record_row is not None

        audit_rows_before_restart = event_loop.run_until_complete(
            _fetchall(
                db,
                """
                SELECT action, actor_id, surface, correlation_id, before_json, after_json
                FROM operational_audit_events
                WHERE entity_type = 'operational_record' AND entity_id = ?
                ORDER BY id ASC
                """,
                (str(record_row["id"]),),
            )
        )
        assert [row["action"] for row in audit_rows_before_restart][:2] == ["record_created", "traceability_link_added"]
        assert any(row["action"] == "record_updated" for row in audit_rows_before_restart)
        assert any(row["action"] == "state_transitioned" for row in audit_rows_before_restart)
        assert {row["actor_id"] for row in audit_rows_before_restart if row["actor_id"] is not None} >= {
            record_row["created_by_actor_id"],
            record_row["updated_by_actor_id"],
        }
        assert any(row["surface"] == "web" for row in audit_rows_before_restart)
        assert any(row["correlation_id"] == "req-audit-001" for row in audit_rows_before_restart)
        updated_row = next(row for row in audit_rows_before_restart if row["action"] == "record_updated")
        assert '"due_at": null' in (updated_row["before_json"] or "")
        assert '"due_at": "2026-05-01"' in (updated_row["after_json"] or "")

        db_path = Path(db.database_path)
        from database import Database

        event_loop.run_until_complete(db.close())
        reopened = Database(str(db_path))
        event_loop.run_until_complete(reopened.connect())
        try:
            audit_rows_after_restart = event_loop.run_until_complete(
                _fetchall(
                    reopened,
                    """
                    SELECT action, actor_id, surface, correlation_id
                    FROM operational_audit_events
                    WHERE entity_type = 'operational_record' AND entity_id = ?
                    ORDER BY id ASC
                    """,
                    (str(record_row["id"]),),
                )
            )
        finally:
            event_loop.run_until_complete(reopened.close())

        assert audit_rows_after_restart == [
            {
                "action": row["action"],
                "actor_id": row["actor_id"],
                "surface": row["surface"],
                "correlation_id": row["correlation_id"],
            }
            for row in audit_rows_before_restart
        ]

    def test_work_packet_review_audit_is_exposed_via_api(self, auth_client, event_loop):
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

        create_thread = client.post(
            "/api/v1/inbox/threads/completed",
            json={
                "message": "What time does the hydrotherapy pool open?",
                "response": "It opens at 6am on weekdays.",
                "sources": ["docs/pool-hours.md"],
            },
            headers={"X-CSRF-Protection": "1", "X-Request-ID": "req-audit-002"},
        )
        assert create_thread.status_code == 200
        thread_id = create_thread.json()["thread_id"]

        review = client.patch(
            f"/api/v1/inbox/threads/{thread_id}",
            json={"status": "read"},
            headers={"X-CSRF-Protection": "1", "X-Request-ID": "req-audit-003"},
        )
        assert review.status_code == 200

        packet_row = event_loop.run_until_complete(
            _fetchone(db, "SELECT id FROM work_packets WHERE created_from_type = 'inbox_thread' AND created_from_id = ?", (str(thread_id),))
        )
        assert packet_row is not None

        api_response = client.get(f"/api/v1/audit-events/work_packet/{packet_row['id']}")
        assert api_response.status_code == 200
        payload = api_response.json()
        assert payload["success"] is True
        actions = [row["action"] for row in payload["events"]]
        assert "review_requested" in actions
        assert "review_approved" in actions
        assert "state_transitioned" in actions

        reviewed = next(row for row in payload["events"] if row["action"] == "review_approved")
        assert reviewed["actor_id"] is not None
        assert reviewed["surface"] == "web"
        assert reviewed["correlation_id"] == "req-audit-003"

    def test_knowledge_gap_corrections_are_visible_in_audit_history(self, auth_client, event_loop):
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

        async def _insert_gap():
            async with db.acquire() as conn:
                async with conn.execute(
                    "INSERT INTO knowledge_gaps (topic, question, context) VALUES (?, ?, ?)",
                    ("Sauna", "Where is the emergency shutoff?", "Created by audit test"),
                ) as cur:
                    gap_id = cur.lastrowid
                await conn.commit()
            return int(gap_id)

        gap_id = event_loop.run_until_complete(_insert_gap())

        update = client.patch(
            f"/api/v1/gaps/{gap_id}",
            json={"curation_status": "keep", "notes": "Verified against the plant room checklist."},
            headers={"X-CSRF-Protection": "1", "X-Request-ID": "req-audit-004"},
        )
        assert update.status_code == 200

        history = client.get(f"/api/v1/audit-events/knowledge_gap/{gap_id}")
        assert history.status_code == 200
        payload = history.json()
        assert payload["success"] is True
        assert payload["count"] >= 1

        correction = payload["events"][0]
        gap_row = event_loop.run_until_complete(
            _fetchone(db, "SELECT curation_status, curated_by_username FROM knowledge_gaps WHERE id = ?", (gap_id,))
        )
        assert correction["action"] == "human_correction"
        assert correction["actor_id"] is not None
        assert correction["surface"] == "web"
        assert correction["correlation_id"] == "req-audit-004"
        assert correction["metadata"]["route"] == "api_update_gap"
        assert correction["changed_fields"]["changed"]
        assert gap_row["curation_status"] == "keep"
        assert gap_row["curated_by_username"] == "Owner Admin"