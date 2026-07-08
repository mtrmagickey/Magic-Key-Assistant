"""Proposal lifecycle for uncertain operational record extraction."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from core.operational_records import ActionState, normalize_record_type
from core.services.audit_service import AuditService
from core.services.operational_record_service import OperationalRecordService
from core.services.provenance_service import ProvenanceService

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except Exception:
        return 0.0
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        return 1.0
    return round(numeric, 4)


def _json_dumps(value: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(dict(value or {}), sort_keys=True)


def _json_loads(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


class ExtractionProposalService:
    """Stores extracted proposals before a human promotes them to canonical state."""

    VALID_STATUSES = {"pending", "accepted", "rejected", "merged"}
    CRITICAL_FIELDS = {
        "action": ("title", "summary"),
        "decision": ("title", "decision", "summary"),
        "blocker": ("title", "summary"),
        "source_link": ("title", "url", "summary"),
    }

    def __init__(self, db: Any):
        self.db = db
        self.audit = AuditService(db)
        self.records = OperationalRecordService(db)
        self.provenance = ProvenanceService(db)

    def _normalize_fields(self, fields: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for key, value in dict(fields or {}).items():
            if value is None:
                continue
            payload[str(key)] = value
        return payload

    def _normalize_field_confidences(self, field_confidences: Optional[Mapping[str, Any]]) -> Dict[str, float]:
        normalized: Dict[str, float] = {}
        for key, value in dict(field_confidences or {}).items():
            if value is None:
                continue
            normalized[str(key)] = _clamp_confidence(value)
        return normalized

    def _proposal_snapshot(self, proposal: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if not proposal:
            return None
        return {
            "record_type": proposal.get("record_type"),
            "status": proposal.get("status"),
            "title": proposal.get("title"),
            "summary": proposal.get("summary"),
            "record_confidence": proposal.get("record_confidence"),
            "effective_confidence": proposal.get("effective_confidence"),
            "canonical_record_id": proposal.get("canonical_record_id"),
            "merged_into_record_id": proposal.get("merged_into_record_id"),
            "final_fields": proposal.get("final_fields"),
            "review_reason": proposal.get("review_reason"),
        }

    def _critical_fields(self, record_type: str, fields: Mapping[str, Any]) -> list[str]:
        ordered = []
        for field_name in self.CRITICAL_FIELDS.get(record_type, ("title",)):
            if field_name in fields and fields.get(field_name) not in (None, ""):
                ordered.append(field_name)
        if not ordered:
            ordered.append("title")
        return ordered

    def _compute_effective_confidence(
        self,
        record_type: str,
        record_confidence: float,
        field_confidences: Mapping[str, float],
        fields: Mapping[str, Any],
    ) -> tuple[float, list[str]]:
        critical_fields = self._critical_fields(record_type, fields)
        values = [record_confidence]
        low_fields: list[str] = []
        for field_name in critical_fields:
            field_confidence = field_confidences.get(field_name)
            if field_confidence is None:
                continue
            values.append(field_confidence)
            if field_confidence <= 0.65:
                low_fields.append(field_name)
        return _clamp_confidence(min(values) if values else record_confidence), low_fields

    def _uncertainty_summary(
        self,
        *,
        record_confidence: float,
        effective_confidence: float,
        low_confidence_fields: Iterable[str],
    ) -> str:
        fields = list(low_confidence_fields)
        if fields:
            return (
                f"Overall confidence {record_confidence:.2f}; critical field confidence drops to "
                f"{effective_confidence:.2f} for {', '.join(fields)}."
            )
        if effective_confidence <= 0.65:
            return f"Overall confidence is low at {effective_confidence:.2f}; human review should precede canonical storage."
        return f"Confidence is tentative at {effective_confidence:.2f}; treat as a proposal rather than settled fact."

    def _row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["extracted_fields"] = _json_loads(row.pop("extracted_fields_json", None))
        row["final_fields"] = _json_loads(row.pop("final_fields_json", None))
        row["field_confidences"] = _json_loads(row.pop("field_confidence_json", None))
        row["source_details"] = _json_loads(row.pop("source_details_json", None))
        row["extraction_metadata"] = _json_loads(row.pop("extraction_metadata_json", None))
        low_confidence_fields = row.get("extraction_metadata", {}).get("low_confidence_fields") or []
        row["low_confidence_fields"] = list(low_confidence_fields)
        row["uncertainty_summary"] = self._uncertainty_summary(
            record_confidence=float(row.get("record_confidence") or 0.0),
            effective_confidence=float(row.get("effective_confidence") or 0.0),
            low_confidence_fields=row["low_confidence_fields"],
        )
        row["requires_review"] = row.get("status") == "pending" and float(row.get("effective_confidence") or 0.0) <= 0.65
        return row

    async def create_proposal(
        self,
        *,
        record_type: str,
        extracted_fields: Mapping[str, Any],
        created_by_actor_id: int,
        record_confidence: float,
        field_confidences: Optional[Mapping[str, Any]] = None,
        rationale: Optional[str] = None,
        supporting_snippet: Optional[str] = None,
        source_entity_type: str,
        source_entity_id: Any,
        source_context_id: Optional[str] = None,
        source_details: Optional[Mapping[str, Any]] = None,
        extraction_metadata: Optional[Mapping[str, Any]] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_type = normalize_record_type(record_type).value
        normalized_fields = self._normalize_fields(extracted_fields)
        proposal_title = str(title or normalized_fields.get("title") or "").strip()
        if not proposal_title:
            raise ValueError("Extraction proposals require a title")
        proposal_summary = str(summary or normalized_fields.get("summary") or normalized_fields.get("decision") or "").strip() or None
        normalized_field_confidences = self._normalize_field_confidences(field_confidences)
        normalized_record_confidence = _clamp_confidence(record_confidence)
        effective_confidence, low_confidence_fields = self._compute_effective_confidence(
            normalized_type,
            normalized_record_confidence,
            normalized_field_confidences,
            normalized_fields,
        )
        metadata = self._normalize_fields(extraction_metadata)
        metadata["low_confidence_fields"] = low_confidence_fields
        now = created_at or _utc_now_iso()
        proposal_stable_id = f"oprop_{uuid.uuid4().hex}"

        async with self.db.acquire() as conn:
            async with conn.execute(
                """INSERT INTO operational_extraction_proposals
                   (proposal_id, record_type, status, title, summary, extracted_fields_json,
                    field_confidence_json, record_confidence, effective_confidence, rationale,
                    supporting_snippet, source_entity_type, source_entity_id, source_context_id,
                    source_details_json, extraction_metadata_json, created_by_actor_id,
                    created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal_stable_id,
                    normalized_type,
                    proposal_title,
                    proposal_summary,
                    _json_dumps(normalized_fields),
                    _json_dumps(normalized_field_confidences),
                    normalized_record_confidence,
                    effective_confidence,
                    rationale,
                    supporting_snippet,
                    str(source_entity_type).strip().lower(),
                    str(source_entity_id).strip(),
                    source_context_id,
                    _json_dumps(source_details),
                    _json_dumps(metadata),
                    created_by_actor_id,
                    now,
                    now,
                ),
            ) as cur:
                row_id = cur.lastrowid
            await conn.commit()

        proposal = await self.get_proposal(int(row_id))
        await self._append_event(
            proposal_row_id=int(row_id),
            event_type="created",
            actor_id=created_by_actor_id,
            previous_status=None,
            new_status="pending",
            summary=f"Created {normalized_type} extraction proposal.",
            payload={"effective_confidence": effective_confidence, "low_confidence_fields": low_confidence_fields},
            created_at=now,
        )
        await self.audit.record_mutation(
            entity_type="operational_extraction_proposal",
            entity_id=row_id,
            action="proposal_created",
            before=None,
            after=self._proposal_snapshot(proposal),
            actor_id=created_by_actor_id,
            source_context_id=source_context_id,
            metadata={"record_type": normalized_type, "effective_confidence": effective_confidence},
            created_at=now,
        )
        return proposal or {}

    async def get_proposal(self, proposal_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM operational_extraction_proposals WHERE id = ?", (proposal_id,))
        return self._row_to_dict(dict(row)) if row else None

    async def list_events(self, proposal_id: int) -> list[Dict[str, Any]]:
        async with self.db.acquire() as conn, conn.execute(
            "SELECT * FROM operational_extraction_proposal_events WHERE proposal_row_id = ? ORDER BY id ASC",
            (proposal_id,),
        ) as cur:
            rows = await cur.fetchall()
        events = []
        for row in rows:
            payload = _json_loads(row[6]) if len(row) > 6 else {}
            event = dict(row)
            event["payload"] = payload
            event.pop("payload_json", None)
            events.append(event)
        return events

    async def list_proposals(
        self,
        *,
        status: Optional[str] = "pending",
        record_type: Optional[str] = None,
        max_effective_confidence: Optional[float] = None,
        limit: int = 100,
    ) -> list[Dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            normalized_status = str(status).strip().lower()
            if normalized_status not in self.VALID_STATUSES:
                raise ValueError("Unknown extraction proposal status")
            clauses.append("status = ?")
            params.append(normalized_status)
        if record_type:
            clauses.append("record_type = ?")
            params.append(normalize_record_type(record_type).value)
        if max_effective_confidence is not None:
            clauses.append("effective_confidence <= ?")
            params.append(_clamp_confidence(max_effective_confidence))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(max(int(limit), 1), 500))
        async with self.db.acquire() as conn, conn.execute(
            f"""SELECT * FROM operational_extraction_proposals
                    {where_sql}
                    ORDER BY effective_confidence ASC, created_at ASC, id ASC
                    LIMIT ?""",
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_dict(dict(row)) for row in rows]

    async def list_low_confidence_proposals(self, *, threshold: float = 0.65, limit: int = 100) -> list[Dict[str, Any]]:
        return await self.list_proposals(status="pending", max_effective_confidence=threshold, limit=limit)

    async def accept_proposal(
        self,
        *,
        proposal_id: int,
        actor_id: int,
        final_fields: Optional[Mapping[str, Any]] = None,
        merge_record_id: Optional[int] = None,
        review_notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        proposal = await self.get_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Unknown extraction proposal: {proposal_id}")
        if proposal.get("status") != "pending":
            raise ValueError("Only pending extraction proposals can be accepted")

        merged_fields = dict(proposal.get("extracted_fields") or {})
        merged_fields.update(self._normalize_fields(final_fields))

        before_snapshot = self._proposal_snapshot(proposal)
        if merge_record_id is not None:
            canonical_record = await self._merge_into_record(
                proposal=proposal,
                actor_id=actor_id,
                record_id=int(merge_record_id),
                final_fields=merged_fields,
            )
            new_status = "merged"
            action = "proposal_merged"
        else:
            canonical_record = await self._create_record_from_fields(
                proposal=proposal,
                actor_id=actor_id,
                final_fields=merged_fields,
            )
            new_status = "accepted"
            action = "proposal_accepted"

        now = _utc_now_iso()
        await self.db.execute(
            """UPDATE operational_extraction_proposals
            SET status = ?, final_fields_json = ?, reviewed_by_actor_id = ?,
            canonical_record_id = ?, merged_into_record_id = ?, review_notes = ?,
            updated_at = ?, reviewed_at = ?
            WHERE id = ?""",
            (
            new_status,
            _json_dumps(merged_fields),
            actor_id,
            int(canonical_record["id"]),
            int(canonical_record["id"]) if new_status == "merged" else None,
            review_notes,
            now,
            now,
            proposal_id,
            ),
            )
        await self.provenance.create_edge(
            source_entity_type=proposal["source_entity_type"],
            source_entity_id=proposal["source_entity_id"],
            target_entity_type="operational_record",
            target_entity_id=canonical_record["id"],
            relationship="origin",
            actor_id=actor_id,
            explanation="Accepted from extraction proposal after human review.",
            source_context_id=proposal.get("source_context_id"),
            source_details=proposal.get("source_details"),
            metadata={
                "proposal_id": proposal.get("proposal_id"),
                "supporting_snippet": proposal.get("supporting_snippet"),
            },
        )

        updated = await self.get_proposal(proposal_id)
        await self._append_event(
            proposal_row_id=proposal_id,
            event_type=new_status,
            actor_id=actor_id,
            previous_status="pending",
            new_status=new_status,
            summary="Merged extraction proposal into an existing record." if new_status == "merged" else "Accepted extraction proposal into a canonical record.",
            payload={"final_fields": merged_fields, "canonical_record_id": int(canonical_record["id"])},
            created_at=now,
        )
        await self.audit.record_mutation(
            entity_type="operational_extraction_proposal",
            entity_id=proposal_id,
            action=action,
            before=before_snapshot,
            after=self._proposal_snapshot(updated),
            actor_id=actor_id,
            source_context_id=proposal.get("source_context_id"),
            metadata={"record_type": proposal.get("record_type"), "canonical_record_id": int(canonical_record["id"]), "review_action": new_status},
            created_at=now,
        )
        return {"proposal": updated, "record": canonical_record}

    async def reject_proposal(
        self,
        *,
        proposal_id: int,
        actor_id: int,
        reason: str,
        review_notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        proposal = await self.get_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Unknown extraction proposal: {proposal_id}")
        if proposal.get("status") != "pending":
            raise ValueError("Only pending extraction proposals can be rejected")
        rejection_reason = str(reason or "").strip()
        if not rejection_reason:
            raise ValueError("Rejected proposals require a reason")

        before_snapshot = self._proposal_snapshot(proposal)
        now = _utc_now_iso()
        await self.db.execute(
            """UPDATE operational_extraction_proposals
            SET status = 'rejected', reviewed_by_actor_id = ?, review_notes = ?,
            review_reason = ?, updated_at = ?, reviewed_at = ?
            WHERE id = ?""",
            (actor_id, review_notes, rejection_reason, now, now, proposal_id),
            )
        updated = await self.get_proposal(proposal_id)
        await self._append_event(
            proposal_row_id=proposal_id,
            event_type="rejected",
            actor_id=actor_id,
            previous_status="pending",
            new_status="rejected",
            summary="Rejected extraction proposal.",
            payload={"reason": rejection_reason, "review_notes": review_notes},
            created_at=now,
        )
        await self.audit.record_mutation(
            entity_type="operational_extraction_proposal",
            entity_id=proposal_id,
            action="proposal_rejected",
            before=before_snapshot,
            after=self._proposal_snapshot(updated),
            actor_id=actor_id,
            source_context_id=proposal.get("source_context_id"),
            metadata={"record_type": proposal.get("record_type"), "reason": rejection_reason},
            created_at=now,
        )

        # Immediate feedback: downgrade source chunk quality so the
        # nightly loop doesn't have to wait to learn from this rejection.
        try:
            source_id = proposal.get("source_entity_id") or proposal.get("source_context_id")
            if source_id:
                await self.db.execute(
                    """INSERT INTO chunk_quality_scores
                    (chunk_id, times_retrieved, helpful_retrievals, unhelpful_retrievals)
                    VALUES (?, 1, 0, 1)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                    unhelpful_retrievals = unhelpful_retrievals + 1,
                    times_retrieved = times_retrieved + 1""",
                    (str(source_id),),
                    )
        except Exception as exc:
            logger.debug("Immediate chunk quality update on rejection failed: %s", exc)

        return updated or {}

    async def _create_record_from_fields(
        self,
        *,
        proposal: Mapping[str, Any],
        actor_id: int,
        final_fields: Mapping[str, Any],
    ) -> Dict[str, Any]:
        record_type = str(proposal.get("record_type") or "")
        title = str(final_fields.get("title") or proposal.get("title") or "").strip()
        if not title:
            raise ValueError("Accepted proposals require a title")
        owner_id = final_fields.get("owner_id")
        owner_id = int(owner_id) if owner_id not in (None, "") else None
        state = final_fields.get("state")
        if record_type == "action" and owner_id is None and not state:
            state = ActionState.UNOWNED.value
        source_context_id = self._resolve_source_context_id(proposal=proposal, final_fields=final_fields)
        summary = final_fields.get("summary") or final_fields.get("decision") or proposal.get("summary")
        record = await self.records.create_record(
            record_type=record_type,
            title=title,
            summary=summary,
            state=state,
            owner_id=owner_id,
            created_by_actor_id=actor_id,
            updated_by_actor_id=actor_id,
            source_context_id=source_context_id,
            due_at=final_fields.get("due_at"),
            review_at=final_fields.get("review_at"),
            stale_after_at=final_fields.get("stale_after_at"),
            rationale=final_fields.get("rationale") or proposal.get("rationale"),
            notes=final_fields.get("notes"),
            deliverables=final_fields.get("deliverables"),
            canonical_payload={
                "proposal_id": proposal.get("proposal_id"),
                "source_entity_type": proposal.get("source_entity_type"),
                "source_entity_id": proposal.get("source_entity_id"),
                "original_extracted_fields": proposal.get("extracted_fields"),
                "accepted_fields": dict(final_fields),
                "field_confidences": proposal.get("field_confidences"),
                "record_confidence": proposal.get("record_confidence"),
                "effective_confidence": proposal.get("effective_confidence"),
                "supporting_snippet": proposal.get("supporting_snippet"),
                "extraction_metadata": proposal.get("extraction_metadata"),
            },
        )
        return record

    async def _merge_into_record(
        self,
        *,
        proposal: Mapping[str, Any],
        actor_id: int,
        record_id: int,
        final_fields: Mapping[str, Any],
    ) -> Dict[str, Any]:
        record = await self.records.get_record(record_id)
        if not record:
            raise ValueError(f"Unknown operational record: {record_id}")
        if record.get("record_type") != proposal.get("record_type"):
            raise ValueError("Can only merge a proposal into a record of the same type")

        existing_payload = _json_loads(record.get("canonical_payload_json"))
        merged_payload = dict(existing_payload)
        merged_entries = list(merged_payload.get("merged_proposals") or [])
        merged_entries.append(
            {
                "proposal_id": proposal.get("proposal_id"),
                "fields": dict(final_fields),
                "effective_confidence": proposal.get("effective_confidence"),
                "supporting_snippet": proposal.get("supporting_snippet"),
            }
        )
        merged_payload["merged_proposals"] = merged_entries
        merged_payload["latest_merged_proposal_id"] = proposal.get("proposal_id")

        updated = await self.records.update_record(
            record_id=record_id,
            actor_id=actor_id,
            title=final_fields.get("title", record.get("title")),
            summary=final_fields.get("summary", record.get("summary")),
            owner_id=final_fields.get("owner_id", record.get("owner_id")),
            due_at=final_fields.get("due_at", record.get("due_at")),
            review_at=final_fields.get("review_at", record.get("review_at")),
            stale_after_at=final_fields.get("stale_after_at", record.get("stale_after_at")),
            rationale=final_fields.get("rationale", record.get("rationale")),
            notes=final_fields.get("notes", record.get("notes")),
            canonical_payload=merged_payload,
            source_context_id=self._resolve_source_context_id(proposal=proposal, final_fields=final_fields),
            event_summary="Merged reviewed extraction proposal into canonical record.",
        )
        desired_state = final_fields.get("state")
        if desired_state and desired_state != updated.get("state"):
            updated = await self.records.transition_record(record_id=record_id, new_state=str(desired_state), actor_id=actor_id)
        return updated

    def _resolve_source_context_id(self, *, proposal: Mapping[str, Any], final_fields: Mapping[str, Any]) -> str:
        explicit = str(final_fields.get("source_context_id") or proposal.get("source_context_id") or "").strip()
        if explicit:
            return explicit
        if proposal.get("record_type") == "source_link":
            url = str(final_fields.get("url") or proposal.get("extracted_fields", {}).get("url") or "").strip()
            if url:
                return url
        return f"{proposal.get('source_entity_type')}:{proposal.get('source_entity_id')}"

    async def _append_event(
        self,
        *,
        proposal_row_id: int,
        event_type: str,
        actor_id: Optional[int],
        previous_status: Optional[str],
        new_status: Optional[str],
        summary: Optional[str],
        payload: Optional[Mapping[str, Any]],
        created_at: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO operational_extraction_proposal_events
            (proposal_row_id, event_type, actor_id, previous_status, new_status,
            summary, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
            proposal_row_id,
            str(event_type).strip(),
            actor_id,
            previous_status,
            new_status,
            summary,
            _json_dumps(payload),
            created_at or _utc_now_iso(),
            ),
            )