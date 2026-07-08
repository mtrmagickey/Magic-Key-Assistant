from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_record_service import OperationalRecordService


class TestSoloWebOperationalContinuityRegressionHarness:
    def test_product_claim_imported_note_to_extracted_action_proposal_to_overdue_review_visibility_without_discord(
        self,
        solo_web_client,
        bootstrap_admin_session,
        event_loop,
    ):
        harness = solo_web_client
        client = harness["client"]
        db = harness["db"]
        docs_path: Path = harness["docs_path"]

        bootstrap_admin_session(client)

        with patch("admin.routers.knowledge._schedule_background_ingest", return_value=None):
            remember = client.post(
                "/api/v1/knowledge/remember",
                json={
                    "title": "Friday handoff note",
                    "category": "operations",
                    "content": (
                        "The boiler safety check is still unresolved.\n\n"
                        "Action: escalate the overdue boiler safety check before Friday and assign an owner."
                    ),
                },
                headers={"X-CSRF-Protection": "1"},
            )

        assert remember.status_code == 200
        remember_payload = remember.json()
        assert remember_payload["success"] is True
        imported_note = remember_payload["file"]
        assert (docs_path / imported_note).exists()

        past_due = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()

        async def seed_extracted_action_proposal():
            records = OperationalRecordService(db)
            proposals = ExtractionProposalService(db)
            extractor = await records.ensure_actor(
                actor_kind="system_job",
                external_ref="imported-note-extractor",
                display_name="Imported Note Extractor",
            )
            return await proposals.create_proposal(
                record_type="action",
                title="Escalate the overdue boiler safety check",
                summary="Escalate the overdue boiler safety check and assign an owner.",
                extracted_fields={
                    "title": "Escalate the overdue boiler safety check",
                    "summary": "Escalate the overdue boiler safety check and assign an owner.",
                    "due_at": past_due,
                },
                created_by_actor_id=extractor["id"],
                record_confidence=0.78,
                field_confidences={"title": 0.91, "summary": 0.58, "due_at": 0.67},
                rationale="The imported handoff note contains an explicit action item and due date pressure.",
                supporting_snippet="Action: escalate the overdue boiler safety check before Friday and assign an owner.",
                source_entity_type="knowledge_note",
                source_entity_id=imported_note,
                source_context_id=f"knowledge_note:{imported_note}",
                source_details={
                    "label": "Friday handoff note",
                    "summary": "Imported through the web remember route.",
                },
                extraction_metadata={"pipeline": "regression_harness"},
            )

        proposal = event_loop.run_until_complete(seed_extracted_action_proposal())

        pending_review = client.get(
            "/api/v1/extraction-proposals/review",
            params={"max_effective_confidence": 0.7},
        )
        assert pending_review.status_code == 200
        pending_payload = pending_review.json()
        assert pending_payload["success"] is True
        assert any(item["id"] == proposal["id"] for item in pending_payload["proposals"])

        review = client.post(
            f"/api/v1/extraction-proposals/{proposal['id']}/review",
            json={
                "action": "edit_accept",
                "final_fields": {
                    "title": "Escalate the overdue boiler safety check and assign an owner",
                    "summary": "Escalate the overdue boiler safety check immediately and assign a named owner.",
                    "due_at": past_due,
                },
                "review_notes": "Clarified the action wording before accepting it into canonical continuity state.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert review.status_code == 200
        review_payload = review.json()
        assert review_payload["success"] is True
        assert review_payload["review_action"] == "accepted"
        record_id = review_payload["record"]["id"]

        review_queue_after_accept = client.get(
            "/api/v1/extraction-proposals/review",
            params={"max_effective_confidence": 0.7},
        )
        assert review_queue_after_accept.status_code == 200
        assert all(item["id"] != proposal["id"] for item in review_queue_after_accept.json()["proposals"])

        sweep = client.post(
            "/api/v1/jobs/operational_continuity_sweep/run",
            headers={"X-CSRF-Protection": "1"},
        )
        assert sweep.status_code == 200
        sweep_payload = sweep.json()
        assert sweep_payload["success"] is True
        assert sweep_payload["summary"]["states_by_type"]["overdue"] >= 1

        overdue_queue = client.get(
            "/api/v1/continuity/operational-states",
            params={"continuity_state": "overdue", "record_type": "action"},
        )
        assert overdue_queue.status_code == 200
        overdue_payload = overdue_queue.json()
        assert overdue_payload["success"] is True
        assert any(item["record_id"] == record_id for item in overdue_payload["states"])

        provenance = client.get(
            "/api/v1/provenance-links",
            params={
                "entity_type": "knowledge_note",
                "entity_id": imported_note,
                "direction": "outgoing",
                "relationship": "origin",
            },
        )
        assert provenance.status_code == 200
        provenance_payload = provenance.json()
        assert provenance_payload["success"] is True
        assert any(edge["target"]["entity_id"] == str(record_id) for edge in provenance_payload["edges"])

    def test_product_claim_web_review_queue_generation_surfaces_active_overdue_unowned_and_unresolved_items(
        self,
        solo_web_client,
        bootstrap_admin_session,
        event_loop,
    ):
        harness = solo_web_client
        client = harness["client"]
        db = harness["db"]

        bootstrap_admin_session(client)

        now = datetime.now(timezone.utc)
        past_due = (now - timedelta(days=3)).date().isoformat()
        past_review = (now - timedelta(days=4)).isoformat()

        async def seed_reviewable_records():
            records = OperationalRecordService(db)
            actor = await records.ensure_actor(
                actor_kind="web_user",
                external_ref="ops-owner@example.local",
                display_name="Ops Owner",
            )
            overdue_unowned = await records.create_record(
                record_type="action",
                title="Assign an owner for the summer rota draft",
                created_by_actor_id=actor["id"],
                state="unowned",
                due_at=past_due,
            )
            unresolved = await records.create_record(
                record_type="decision",
                title="Confirm the Easter pool opening schedule",
                created_by_actor_id=actor["id"],
                owner_id=actor["id"],
                state="proposed",
                review_at=past_review,
            )
            resolved = await records.create_record(
                record_type="action",
                title="Already completed handoff task",
                created_by_actor_id=actor["id"],
                owner_id=actor["id"],
                state="done",
                due_at=past_due,
            )
            return overdue_unowned, unresolved, resolved

        overdue_unowned, unresolved, resolved = event_loop.run_until_complete(seed_reviewable_records())

        sweep = client.post(
            "/api/v1/jobs/operational_continuity_sweep/run",
            headers={"X-CSRF-Protection": "1"},
        )
        assert sweep.status_code == 200
        assert sweep.json()["success"] is True

        queue = client.get("/api/v1/continuity/operational-states", params={"limit": 20})
        assert queue.status_code == 200
        queue_payload = queue.json()
        assert queue_payload["success"] is True

        states_by_record = {}
        for item in queue_payload["states"]:
            states_by_record.setdefault(item["record_id"], set()).add(item["continuity_state"])

        assert {"overdue", "unowned"}.issubset(states_by_record[overdue_unowned["id"]])
        assert states_by_record[unresolved["id"]] == {"unresolved"}
        assert resolved["id"] not in states_by_record

        overdue_only = client.get(
            "/api/v1/continuity/operational-states",
            params={"continuity_state": "overdue", "record_type": "action"},
        )
        assert overdue_only.status_code == 200
        assert any(item["record_id"] == overdue_unowned["id"] for item in overdue_only.json()["states"])

        unresolved_only = client.get(
            "/api/v1/continuity/operational-states",
            params={"continuity_state": "unresolved", "record_type": "decision"},
        )
        assert unresolved_only.status_code == 200
        assert any(item["record_id"] == unresolved["id"] for item in unresolved_only.json()["states"])