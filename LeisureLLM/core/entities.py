"""
Core entity definitions — Discord-free data models.

These are the product-surface artifacts described in the product
roadmap.  They mirror the database schema but add validation,
factory methods, and serialisation helpers.

Original six:
    ActionItem   — owner, due, status, dependencies
    Decision     — what, why, who, when, linked evidence
    Lead         — stage, next action, value range, last touch
    MeetingNote  — summary, decisions, actions, risks
    KnowledgeGap — question, owner, resolution path
    SourceLink   — message id, doc id, timestamp, provenance

Continuity types (M2.5):
    Obligation   — recurring requirement with next-due, owner, evidence
    SOP          — versioned runbook with checklist and exercise log
    Feedback     — structured product feedback with environment snapshot

Rails types (M3):
    Rail         — venture lifecycle track (Validate / Launch / Operate)
    RailStage    — a stage within a rail with required outputs
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# ── Enums ─────────────────────────────────────────────────────

class ActionStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class LeadStage(str, Enum):
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"
    PROPOSAL = "proposal"
    WON = "won"
    LOST = "lost"


class GapStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


# ── Entities ──────────────────────────────────────────────────

@dataclass
class ActionItem:
    """A trackable unit of work with an owner and due date."""

    title: str
    id: Optional[int] = None
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None
    status: ActionStatus = ActionStatus.TODO
    priority: Priority = Priority.MEDIUM
    due_date: Optional[str] = None                # ISO 8601
    completed_at: Optional[str] = None
    dependencies: List[int] = field(default_factory=list)  # IDs of blocking items
    tags: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    # Artifact linkage
    source_meeting_id: Optional[int] = None
    source_decision_id: Optional[int] = None

    def __post_init__(self):
        if not self.title or not self.title.strip():
            raise ValueError("ActionItem title cannot be empty")
        self.title = self.title.strip()[:500]
        if isinstance(self.status, str):
            self.status = ActionStatus(self.status)
        if isinstance(self.priority, str):
            self.priority = Priority(self.priority)

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        d["tags"] = json.dumps(self.tags) if self.tags else None
        d["dependencies"] = json.dumps(self.dependencies) if self.dependencies else None
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Decision:
    """A recorded decision with full provenance for recall."""

    title: str
    decision: str                    # The actual decision text
    rationale: Optional[str] = None
    id: Optional[int] = None
    decided_by: List[str] = field(default_factory=list)
    decided_at: Optional[str] = None
    category: Optional[str] = None   # technical, business, process, …
    impact: Optional[str] = None     # low, medium, high
    linked_evidence: List[str] = field(default_factory=list)  # doc IDs, message IDs
    related_project_id: Optional[int] = None
    tags: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.title or not self.title.strip():
            raise ValueError("Decision title cannot be empty")
        if not self.decision or not self.decision.strip():
            raise ValueError("Decision text cannot be empty")

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decided_by"] = json.dumps(self.decided_by) if self.decided_by else None
        d["linked_evidence"] = json.dumps(self.linked_evidence) if self.linked_evidence else None
        d["tags"] = json.dumps(self.tags) if self.tags else None
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Lead:
    """A sales or partnership opportunity with pipeline stage tracking."""

    name: str
    id: Optional[int] = None
    stage: LeadStage = LeadStage.COLD
    contact_name: Optional[str] = None
    contact_info: Optional[str] = None
    value_range: Optional[str] = None   # e.g. "£25k–£40k"
    next_action: Optional[str] = None
    next_action_date: Optional[str] = None
    last_touch: Optional[str] = None
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Lead name cannot be empty")
        if isinstance(self.stage, str):
            self.stage = LeadStage(self.stage)

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stage"] = self.stage.value
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class MeetingNote:
    """Structured output of a parsed meeting."""

    summary: str
    id: Optional[int] = None
    meeting_date: Optional[str] = None
    attendees: List[str] = field(default_factory=list)
    decisions: List[int] = field(default_factory=list)     # Decision IDs
    actions: List[int] = field(default_factory=list)        # ActionItem IDs
    risks: List[str] = field(default_factory=list)
    raw_text: Optional[str] = None
    created_at: Optional[str] = None

    def __post_init__(self):
        if not self.summary or not self.summary.strip():
            raise ValueError("MeetingNote summary cannot be empty")


@dataclass
class KnowledgeGap:
    """A known unknown that needs resolution."""

    question: str
    id: Optional[int] = None
    topic: Optional[str] = None
    status: GapStatus = GapStatus.OPEN
    priority: Priority = Priority.MEDIUM
    owner_username: Optional[str] = None
    resolution_path: Optional[str] = None
    interview_prompts: List[str] = field(default_factory=list)
    resolution: Optional[str] = None
    times_asked: int = 1
    created_at: Optional[str] = None

    def __post_init__(self):
        if not self.question or not self.question.strip():
            raise ValueError("KnowledgeGap question cannot be empty")
        if isinstance(self.status, str):
            self.status = GapStatus(self.status)
        if isinstance(self.priority, str):
            self.priority = Priority(self.priority)


@dataclass
class SourceLink:
    """Provenance link tying an artifact to its origin."""

    record_type: str          # "action_item", "decision", "lead", …
    record_id: int
    source_type: str          # "discord_message", "document", "meeting", …
    source_id: str            # message ID, doc path, etc.
    timestamp: Optional[str] = None
    id: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not self.record_type:
            raise ValueError("SourceLink must have a record_type")
        if not self.source_type:
            raise ValueError("SourceLink must have a source_type")


# ── Continuity Entities ───────────────────────────────────────

class ObligationFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUALLY = "annually"
    CUSTOM = "custom"


class ObligationStatus(str, Enum):
    ACTIVE = "active"
    UPCOMING = "upcoming"
    OVERDUE = "overdue"
    COMPLETED = "completed"
    SUSPENDED = "suspended"


class RailType(str, Enum):
    VALIDATE = "validate"
    LAUNCH = "launch"
    OPERATE = "operate"


class RailStageStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    SKIPPED = "skipped"


@dataclass
class Obligation:
    """A recurring requirement with deadline tracking and evidence."""

    title: str
    frequency: ObligationFrequency = ObligationFrequency.MONTHLY
    id: Optional[int] = None
    description: Optional[str] = None
    owner_username: Optional[str] = None
    next_due: Optional[str] = None          # ISO 8601
    last_completed: Optional[str] = None
    status: ObligationStatus = ObligationStatus.ACTIVE
    checklist: List[str] = field(default_factory=list)
    evidence_links: List[str] = field(default_factory=list)  # SourceLink IDs or paths
    category: Optional[str] = None          # compliance, financial, operational, legal
    notes: Optional[str] = None
    created_at: Optional[str] = None

    def __post_init__(self):
        if not self.title or not self.title.strip():
            raise ValueError("Obligation title cannot be empty")
        if isinstance(self.frequency, str):
            self.frequency = ObligationFrequency(self.frequency)
        if isinstance(self.status, str):
            self.status = ObligationStatus(self.status)

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["frequency"] = self.frequency.value
        d["status"] = self.status.value
        d["checklist"] = json.dumps(self.checklist) if self.checklist else None
        d["evidence_links"] = json.dumps(self.evidence_links) if self.evidence_links else None
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class SOP:
    """A versioned standard operating procedure with exercise tracking."""

    title: str
    id: Optional[int] = None
    version: int = 1
    owner_username: Optional[str] = None
    body: Optional[str] = None              # Markdown runbook content
    checklist: List[str] = field(default_factory=list)
    last_exercised: Optional[str] = None    # ISO 8601
    last_reviewed: Optional[str] = None
    linked_decisions: List[int] = field(default_factory=list)
    linked_incidents: List[str] = field(default_factory=list)
    category: Optional[str] = None          # onboarding, operations, compliance, emergency
    status: str = "active"                  # active, draft, deprecated
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self):
        if not self.title or not self.title.strip():
            raise ValueError("SOP title cannot be empty")

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["checklist"] = json.dumps(self.checklist) if self.checklist else None
        d["linked_decisions"] = json.dumps(self.linked_decisions) if self.linked_decisions else None
        d["linked_incidents"] = json.dumps(self.linked_incidents) if self.linked_incidents else None
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Feedback:
    """Structured product feedback with environment snapshot."""

    summary: str
    id: Optional[int] = None
    category: Optional[str] = None          # bug, feature, ux, performance, other
    severity: Optional[str] = None          # low, medium, high, critical
    context: Optional[str] = None           # what the user was doing
    environment_snapshot: Optional[Dict[str, Any]] = None  # OS, python version, config hash
    submitted_by: Optional[str] = None
    status: str = "new"                     # new, triaged, resolved, wont_fix
    resolution: Optional[str] = None
    created_at: Optional[str] = None

    def __post_init__(self):
        if not self.summary or not self.summary.strip():
            raise ValueError("Feedback summary cannot be empty")

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["environment_snapshot"] = (
            json.dumps(self.environment_snapshot) if self.environment_snapshot else None
        )
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Rail:
    """A venture lifecycle track (Validate / Launch / Operate)."""

    name: str
    rail_type: RailType
    id: Optional[int] = None
    description: Optional[str] = None
    current_stage_id: Optional[int] = None
    status: str = "active"                  # active, paused, completed, archived
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Rail name cannot be empty")
        if isinstance(self.rail_type, str):
            self.rail_type = RailType(self.rail_type)


@dataclass
class RailStage:
    """A stage within a rail with required outputs and escalation rules."""

    rail_id: int
    name: str
    position: int                           # order within rail
    id: Optional[int] = None
    description: Optional[str] = None
    required_outputs: List[str] = field(default_factory=list)  # what must exist to advance
    actual_outputs: List[str] = field(default_factory=list)    # artifact refs produced
    status: RailStageStatus = RailStageStatus.NOT_STARTED
    entered_at: Optional[str] = None
    completed_at: Optional[str] = None
    escalation_days: int = 7               # days before escalation if incomplete
    notes: Optional[str] = None

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("RailStage name cannot be empty")
        if isinstance(self.status, str):
            self.status = RailStageStatus(self.status)

    def to_db_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["required_outputs"] = json.dumps(self.required_outputs) if self.required_outputs else None
        d["actual_outputs"] = json.dumps(self.actual_outputs) if self.actual_outputs else None
        return {k: v for k, v in d.items() if v is not None}
