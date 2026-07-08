from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.services.extraction_proposal_service import ExtractionProposalService
from core.services.operational_continuity_service import OperationalContinuityService
from core.services.operational_record_service import OperationalRecordService


class TestReviewQueueApi:
    def test_mixed_queue_actions_mutate_canonical_objects_and_write_audit(
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
        past_due = (now - timedelta(days=4)).date().isoformat()
        past_review = (now - timedelta(days=3)).isoformat()

        async def seed_queue_inputs():
            records = OperationalRecordService(db)
            proposals = ExtractionProposalService(db)
            continuity = OperationalContinuityService(db)
            reviewer = await records.ensure_actor(
                actor_kind="web_user",
                external_ref="owner@example.local",
                display_name="Owner Admin",
            )
            assignee = await records.ensure_actor(
                actor_kind="web_user",
                external_ref="assignee@example.local",
                display_name="Assignee",
            )
            action = await records.create_record(
                record_type="action",
                title="Assign owner for gym floor repair",
                created_by_actor_id=reviewer["id"],
                workspace_scope="facilities",
                project_scope="gym-floor",
                state="unowned",
            )
            decision = await records.create_record(
                record_type="decision",
                title="Approve revised party room pricing",
                owner_id=reviewer["id"],
                created_by_actor_id=reviewer["id"],
                workspace_scope="events",
                project_scope="pricing",
                review_at=past_review,
                state="proposed",
            )
            await records.create_record(
                record_type="action",
                title="Chase overdue lifeguard rota sign-off",
                owner_id=reviewer["id"],
                created_by_actor_id=reviewer["id"],
                workspace_scope="aquatics",
                project_scope="rota",
                due_at=past_due,
                state="open",
            )
            proposal = await proposals.create_proposal(
                record_type="action",
                title="Book the HVAC contractor revisit",
                summary="Pending review proposal from imported maintenance notes.",
                extracted_fields={
                    "title": "Book the HVAC contractor revisit",
                    "summary": "Book the HVAC contractor revisit this week.",
                    "workspace_scope": "facilities",
                },
                created_by_actor_id=reviewer["id"],
                record_confidence=0.74,
                field_confidences={"title": 0.95, "summary": 0.52},
                rationale="Maintenance note implied a follow-up visit but summary confidence stayed low.",
                source_entity_type="knowledge_note",
                source_entity_id="maintenance-queue.md",
                source_context_id="knowledge_note:maintenance-queue.md",
                source_details={"label": "Maintenance queue"},
            )
            await continuity.run_sweep(actor_id=reviewer["id"])
            return reviewer, assignee, action, decision, proposal

        reviewer, assignee, action, decision, proposal = event_loop.run_until_complete(seed_queue_inputs())

        queue = client.get("/api/v1/review-queue", params={"limit": 50})
        assert queue.status_code == 200
        payload = queue.json()
        assert payload["success"] is True

        items_by_type = {}
        for item in payload["items"]:
            items_by_type.setdefault(item["item_type"], []).append(item)

        assert "extraction_proposal_low_confidence" in items_by_type
        assert "action_unowned" in items_by_type
        assert "action_overdue" in items_by_type
        assert "decision_unresolved" in items_by_type

        proposal_item = next(item for item in payload["items"] if item["proposal_id"] == proposal["id"])
        assign_item = next(item for item in payload["items"] if item["operational_record_id"] == action["id"] and item["item_type"] == "action_unowned")
        decision_item = next(item for item in payload["items"] if item["operational_record_id"] == decision["id"])

        accept = client.post(
            f"/api/v1/review-queue/{proposal_item['id']}/actions",
            json={
                "action": "accept",
                "final_fields": {
                    "title": "Book the HVAC contractor revisit this week",
                    "summary": "Book the HVAC contractor revisit this week and confirm the engineer slot.",
                },
                "review_notes": "Accepted from the unified queue after clarifying the summary.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert accept.status_code == 200
        accept_payload = accept.json()
        assert accept_payload["success"] is True
        assert accept_payload["review_action"] == "accepted"
        assert accept_payload["record"]["title"] == "Book the HVAC contractor revisit this week"

        assign = client.post(
            f"/api/v1/review-queue/{assign_item['id']}/actions",
            json={
                "action": "assign_owner",
                "owner_id": assignee["id"],
                "rationale": "Assigned directly from the review queue.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert assign.status_code == 200
        assert assign.json()["success"] is True

        defer = client.post(
            f"/api/v1/review-queue/{decision_item['id']}/actions",
            json={
                "action": "defer",
                "defer_until": (now + timedelta(days=5)).isoformat(),
                "rationale": "Need final finance numbers before deciding.",
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert defer.status_code == 200
        assert defer.json()["success"] is True

        escalate = client.post(
            f"/api/v1/review-queue/{decision_item['id']}/actions",
            json={
                "action": "escalate",
                "severity": "critical",
                "rationale": "Escalating for weekly leadership review.",
                "escalation_destination": {"route": "weekly_review", "target": "leadership"},
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert escalate.status_code == 200
        assert escalate.json()["success"] is True
        assert escalate.json()["item"]["severity"] == "critical"

        filtered = client.get("/api/v1/review-queue", params={"item_type": "action_overdue", "limit": 20})
        assert filtered.status_code == 200
        filtered_payload = filtered.json()
        assert filtered_payload["success"] is True
        assert all(item["item_type"] == "action_overdue" for item in filtered_payload["items"])

        weekly_session = client.post(
            "/api/v1/review-queue/sessions",
            json={"cadence": "weekly", "scope": "all"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert weekly_session.status_code == 200
        weekly_payload = weekly_session.json()
        assert weekly_payload["success"] is True
        session = weekly_payload["session"]
        assert any(item["review_item_id"] == decision_item["id"] for item in session["items"])

        complete = client.post(
            f"/api/v1/review-queue/sessions/{session['session_id']}/complete",
            json={"completion_notes": "Weekly queue reviewed."},
            headers={"X-CSRF-Protection": "1"},
        )
        assert complete.status_code == 200
        assert complete.json()["success"] is True
        assert complete.json()["session"]["completed_at"] is not None

        async def assert_underlying_mutations_and_audit():
            records = OperationalRecordService(db)
            assigned_record = await records.get_record(action["id"])
            assert assigned_record["owner_id"] == assignee["id"]
            assert assigned_record["state"] == "open"

            async with db.acquire() as conn:
                async with conn.execute(
                    "SELECT action_type FROM operational_review_queue_actions ORDER BY id ASC"
                ) as cur:
                    queue_actions = [row[0] for row in await cur.fetchall()]
                async with conn.execute(
                    "SELECT action, actor_id FROM operational_audit_events WHERE entity_type = 'review_queue_item' ORDER BY id ASC"
                ) as cur:
                    audit_rows = await cur.fetchall()
            assert "proposal_accepted_from_queue" in queue_actions
            assert "owner_assigned_from_queue" in queue_actions
            assert "review_deferred" in queue_actions
            assert "review_escalated" in queue_actions
            assert all(row[1] is not None for row in audit_rows)

        event_loop.run_until_complete(assert_underlying_mutations_and_audit())

    def test_bulk_escalation_and_daily_review_completion_work_from_queue(
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

        async def seed_bulk_items():
            records = OperationalRecordService(db)
            continuity = OperationalContinuityService(db)
            reviewer = await records.ensure_actor(
                actor_kind="web_user",
                external_ref="bulk@example.local",
                display_name="Bulk Reviewer",
            )
            action = await records.create_record(
                record_type="action",
                title="Resolve overdue plant filter order",
                owner_id=reviewer["id"],
                created_by_actor_id=reviewer["id"],
                due_at=(now - timedelta(days=2)).date().isoformat(),
                state="open",
            )
            blocker = await records.create_record(
                record_type="blocker",
                title="Pool closure notice still unsigned",
                owner_id=reviewer["id"],
                created_by_actor_id=reviewer["id"],
                stale_after_at=(now - timedelta(days=4)).isoformat(),
                state="open",
            )
            await continuity.run_sweep(actor_id=reviewer["id"])
            return action, blocker

        action, blocker = event_loop.run_until_complete(seed_bulk_items())

        queue = client.get("/api/v1/review-queue", params={"limit": 20})
        assert queue.status_code == 200
        items = queue.json()["items"]
        target_ids = [
            item["id"]
            for item in items
            if item.get("operational_record_id") in {action["id"], blocker["id"]}
        ]
        assert len(target_ids) == 2

        bulk = client.post(
            "/api/v1/review-queue/bulk-actions",
            json={
                "item_ids": target_ids,
                "action": "escalate",
                "severity": "critical",
                "rationale": "Escalate both items for urgent follow-up.",
                "escalation_destination": {"route": "daily_review", "target": "ops-desk"},
            },
            headers={"X-CSRF-Protection": "1"},
        )
        assert bulk.status_code == 200
        bulk_payload = bulk.json()
        assert bulk_payload["success"] is True
        assert bulk_payload["success_count"] == 2
        assert bulk_payload["error_count"] == 0

        daily_session = client.post(
            "/api/v1/review-queue/sessions",
            json={"cadence": "daily", "scope": "all"},
            headers={"X-CSRF-Protection": "1"},
        )
        assert daily_session.status_code == 200
        daily_payload = daily_session.json()
        assert daily_payload["success"] is True
        assert {item["review_item_id"] for item in daily_payload["session"]["items"]} >= set(target_ids)

        completed = client.post(
            f"/api/v1/review-queue/sessions/{daily_payload['session']['session_id']}/complete",
            json={"completion_notes": "Daily triage complete."},
            headers={"X-CSRF-Protection": "1"},
        )
        assert completed.status_code == 200
        assert completed.json()["success"] is True