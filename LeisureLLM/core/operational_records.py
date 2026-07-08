"""Canonical operational record model and validation rules.

This layer is additive. It does not replace the current task, decision,
lead, or knowledge-gap authority tables. Instead it provides a shared,
typed continuity schema that new operational flows can target directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Set

from core.symbolic_rules import StateMachine


class OperationalRecordValidationError(ValueError):
    """Raised when an operational record fails schema validation."""


class OperationalRecordType(str, Enum):
    ACTION = "action"
    DECISION = "decision"
    BLOCKER = "blocker"
    SOURCE_LINK = "source_link"


class ActionState(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELED = "canceled"
    OVERDUE = "overdue"
    STALE = "stale"
    UNOWNED = "unowned"
    ESCALATED = "escalated"


class DecisionState(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    UNRESOLVED = "unresolved"


class BlockerState(str, Enum):
    OPEN = "open"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class SourceLinkState(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    BROKEN = "broken"
    ARCHIVED = "archived"


STATE_ENUMS: Dict[OperationalRecordType, type[Enum]] = {
    OperationalRecordType.ACTION: ActionState,
    OperationalRecordType.DECISION: DecisionState,
    OperationalRecordType.BLOCKER: BlockerState,
    OperationalRecordType.SOURCE_LINK: SourceLinkState,
}

DEFAULT_STATE_BY_TYPE: Dict[OperationalRecordType, str] = {
    OperationalRecordType.ACTION: ActionState.OPEN.value,
    OperationalRecordType.DECISION: DecisionState.PROPOSED.value,
    OperationalRecordType.BLOCKER: BlockerState.OPEN.value,
    OperationalRecordType.SOURCE_LINK: SourceLinkState.ACTIVE.value,
}

RESOLVED_STATES_BY_TYPE: Dict[OperationalRecordType, Set[str]] = {
    OperationalRecordType.ACTION: {ActionState.DONE.value, ActionState.CANCELED.value},
    OperationalRecordType.DECISION: {
        DecisionState.ACCEPTED.value,
        DecisionState.REJECTED.value,
        DecisionState.SUPERSEDED.value,
    },
    OperationalRecordType.BLOCKER: {BlockerState.RESOLVED.value},
    OperationalRecordType.SOURCE_LINK: set(),
}

ARCHIVED_STATES_BY_TYPE: Dict[OperationalRecordType, Set[str]] = {
    OperationalRecordType.ACTION: set(),
    OperationalRecordType.DECISION: set(),
    OperationalRecordType.BLOCKER: set(),
    OperationalRecordType.SOURCE_LINK: {SourceLinkState.ARCHIVED.value},
}

RECORD_CAPABILITIES: Dict[OperationalRecordType, Dict[str, bool]] = {
    OperationalRecordType.ACTION: {
        "supports_owner": True,
        "supports_due_at": True,
        "supports_review_at": True,
        "supports_stale_after_at": True,
        "supports_rationale": True,
    },
    OperationalRecordType.DECISION: {
        "supports_owner": True,
        "supports_due_at": False,
        "supports_review_at": True,
        "supports_stale_after_at": False,
        "supports_rationale": True,
    },
    OperationalRecordType.BLOCKER: {
        "supports_owner": True,
        "supports_due_at": False,
        "supports_review_at": True,
        "supports_stale_after_at": True,
        "supports_rationale": True,
    },
    OperationalRecordType.SOURCE_LINK: {
        "supports_owner": False,
        "supports_due_at": False,
        "supports_review_at": True,
        "supports_stale_after_at": True,
        "supports_rationale": True,
    },
}

ACTION_FSM = StateMachine(
    name="operational_action",
    transitions={
        ActionState.OPEN.value: {
            ActionState.IN_PROGRESS.value,
            ActionState.BLOCKED.value,
            ActionState.DONE.value,
            ActionState.CANCELED.value,
            ActionState.OVERDUE.value,
            ActionState.STALE.value,
            ActionState.UNOWNED.value,
            ActionState.ESCALATED.value,
        },
        ActionState.IN_PROGRESS.value: {
            ActionState.OPEN.value,
            ActionState.BLOCKED.value,
            ActionState.DONE.value,
            ActionState.CANCELED.value,
            ActionState.OVERDUE.value,
            ActionState.STALE.value,
            ActionState.ESCALATED.value,
        },
        ActionState.BLOCKED.value: {
            ActionState.OPEN.value,
            ActionState.IN_PROGRESS.value,
            ActionState.CANCELED.value,
            ActionState.ESCALATED.value,
        },
        ActionState.DONE.value: set(),
        ActionState.CANCELED.value: set(),
        ActionState.OVERDUE.value: {
            ActionState.IN_PROGRESS.value,
            ActionState.BLOCKED.value,
            ActionState.DONE.value,
            ActionState.CANCELED.value,
            ActionState.STALE.value,
            ActionState.ESCALATED.value,
        },
        ActionState.STALE.value: {
            ActionState.OPEN.value,
            ActionState.IN_PROGRESS.value,
            ActionState.BLOCKED.value,
            ActionState.DONE.value,
            ActionState.CANCELED.value,
            ActionState.OVERDUE.value,
            ActionState.ESCALATED.value,
        },
        ActionState.UNOWNED.value: {
            ActionState.OPEN.value,
            ActionState.IN_PROGRESS.value,
            ActionState.BLOCKED.value,
            ActionState.CANCELED.value,
            ActionState.ESCALATED.value,
        },
        ActionState.ESCALATED.value: {
            ActionState.OPEN.value,
            ActionState.IN_PROGRESS.value,
            ActionState.BLOCKED.value,
            ActionState.DONE.value,
            ActionState.CANCELED.value,
        },
    },
)

DECISION_FSM = StateMachine(
    name="operational_decision",
    transitions={
        DecisionState.PROPOSED.value: {
            DecisionState.ACCEPTED.value,
            DecisionState.REJECTED.value,
            DecisionState.UNRESOLVED.value,
        },
        DecisionState.ACCEPTED.value: {DecisionState.SUPERSEDED.value},
        DecisionState.REJECTED.value: set(),
        DecisionState.SUPERSEDED.value: set(),
        DecisionState.UNRESOLVED.value: {
            DecisionState.ACCEPTED.value,
            DecisionState.REJECTED.value,
        },
    },
)

BLOCKER_FSM = StateMachine(
    name="operational_blocker",
    transitions={
        BlockerState.OPEN.value: {
            BlockerState.MITIGATED.value,
            BlockerState.RESOLVED.value,
            BlockerState.ESCALATED.value,
        },
        BlockerState.MITIGATED.value: {
            BlockerState.OPEN.value,
            BlockerState.RESOLVED.value,
            BlockerState.ESCALATED.value,
        },
        BlockerState.RESOLVED.value: set(),
        BlockerState.ESCALATED.value: {
            BlockerState.OPEN.value,
            BlockerState.MITIGATED.value,
            BlockerState.RESOLVED.value,
        },
    },
)

SOURCE_LINK_FSM = StateMachine(
    name="operational_source_link",
    transitions={
        SourceLinkState.ACTIVE.value: {
            SourceLinkState.STALE.value,
            SourceLinkState.BROKEN.value,
            SourceLinkState.ARCHIVED.value,
        },
        SourceLinkState.STALE.value: {
            SourceLinkState.ACTIVE.value,
            SourceLinkState.BROKEN.value,
            SourceLinkState.ARCHIVED.value,
        },
        SourceLinkState.BROKEN.value: {
            SourceLinkState.ACTIVE.value,
            SourceLinkState.ARCHIVED.value,
        },
        SourceLinkState.ARCHIVED.value: set(),
    },
)

STATE_MACHINES: Dict[OperationalRecordType, StateMachine] = {
    OperationalRecordType.ACTION: ACTION_FSM,
    OperationalRecordType.DECISION: DECISION_FSM,
    OperationalRecordType.BLOCKER: BLOCKER_FSM,
    OperationalRecordType.SOURCE_LINK: SOURCE_LINK_FSM,
}


@dataclass(slots=True)
class OperationalRecordInput:
    record_type: str
    title: str
    summary: Optional[str] = None
    state: Optional[str] = None
    owner_id: Optional[int] = None
    created_by_actor_id: Optional[int] = None
    updated_by_actor_id: Optional[int] = None
    source_context_id: Optional[str] = None
    workspace_scope: Optional[str] = None
    project_scope: Optional[str] = None
    due_at: Optional[str] = None
    stale_after_at: Optional[str] = None
    review_at: Optional[str] = None
    rationale: Optional[str] = None
    notes: Optional[str] = None
    resolved_at: Optional[str] = None
    archived_at: Optional[str] = None
    deliverables: Optional[str] = None
    canonical_payload: Optional[Mapping[str, Any]] = None


def normalize_record_type(record_type: str | OperationalRecordType) -> OperationalRecordType:
    try:
        return record_type if isinstance(record_type, OperationalRecordType) else OperationalRecordType(str(record_type).strip().lower())
    except Exception as exc:
        allowed = ", ".join(sorted(item.value for item in OperationalRecordType))
        raise OperationalRecordValidationError(f"Unknown operational record type: {record_type!r}. Allowed: {allowed}") from exc


def normalize_state(record_type: str | OperationalRecordType, state: Optional[str]) -> str:
    record_kind = normalize_record_type(record_type)
    if state is None or not str(state).strip():
        return DEFAULT_STATE_BY_TYPE[record_kind]

    state_value = str(state).strip().lower()
    enum_cls = STATE_ENUMS[record_kind]
    try:
        return enum_cls(state_value).value
    except Exception as exc:
        allowed = ", ".join(sorted(member.value for member in enum_cls))
        raise OperationalRecordValidationError(
            f"Invalid state {state!r} for record type {record_kind.value!r}. Allowed: {allowed}"
        ) from exc


def validate_transition(record_type: str | OperationalRecordType, current_state: str, target_state: str) -> bool:
    record_kind = normalize_record_type(record_type)
    current = normalize_state(record_kind, current_state)
    target = normalize_state(record_kind, target_state)
    if current == target:
        return True
    return STATE_MACHINES[record_kind].validate(current, target)


def is_resolved_state(record_type: str | OperationalRecordType, state: str) -> bool:
    record_kind = normalize_record_type(record_type)
    return normalize_state(record_kind, state) in RESOLVED_STATES_BY_TYPE[record_kind]


def is_archived_state(record_type: str | OperationalRecordType, state: str) -> bool:
    record_kind = normalize_record_type(record_type)
    return normalize_state(record_kind, state) in ARCHIVED_STATES_BY_TYPE[record_kind]


def _validate_datetime(value: Optional[str], field_name: str) -> None:
    if value in (None, ""):
        return
    raw = str(value).strip()
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationalRecordValidationError(f"{field_name} must be ISO-8601 compatible: {value!r}") from exc


def validate_operational_record_input(record: OperationalRecordInput) -> OperationalRecordInput:
    record_kind = normalize_record_type(record.record_type)
    title = (record.title or "").strip()
    if not title:
        raise OperationalRecordValidationError("Operational record title cannot be empty")

    state = normalize_state(record_kind, record.state)
    capabilities = RECORD_CAPABILITIES[record_kind]

    if record.created_by_actor_id is None:
        raise OperationalRecordValidationError("created_by_actor_id is required")

    if record.updated_by_actor_id is None:
        record.updated_by_actor_id = record.created_by_actor_id

    if record.owner_id is not None and not capabilities["supports_owner"]:
        raise OperationalRecordValidationError(f"{record_kind.value} records do not support owner_id")

    if record.due_at is not None and not capabilities["supports_due_at"]:
        raise OperationalRecordValidationError(f"{record_kind.value} records do not support due_at")

    if record.review_at is not None and not capabilities["supports_review_at"]:
        raise OperationalRecordValidationError(f"{record_kind.value} records do not support review_at")

    if record.stale_after_at is not None and not capabilities["supports_stale_after_at"]:
        raise OperationalRecordValidationError(f"{record_kind.value} records do not support stale_after_at")

    if record_kind is OperationalRecordType.ACTION:
        if record.owner_id is None and state != ActionState.UNOWNED.value:
            raise OperationalRecordValidationError("Action records without an owner must use state 'unowned'")
        if record.owner_id is not None and state == ActionState.UNOWNED.value:
            raise OperationalRecordValidationError("Action records in state 'unowned' cannot have owner_id set")

    if record_kind is OperationalRecordType.SOURCE_LINK and not (record.source_context_id or "").strip():
        raise OperationalRecordValidationError("source_link records require source_context_id for traceability")

    _validate_datetime(record.due_at, "due_at")
    _validate_datetime(record.review_at, "review_at")
    _validate_datetime(record.stale_after_at, "stale_after_at")
    _validate_datetime(record.resolved_at, "resolved_at")
    _validate_datetime(record.archived_at, "archived_at")

    record.record_type = record_kind.value
    record.title = title[:500]
    record.summary = (record.summary or None)
    record.state = state
    return record