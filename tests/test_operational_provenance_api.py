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
    db_path = tmp_path / "operational_provenance.db"
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


async def _fetchone(db, query, params=()):
    async with db.acquire() as conn:
        async with conn.execute(query, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


class TestOperationalProvenanceApi:
    def test_action_provenance_endpoint_returns_manual_meeting_and_evidence_links(self, auth_client, event_loop):
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

        create_action = client.post(
            "/api/v1/actions",
            json={
                "title": "Replace the sauna thermostat",
                "description": "Trace the thermostat swap back to its source.",
                "priority": "high",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_action.status_code == 200
        action_id = create_action.json()["id"]

        create_meeting = client.post(
            "/api/v1/meetings",
            json={
                "title": "Weekly maintenance review",
                "summary": "Thermostat replacement approved.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert create_meeting.status_code == 200
        meeting_id = create_meeting.json()["id"]

        link_meeting = client.post(
            f"/api/v1/meetings/{meeting_id}/link-action",
            json={"target_id": action_id},
            headers={"X-CSRF-Protection": "1"},
        )
        assert link_meeting.status_code == 200

        record_row = event_loop.run_until_complete(
            _fetchone(
                db,
                """
                SELECT r.id
                FROM operational_record_legacy_links l
                JOIN operational_records r ON r.id = l.record_id
                WHERE l.legacy_table = 'tasks' AND l.legacy_id = ?
                """,
                (action_id,),
            )
        )
        assert record_row is not None

        add_message_origin = client.post(
            "/api/v1/provenance-links",
            json={
                "source": {
                    "entity_type": "message",
                    "entity_id": "thread-44",
                    "label": "Member request #44",
                    "summary": "Customer reported the sauna thermostat failure.",
                },
                "target": {
                    "entity_type": "operational_record",
                    "entity_id": str(record_row["id"]),
                },
                "relationship": "origin",
                "explanation": "Reported by a member before maintenance review.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert add_message_origin.status_code == 200
        assert add_message_origin.json()["success"] is True

        add_evidence = client.post(
            "/api/v1/provenance-links",
            json={
                "source": {
                    "entity_type": "source_link",
                    "entity_id": "thermostat-manual",
                    "label": "Thermostat manual",
                    "summary": "Manufacturer wiring manual.",
                    "url": "https://example.local/thermostat-manual.pdf",
                },
                "target": {
                    "entity_type": "operational_record",
                    "entity_id": str(record_row["id"]),
                },
                "relationship": "evidence",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert add_evidence.status_code == 200
        assert add_evidence.json()["success"] is True

        provenance = client.get(f"/api/v1/actions/{action_id}/provenance")
        assert provenance.status_code == 200
        payload = provenance.json()

        assert payload["success"] is True
        assert payload["record"]["label"] == "Replace the sauna thermostat"
        assert {origin["source"]["entity_type"] for origin in payload["origins"]} == {
            "manual_creation",
            "meeting",
            "message",
        }
        assert [item["label"] for item in payload["linked_evidence_objects"]] == ["Thermostat manual"]
        assert "exists because of" in payload["summary"]

        reverse_edges = client.get(
            "/api/v1/provenance-links",
            params={
                "entity_type": "meeting",
                "entity_id": str(meeting_id),
                "direction": "outgoing",
            },
        )
        assert reverse_edges.status_code == 200
        reverse_payload = reverse_edges.json()
        assert reverse_payload["success"] is True
        assert any(edge["target"]["label"] == "Replace the sauna thermostat" for edge in reverse_payload["edges"])