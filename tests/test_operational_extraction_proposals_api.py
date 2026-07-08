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

from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_record_service import OperationalRecordService


@pytest.fixture
def auth_client(tmp_path, monkeypatch, event_loop):
    db_path = tmp_path / "operational_extraction_proposals_api.db"
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


class TestOperationalExtractionProposalsApi:
    def test_low_confidence_review_endpoint_and_accept_flow(self, auth_client, event_loop):
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

        async def seed_proposals():
            record_service = OperationalRecordService(db)
            proposal_service = ExtractionProposalService(db)
            actor = await record_service.ensure_actor(
                actor_kind="system_job",
                external_ref="seed-review-api",
                display_name="Seed Review API",
            )
            low = await proposal_service.create_proposal(
                record_type="action",
                title="Call the plant room contractor",
                extracted_fields={
                    "title": "Call the plant room contractor",
                    "summary": "Contact the contractor about the plant room leak.",
                },
                created_by_actor_id=actor["id"],
                record_confidence=0.81,
                field_confidences={"title": 0.84, "summary": 0.41},
                supporting_snippet="Call the plant room contractor about the leak.",
                source_entity_type="conversation_session",
                source_entity_id="sess-api-low",
                source_context_id="conversation_session:sess-api-low",
                source_details={"label": "Leak chat"},
            )
            await proposal_service.create_proposal(
                record_type="action",
                title="Restock reception towels",
                extracted_fields={
                    "title": "Restock reception towels",
                    "summary": "Restock towels at reception before the weekend.",
                },
                created_by_actor_id=actor["id"],
                record_confidence=0.88,
                field_confidences={"title": 0.92, "summary": 0.82},
                supporting_snippet="Restock the reception towels before the weekend.",
                source_entity_type="conversation_session",
                source_entity_id="sess-api-high",
                source_context_id="conversation_session:sess-api-high",
                source_details={"label": "Reception prep"},
            )
            return low

        low_proposal = event_loop.run_until_complete(seed_proposals())

        low_confidence = client.get(
            "/api/v1/extraction-proposals/review",
            params={"max_effective_confidence": 0.6},
        )
        assert low_confidence.status_code == 200
        payload = low_confidence.json()

        assert payload["success"] is True
        assert payload["count"] == 1
        assert payload["proposals"][0]["id"] == low_proposal["id"]
        assert payload["proposals"][0]["effective_confidence"] == pytest.approx(0.41)

        review = client.post(
            f"/api/v1/extraction-proposals/{low_proposal['id']}/review",
            json={
                "action": "edit_accept",
                "final_fields": {
                    "title": "Call the plant room leak contractor",
                    "summary": "Contact the plant room contractor about the active leak today.",
                },
                "review_notes": "Clarified the title before acceptance.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert review.status_code == 200
        review_payload = review.json()

        assert review_payload["success"] is True
        assert review_payload["review_action"] == "accepted"
        assert review_payload["proposal"]["status"] == "accepted"
        assert review_payload["record"]["record_type"] == "action"
        assert review_payload["record"]["title"] == "Call the plant room leak contractor"
