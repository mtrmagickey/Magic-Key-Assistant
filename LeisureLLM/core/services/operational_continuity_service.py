"""Core operational continuity sweep service.

Computes review-critical continuity conditions regardless of source surface
and persists them durably for web, Discord, and import pipelines alike.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from core.services.audit_service import AuditService
from core.services.operational_record_service import OperationalRecordService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _json_dumps(value: Optional[dict[str, Any]]) -> str:
    return json.dumps(dict(value or {}), sort_keys=True)


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_dt(raw_value: Optional[str], *, end_of_day_for_date_only: bool = False) -> Optional[datetime]:
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10 and value.count("-") == 2:
            parsed_date = date.fromisoformat(value)
            parsed_time = time.max.replace(microsecond=0) if end_of_day_for_date_only else time.min
            dt = datetime.combine(parsed_date, parsed_time)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(slots=True)
class ContinuityPolicy:
    stale_action_days: int = 14
    stale_blocker_days: int = 7
    unresolved_decision_days: int = 7
    escalate_overdue_action_days: int = 3
    escalate_stale_blocker_days: int = 3


@dataclass(slots=True)
class ContinuityCandidate:
    continuity_state: str
    reason: str
    details: dict[str, Any]


@dataclass(slots=True)
class ContinuitySweepResult:
    checked_records: int = 0
    active_count: int = 0
    activated_count: int = 0
    updated_count: int = 0
    cleared_count: int = 0
    states_by_type: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["states_by_type"] = dict(self.states_by_type or {})
        return data


class OperationalContinuityService:
    """Compute and persist continuity conditions for canonical records."""

    supported_states = frozenset({"overdue", "stale", "unowned", "unresolved", "escalated"})
    _decision_terminal_states = frozenset({"accepted", "rejected", "superseded"})
    _action_terminal_states = frozenset({"done", "canceled"})
    _blocker_terminal_states = frozenset({"resolved"})

    def __init__(self, db: Any, *, policy: Optional[ContinuityPolicy] = None):
        self.db = db
        self.policy = policy or ContinuityPolicy()
        self.audit = AuditService(db)
        self.records = OperationalRecordService(db)

    async def ensure_sweep_actor(self) -> dict[str, Any]:
        return await self.records.ensure_actor(
            actor_kind="system_job",
            external_ref="operational-continuity-sweep",
            display_name="Operational Continuity Sweep",
        )

    async def list_states(
        self,
        *,
        active_only: bool = True,
        continuity_state: Optional[str] = None,
        record_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if active_only:
            where.append("s.status = 'active'")
        if continuity_state:
            where.append("s.continuity_state = ?")
            params.append(str(continuity_state).strip().lower())
        if record_type:
            where.append("s.record_type = ?")
            params.append(str(record_type).strip().lower())

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(int(limit))
        async with self.db.acquire() as conn, conn.execute(
            f"""
                SELECT s.*, r.title, r.summary, r.state AS record_state,
                       r.owner_id, r.due_at, r.review_at, r.stale_after_at,
                       a.display_name AS owner_display_name
                FROM operational_continuity_states s
                JOIN operational_records r ON r.id = s.record_id
                LEFT JOIN operational_actors a ON a.id = r.owner_id
                {clause}
                ORDER BY s.status = 'active' DESC, s.last_observed_at DESC, s.id DESC
                LIMIT ?
                """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["details"] = _json_loads(item.pop("details_json", None))
            results.append(item)
        return results

    async def list_state_events(self, continuity_state_id: int) -> list[dict[str, Any]]:
        async with self.db.acquire() as conn, conn.execute(
            "SELECT * FROM operational_continuity_state_events WHERE continuity_state_id = ? ORDER BY id ASC",
            (continuity_state_id,),
        ) as cur:
            rows = await cur.fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json", None))
            events.append(item)
        return events

    async def run_sweep(
        self,
        *,
        actor_id: Optional[int] = None,
        source_context_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        now_dt = now.astimezone(timezone.utc) if now else _utc_now()
        actor = await self.ensure_sweep_actor() if actor_id is None else await self.records.get_actor(int(actor_id))
        if not actor:
            raise ValueError("Continuity sweep requires a valid actor")
        actor_id = int(actor["id"])
        source_context_id = source_context_id or f"job:operational-continuity-sweep:{now_dt.strftime('%Y%m%dT%H%M%SZ')}"

        records = await self._list_candidate_records()
        active_rows = await self._list_existing_states(status="active")
        active_by_key = {
            (int(row["record_id"]), str(row["continuity_state"])): row
            for row in active_rows
        }

        result = ContinuitySweepResult(checked_records=len(records), states_by_type={})
        seen_keys: set[tuple[int, str]] = set()

        for record in records:
            candidates = self._evaluate_record(record, now=now_dt)
            for candidate in candidates:
                key = (int(record["id"]), candidate.continuity_state)
                seen_keys.add(key)
                mutation = await self._activate_state(
                    record=record,
                    candidate=candidate,
                    actor_id=actor_id,
                    source_context_id=source_context_id,
                    existing=active_by_key.get(key),
                    observed_at=now_dt,
                )
                result.active_count += 1
                result.states_by_type[candidate.continuity_state] = result.states_by_type.get(candidate.continuity_state, 0) + 1
                if mutation == "activated":
                    result.activated_count += 1
                elif mutation == "updated":
                    result.updated_count += 1

        for key, existing in active_by_key.items():
            if key in seen_keys:
                continue
            await self._clear_state(
                state_row=existing,
                actor_id=actor_id,
                source_context_id=source_context_id,
                cleared_at=now_dt,
            )
            result.cleared_count += 1

        return result.to_dict()

    async def _list_candidate_records(self) -> list[dict[str, Any]]:
        async with self.db.acquire() as conn, conn.execute(
            """
                SELECT *
                FROM operational_records
                WHERE archived_at IS NULL
                  AND record_type IN ('action', 'decision', 'blocker')
                ORDER BY id ASC
                """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def _list_existing_states(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        clause = ""
        if status:
            clause = "WHERE status = ?"
            params.append(status)
        async with self.db.acquire() as conn, conn.execute(
            f"SELECT * FROM operational_continuity_states {clause}",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    def _evaluate_record(self, record: dict[str, Any], *, now: datetime) -> list[ContinuityCandidate]:
        record_type = str(record.get("record_type") or "").strip().lower()
        current_state = str(record.get("state") or "").strip().lower()
        candidates: list[ContinuityCandidate] = []

        if record_type == "action" and current_state not in self._action_terminal_states:
            due_at = _normalize_dt(record.get("due_at"), end_of_day_for_date_only=True)
            stale_after_at = _normalize_dt(record.get("stale_after_at"), end_of_day_for_date_only=True)
            updated_at = _normalize_dt(record.get("updated_at"))

            if due_at and due_at < now:
                overdue_days = max(1, int((now - due_at).total_seconds() // 86400))
                candidates.append(
                    ContinuityCandidate(
                        continuity_state="overdue",
                        reason="Action due date passed before resolution.",
                        details={
                            "rule": "overdue_action",
                            "due_at": record.get("due_at"),
                            "days_overdue": overdue_days,
                        },
                    )
                )
                if overdue_days >= self.policy.escalate_overdue_action_days:
                    candidates.append(
                        ContinuityCandidate(
                            continuity_state="escalated",
                            reason="Escalation hook matched an overdue action beyond policy tolerance.",
                            details={
                                "rule": "escalate_overdue_action",
                                "trigger_state": "overdue",
                                "days_overdue": overdue_days,
                                "threshold_days": self.policy.escalate_overdue_action_days,
                                "policy_hook": "overdue_action_age",
                            },
                        )
                    )

            stale_days = None
            if stale_after_at and stale_after_at < now:
                stale_days = max(1, int((now - stale_after_at).total_seconds() // 86400))
            elif updated_at and updated_at < now - timedelta(days=self.policy.stale_action_days):
                stale_days = max(1, int((now - updated_at).total_seconds() // 86400))
            if stale_days is not None:
                candidates.append(
                    ContinuityCandidate(
                        continuity_state="stale",
                        reason="Action has gone stale and needs review.",
                        details={
                            "rule": "stale_action",
                            "stale_after_at": record.get("stale_after_at"),
                            "last_activity_at": record.get("updated_at"),
                            "days_stale": stale_days,
                            "policy_days": self.policy.stale_action_days,
                        },
                    )
                )

            if record.get("owner_id") is None:
                candidates.append(
                    ContinuityCandidate(
                        continuity_state="unowned",
                        reason="Action has no assigned owner.",
                        details={
                            "rule": "unowned_action",
                            "record_state": current_state,
                        },
                    )
                )

        if record_type == "decision" and current_state not in self._decision_terminal_states:
            review_at = _normalize_dt(record.get("review_at"), end_of_day_for_date_only=True)
            updated_at = _normalize_dt(record.get("updated_at"))
            unresolved_days = None
            if review_at and review_at < now:
                unresolved_days = max(1, int((now - review_at).total_seconds() // 86400))
            elif updated_at and updated_at < now - timedelta(days=self.policy.unresolved_decision_days):
                unresolved_days = max(1, int((now - updated_at).total_seconds() // 86400))
            if unresolved_days is not None:
                candidates.append(
                    ContinuityCandidate(
                        continuity_state="unresolved",
                        reason="Decision remains unresolved beyond its review window.",
                        details={
                            "rule": "unresolved_decision",
                            "review_at": record.get("review_at"),
                            "last_activity_at": record.get("updated_at"),
                            "days_unresolved": unresolved_days,
                            "policy_days": self.policy.unresolved_decision_days,
                        },
                    )
                )

        if record_type == "blocker" and current_state not in self._blocker_terminal_states:
            stale_after_at = _normalize_dt(record.get("stale_after_at"), end_of_day_for_date_only=True)
            updated_at = _normalize_dt(record.get("updated_at"))
            stale_days = None
            if stale_after_at and stale_after_at < now:
                stale_days = max(1, int((now - stale_after_at).total_seconds() // 86400))
            elif updated_at and updated_at < now - timedelta(days=self.policy.stale_blocker_days):
                stale_days = max(1, int((now - updated_at).total_seconds() // 86400))
            if stale_days is not None:
                candidates.append(
                    ContinuityCandidate(
                        continuity_state="stale",
                        reason="Blocker has remained unresolved long enough to be considered stale.",
                        details={
                            "rule": "stale_blocker",
                            "stale_after_at": record.get("stale_after_at"),
                            "last_activity_at": record.get("updated_at"),
                            "days_stale": stale_days,
                            "policy_days": self.policy.stale_blocker_days,
                        },
                    )
                )
                if stale_days >= self.policy.escalate_stale_blocker_days:
                    candidates.append(
                        ContinuityCandidate(
                            continuity_state="escalated",
                            reason="Escalation hook matched a stale blocker beyond policy tolerance.",
                            details={
                                "rule": "escalate_stale_blocker",
                                "trigger_state": "stale",
                                "days_stale": stale_days,
                                "threshold_days": self.policy.escalate_stale_blocker_days,
                                "policy_hook": "stale_blocker_age",
                            },
                        )
                    )

        deduped: dict[str, ContinuityCandidate] = {}
        for candidate in candidates:
            deduped[candidate.continuity_state] = candidate
        return list(deduped.values())

    async def _activate_state(
        self,
        *,
        record: dict[str, Any],
        candidate: ContinuityCandidate,
        actor_id: int,
        source_context_id: Optional[str],
        existing: Optional[dict[str, Any]],
        observed_at: datetime,
    ) -> str:
        details_json = _json_dumps(candidate.details)
        observed_at_iso = observed_at.isoformat()
        if existing:
            changed = (
                existing.get("reason") != candidate.reason
                or str(existing.get("details_json") or "{}") != details_json
                or existing.get("status") != "active"
            )
            await self.db.execute(
                """
                UPDATE operational_continuity_states
                SET status = 'active',
                reason = ?,
                details_json = ?,
                source_context_id = ?,
                updated_by_actor_id = ?,
                last_observed_at = ?,
                cleared_at = NULL
                WHERE id = ?
                """,
                (
                candidate.reason,
                details_json,
                source_context_id,
                actor_id,
                observed_at_iso,
                existing["id"],
                ),
                )
            if changed:
                await self._append_state_event(
                    state_id=int(existing["id"]),
                    record=record,
                    continuity_state=candidate.continuity_state,
                    event_type="updated" if existing.get("status") == "active" else "activated",
                    actor_id=actor_id,
                    source_context_id=source_context_id,
                    summary=candidate.reason,
                    payload=candidate.details,
                )
                await self.audit.record_mutation(
                    entity_type="operational_continuity_state",
                    entity_id=int(existing["id"]),
                    action="state_updated",
                    before={
                        "status": existing.get("status"),
                        "reason": existing.get("reason"),
                        "details": _json_loads(existing.get("details_json")),
                    },
                    after={
                        "status": "active",
                        "reason": candidate.reason,
                        "details": candidate.details,
                    },
                    actor_id=actor_id,
                    source_context_id=source_context_id,
                    metadata={
                        "record_id": record["id"],
                        "record_type": record["record_type"],
                        "continuity_state": candidate.continuity_state,
                    },
                )
                return "updated"
            return "refreshed"

        async with self.db.acquire() as conn:
            async with conn.execute(
                """
                INSERT INTO operational_continuity_states
                    (record_id, record_stable_id, record_type, continuity_state, status,
                     reason, details_json, source_context_id,
                     created_by_actor_id, updated_by_actor_id,
                     first_observed_at, last_observed_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["stable_id"],
                    record["record_type"],
                    candidate.continuity_state,
                    candidate.reason,
                    details_json,
                    source_context_id,
                    actor_id,
                    actor_id,
                    observed_at_iso,
                    observed_at_iso,
                ),
            ) as cur:
                state_id = cur.lastrowid
            await conn.commit()
        await self._append_state_event(
            state_id=int(state_id),
            record=record,
            continuity_state=candidate.continuity_state,
            event_type="activated",
            actor_id=actor_id,
            source_context_id=source_context_id,
            summary=candidate.reason,
            payload=candidate.details,
        )
        await self.audit.record_mutation(
            entity_type="operational_continuity_state",
            entity_id=int(state_id),
            action="state_activated",
            before=None,
            after={
                "status": "active",
                "reason": candidate.reason,
                "details": candidate.details,
            },
            actor_id=actor_id,
            source_context_id=source_context_id,
            metadata={
                "record_id": record["id"],
                "record_type": record["record_type"],
                "continuity_state": candidate.continuity_state,
            },
        )
        return "activated"

    async def _clear_state(
        self,
        *,
        state_row: dict[str, Any],
        actor_id: int,
        source_context_id: Optional[str],
        cleared_at: datetime,
    ) -> None:
        cleared_at_iso = cleared_at.isoformat()
        await self.db.execute(
            """
            UPDATE operational_continuity_states
            SET status = 'cleared',
            updated_by_actor_id = ?,
            source_context_id = ?,
            last_observed_at = ?,
            cleared_at = ?
            WHERE id = ?
            """,
            (actor_id, source_context_id, cleared_at_iso, cleared_at_iso, state_row["id"]),
            )
        record = {
            "id": state_row["record_id"],
            "record_type": state_row["record_type"],
        }
        await self._append_state_event(
            state_id=int(state_row["id"]),
            record=record,
            continuity_state=str(state_row["continuity_state"]),
            event_type="cleared",
            actor_id=actor_id,
            source_context_id=source_context_id,
            summary=f"Cleared {state_row['continuity_state']} continuity state.",
            payload={"previous_reason": state_row.get("reason")},
        )
        await self.audit.record_mutation(
            entity_type="operational_continuity_state",
            entity_id=int(state_row["id"]),
            action="state_cleared",
            before={
                "status": state_row.get("status"),
                "reason": state_row.get("reason"),
                "details": _json_loads(state_row.get("details_json")),
            },
            after={
                "status": "cleared",
                "reason": state_row.get("reason"),
                "details": _json_loads(state_row.get("details_json")),
            },
            actor_id=actor_id,
            source_context_id=source_context_id,
            metadata={
                "record_id": state_row["record_id"],
                "record_type": state_row["record_type"],
                "continuity_state": state_row["continuity_state"],
            },
        )

    async def _append_state_event(
        self,
        *,
        state_id: int,
        record: dict[str, Any],
        continuity_state: str,
        event_type: str,
        actor_id: int,
        source_context_id: Optional[str],
        summary: Optional[str],
        payload: Optional[dict[str, Any]],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO operational_continuity_state_events
            (continuity_state_id, record_id, record_type, continuity_state,
            event_type, actor_id, source_context_id, summary, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
            state_id,
            record["id"],
            record["record_type"],
            continuity_state,
            event_type,
            actor_id,
            source_context_id,
            summary,
            _json_dumps(payload),
            ),
            )