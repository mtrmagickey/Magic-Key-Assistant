"""Bidirectional provenance edges for operational continuity records."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from core.services.audit_service import AuditService

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _normalize_entity_type(entity_type: str) -> str:
    normalized = str(entity_type or "").strip().lower()
    if not normalized:
        raise ValueError("entity_type is required")
    return normalized


def _normalize_entity_id(entity_id: Any) -> str:
    normalized = str(entity_id or "").strip()
    if not normalized:
        raise ValueError("entity_id is required")
    return normalized


def _normalize_relationship(relationship: str) -> str:
    normalized = str(relationship or "").strip().lower()
    if not normalized:
        raise ValueError("relationship is required")
    return normalized


def _compact_details(details: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not details:
        return {}
    return {key: value for key, value in dict(details).items() if value is not None}


class ProvenanceService:
    """Create and explain provenance edges in both directions."""

    def __init__(self, db: Any):
        self.db = db
        self.audit = AuditService(db)

    async def create_edge(
        self,
        *,
        source_entity_type: str,
        source_entity_id: Any,
        target_entity_type: str,
        target_entity_id: Any,
        relationship: str,
        actor_id: Optional[int] = None,
        explanation: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        source_details: Optional[Mapping[str, Any]] = None,
        target_details: Optional[Mapping[str, Any]] = None,
        source_context_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        source_type = _normalize_entity_type(source_entity_type)
        source_id = _normalize_entity_id(source_entity_id)
        target_type = _normalize_entity_type(target_entity_type)
        target_id = _normalize_entity_id(target_entity_id)
        normalized_relationship = _normalize_relationship(relationship)
        timestamp = created_at or _utc_now_iso()
        payload: Dict[str, Any] = _compact_details(metadata)
        if source_details:
            payload["source"] = _compact_details(source_details)
        if target_details:
            payload["target"] = _compact_details(target_details)
        edge_id = f"prov_{uuid.uuid4().hex}"
        inserted = False

        async with self.db.acquire() as conn:
            try:
                async with conn.execute(
                    """INSERT OR IGNORE INTO operational_provenance_edges
                       (edge_id, source_entity_type, source_entity_id, target_entity_type,
                        target_entity_id, relationship, explanation, metadata_json,
                        created_by_actor_id, source_context_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        edge_id,
                        source_type,
                        source_id,
                        target_type,
                        target_id,
                        normalized_relationship,
                        explanation,
                        _json_dumps(payload),
                        actor_id,
                        source_context_id,
                        timestamp,
                    ),
                ) as cur:
                    inserted = bool(cur.rowcount)
                await conn.commit()
                async with conn.execute(
                    """SELECT * FROM operational_provenance_edges
                       WHERE source_entity_type = ? AND source_entity_id = ?
                         AND target_entity_type = ? AND target_entity_id = ?
                         AND relationship = ?""",
                    (source_type, source_id, target_type, target_id, normalized_relationship),
                ) as cur:
                    row = await cur.fetchone()
            except Exception as exc:
                if "no such table: operational_provenance_edges" not in str(exc).lower():
                    raise
                logger.debug("Operational provenance table unavailable; skipping edge write")
                row = None

        edge = self._row_to_dict(dict(row)) if row else {
            "id": 0,
            "edge_id": edge_id,
            "source_entity_type": source_type,
            "source_entity_id": source_id,
            "target_entity_type": target_type,
            "target_entity_id": target_id,
            "relationship": normalized_relationship,
            "explanation": explanation,
            "metadata": payload,
            "created_by_actor_id": actor_id,
            "source_context_id": source_context_id,
            "created_at": timestamp,
        }

        if inserted:
            await self.audit.record_mutation(
                entity_type="provenance_link",
                entity_id=edge["edge_id"],
                action="provenance_link_created",
                before=None,
                after={
                    "source_entity_type": source_type,
                    "source_entity_id": source_id,
                    "target_entity_type": target_type,
                    "target_entity_id": target_id,
                    "relationship": normalized_relationship,
                },
                actor_id=actor_id,
                source_context_id=source_context_id,
                metadata={"relationship": normalized_relationship},
            )
            await self._audit_record_attachment(
                record_entity_type=source_type,
                record_entity_id=source_id,
                counterparty_type=target_type,
                counterparty_id=target_id,
                relationship=normalized_relationship,
                actor_id=actor_id,
                source_context_id=source_context_id,
            )
            await self._audit_record_attachment(
                record_entity_type=target_type,
                record_entity_id=target_id,
                counterparty_type=source_type,
                counterparty_id=source_id,
                relationship=normalized_relationship,
                actor_id=actor_id,
                source_context_id=source_context_id,
            )
        return edge

    async def record_manual_origin(
        self,
        *,
        record_id: int,
        actor_id: Optional[int],
        actor_label: str,
        surface: str,
        source_context_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.create_edge(
            source_entity_type="manual_creation",
            source_entity_id=f"manual:{surface}:{actor_id or 0}:{record_id}",
            target_entity_type="operational_record",
            target_entity_id=record_id,
            relationship="origin",
            actor_id=actor_id,
            explanation=f"Created manually through the {surface} surface.",
            source_context_id=source_context_id,
            source_details={
                "label": f"Manual creation by {actor_label}",
                "summary": f"Created manually by {actor_label} via {surface}.",
                "surface": surface,
            },
        )

    async def list_edges(
        self,
        *,
        entity_type: str,
        entity_id: Any,
        direction: str = "both",
        relationship: Optional[str] = None,
        limit: int = 200,
    ) -> list[Dict[str, Any]]:
        normalized_type = _normalize_entity_type(entity_type)
        normalized_id = _normalize_entity_id(entity_id)
        normalized_direction = str(direction or "both").strip().lower()
        if normalized_direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be one of: incoming, outgoing, both")

        clauses = []
        params: list[Any] = []
        if normalized_direction == "incoming":
            clauses.append("target_entity_type = ? AND target_entity_id = ?")
            params.extend([normalized_type, normalized_id])
        elif normalized_direction == "outgoing":
            clauses.append("source_entity_type = ? AND source_entity_id = ?")
            params.extend([normalized_type, normalized_id])
        else:
            clauses.append(
                "((source_entity_type = ? AND source_entity_id = ?) OR (target_entity_type = ? AND target_entity_id = ?))"
            )
            params.extend([normalized_type, normalized_id, normalized_type, normalized_id])

        if relationship:
            clauses.append("relationship = ?")
            params.append(_normalize_relationship(relationship))

        params.append(min(max(limit, 1), 500))
        where_sql = " AND ".join(clauses)
        async with self.db.acquire() as conn:
            try:
                async with conn.execute(
                    f"""SELECT * FROM operational_provenance_edges
                        WHERE {where_sql}
                        ORDER BY created_at ASC, id ASC
                        LIMIT ?""",
                    tuple(params),
                ) as cur:
                    rows = await cur.fetchall()
            except Exception as exc:
                if "no such table: operational_provenance_edges" not in str(exc).lower():
                    raise
                logger.debug("Operational provenance table unavailable; returning empty provenance list")
                rows = []

        edges = []
        for row in rows:
            edge = self._row_to_dict(dict(row))
            edge["source"] = await self._resolve_entity(
                edge["source_entity_type"],
                edge["source_entity_id"],
                edge["metadata"].get("source"),
            )
            edge["target"] = await self._resolve_entity(
                edge["target_entity_type"],
                edge["target_entity_id"],
                edge["metadata"].get("target"),
            )
            if edge["target_entity_type"] == normalized_type and edge["target_entity_id"] == normalized_id:
                edge["direction"] = "incoming"
            elif edge["source_entity_type"] == normalized_type and edge["source_entity_id"] == normalized_id:
                edge["direction"] = "outgoing"
            else:
                edge["direction"] = "related"
            edges.append(edge)
        return edges

    async def explain_record_origin(self, record_id: int) -> Dict[str, Any]:
        record = await self._resolve_entity("operational_record", record_id, None)
        edges = await self.list_edges(entity_type="operational_record", entity_id=record_id, direction="both", limit=500)
        origins = [
            edge for edge in edges
            if edge["direction"] == "incoming" and edge["relationship"] == "origin"
        ]
        evidence_edges = [
            edge for edge in edges
            if edge["direction"] == "incoming" and edge["relationship"] in {"evidence", "citation", "cites"}
        ]
        incoming_blocks = [
            edge for edge in edges
            if edge["direction"] == "incoming" and edge["relationship"] == "blocks"
        ]
        outgoing_blocks = [
            edge for edge in edges
            if edge["direction"] == "outgoing" and edge["relationship"] == "blocks"
        ]
        summary = self._build_record_summary(record, origins, evidence_edges, incoming_blocks, outgoing_blocks)
        return {
            "record": record,
            "summary": summary,
            "origins": origins,
            "linked_evidence_objects": [edge["source"] for edge in evidence_edges],
            "incoming_references": incoming_blocks,
            "outgoing_references": outgoing_blocks,
            "all_links": edges,
        }

    async def _audit_record_attachment(
        self,
        *,
        record_entity_type: str,
        record_entity_id: str,
        counterparty_type: str,
        counterparty_id: str,
        relationship: str,
        actor_id: Optional[int],
        source_context_id: Optional[str],
    ) -> None:
        if record_entity_type != "operational_record":
            return
        await self.audit.record_mutation(
            entity_type="operational_record",
            entity_id=record_entity_id,
            action="traceability_link_added",
            before=None,
            after={
                "relationship": relationship,
                "counterparty_type": counterparty_type,
                "counterparty_id": counterparty_id,
            },
            actor_id=actor_id,
            source_context_id=source_context_id,
            metadata={"relationship": relationship},
        )

    async def _resolve_entity(
        self,
        entity_type: str,
        entity_id: str,
        details: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        normalized_type = _normalize_entity_type(entity_type)
        normalized_id = _normalize_entity_id(entity_id)
        fallback = _compact_details(details)

        async with self.db.acquire() as conn:
            try:
                if normalized_type == "operational_record":
                    async with conn.execute("SELECT * FROM operational_records WHERE id = ?", (int(normalized_id),)) as cur:
                        row = await cur.fetchone()
                    if row:
                        record = dict(row)
                        return {
                            "entity_type": normalized_type,
                            "entity_id": normalized_id,
                            "label": record.get("title") or fallback.get("label") or f"Operational record #{normalized_id}",
                            "summary": record.get("summary") or fallback.get("summary"),
                            "kind": record.get("record_type"),
                            "object": record,
                        }
                if normalized_type in {"meeting", "meeting_note"}:
                    async with conn.execute("SELECT * FROM meeting_notes WHERE id = ?", (int(normalized_id),)) as cur:
                        row = await cur.fetchone()
                    if row:
                        meeting = dict(row)
                        return {
                            "entity_type": normalized_type,
                            "entity_id": normalized_id,
                            "label": meeting.get("title") or fallback.get("label") or f"Meeting #{normalized_id}",
                            "summary": meeting.get("summary") or fallback.get("summary"),
                            "kind": "meeting",
                            "object": meeting,
                        }
                if normalized_type in {"action", "task"}:
                    async with conn.execute("SELECT * FROM tasks WHERE id = ?", (int(normalized_id),)) as cur:
                        row = await cur.fetchone()
                    if row:
                        action = dict(row)
                        return {
                            "entity_type": normalized_type,
                            "entity_id": normalized_id,
                            "label": action.get("title") or fallback.get("label") or f"Action #{normalized_id}",
                            "summary": action.get("description") or fallback.get("summary"),
                            "kind": "action",
                            "object": action,
                        }
                if normalized_type == "decision":
                    async with conn.execute("SELECT * FROM decisions WHERE id = ?", (int(normalized_id),)) as cur:
                        row = await cur.fetchone()
                    if row:
                        decision = dict(row)
                        return {
                            "entity_type": normalized_type,
                            "entity_id": normalized_id,
                            "label": decision.get("title") or fallback.get("label") or f"Decision #{normalized_id}",
                            "summary": decision.get("rationale") or decision.get("decision") or fallback.get("summary"),
                            "kind": "decision",
                            "object": decision,
                        }
                if normalized_type == "message":
                    async with conn.execute("SELECT * FROM inbox_threads WHERE id = ?", (int(normalized_id),)) as cur:
                        row = await cur.fetchone()
                    if row:
                        message = dict(row)
                        return {
                            "entity_type": normalized_type,
                            "entity_id": normalized_id,
                            "label": fallback.get("label") or message.get("subject") or f"Message #{normalized_id}",
                            "summary": fallback.get("summary") or message.get("processing_status") or message.get("status"),
                            "kind": "message",
                            "object": message,
                        }
            except Exception:
                logger.debug("Failed resolving provenance entity %s:%s", normalized_type, normalized_id, exc_info=True)

        return {
            "entity_type": normalized_type,
            "entity_id": normalized_id,
            "label": fallback.get("label") or fallback.get("title") or f"{normalized_type.replace('_', ' ').title()} {normalized_id}",
            "summary": fallback.get("summary"),
            "kind": fallback.get("kind") or normalized_type,
            "object": fallback or None,
        }

    def _build_record_summary(
        self,
        record: Mapping[str, Any],
        origins: list[Dict[str, Any]],
        evidence_edges: list[Dict[str, Any]],
        incoming_blocks: list[Dict[str, Any]],
        outgoing_blocks: list[Dict[str, Any]],
    ) -> str:
        label = record.get("label") or f"Operational record #{record.get('entity_id')}"
        kind = str(record.get("kind") or "record").replace("_", " ")
        if not origins:
            origin_text = "has no recorded origins yet"
        else:
            origin_labels = [edge["source"].get("label") or edge["source"]["entity_id"] for edge in origins[:3]]
            if len(origins) > 3:
                origin_labels.append(f"and {len(origins) - 3} more")
            origin_text = "exists because of " + ", ".join(origin_labels)

        details = [f"This {kind} \"{label}\" {origin_text}."]
        if evidence_edges:
            details.append(f"It carries {len(evidence_edges)} evidence link{'s' if len(evidence_edges) != 1 else ''}.")
        if incoming_blocks:
            details.append(f"It is referenced by {len(incoming_blocks)} blocker link{'s' if len(incoming_blocks) != 1 else ''}.")
        if outgoing_blocks:
            details.append(f"It blocks {len(outgoing_blocks)} linked record{'s' if len(outgoing_blocks) != 1 else ''}.")
        return " ".join(details)

    def _row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row["metadata"] = _json_loads(row.pop("metadata_json", None))
        return row