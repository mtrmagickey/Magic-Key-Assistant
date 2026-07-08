"""
Data models for database entities.

Using dataclasses for lightweight validation and type hints.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional


class TaskStatus(str, Enum):
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


class GapStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


class OpportunityStatus(str, Enum):
    PROSPECT = "prospect"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    NEGOTIATING = "negotiating"
    WON = "won"
    LOST = "lost"
    ON_HOLD = "on_hold"


@dataclass
class Task:
    """Action item / task entity."""
    title: str
    id: Optional[int] = None
    project_id: Optional[int] = None
    description: Optional[str] = None
    status: TaskStatus = TaskStatus.TODO
    priority: Priority = Priority.MEDIUM
    assigned_to_user_id: Optional[int] = None
    assigned_to_username: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_username: Optional[str] = None
    due_date: Optional[str] = None
    completed_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    notes: Optional[str] = None
    
    def __post_init__(self):
        # Validate title
        if not self.title or not self.title.strip():
            raise ValueError("Task title cannot be empty")
        self.title = self.title.strip()[:500]  # Limit length
        
        # Normalize enums
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)
        if isinstance(self.priority, str):
            self.priority = Priority(self.priority)
    
    def to_db_dict(self) -> dict:
        """Convert to dict for database insertion."""
        data = asdict(self)
        data['status'] = self.status.value
        data['priority'] = self.priority.value
        data['tags'] = ','.join(self.tags) if self.tags else None
        return {k: v for k, v in data.items() if v is not None}


@dataclass
class KnowledgeGap:
    """Knowledge gap entity."""
    topic: str
    id: Optional[int] = None
    question: Optional[str] = None
    context: Optional[str] = None
    status: GapStatus = GapStatus.OPEN
    priority: Priority = Priority.MEDIUM
    source: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    resolution: Optional[str] = None
    resolved_by_user_id: Optional[int] = None
    resolved_by_username: Optional[str] = None
    
    def __post_init__(self):
        if not self.topic or not self.topic.strip():
            raise ValueError("Knowledge gap topic cannot be empty")
        self.topic = self.topic.strip()[:500]
        
        if isinstance(self.status, str):
            self.status = GapStatus(self.status)
        if isinstance(self.priority, str):
            self.priority = Priority(self.priority)
    
    def to_db_dict(self) -> dict:
        data = asdict(self)
        data['status'] = self.status.value
        data['priority'] = self.priority.value
        data['tags'] = ','.join(self.tags) if self.tags else None
        return {k: v for k, v in data.items() if v is not None}


@dataclass
class Opportunity:
    """Sales opportunity / lead entity."""
    name: str
    id: Optional[int] = None
    company: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    status: OpportunityStatus = OpportunityStatus.PROSPECT
    value_usd: Optional[int] = None
    source: Optional[str] = None
    next_action: Optional[str] = None
    next_action_date: Optional[str] = None
    notes: Optional[str] = None
    
    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Opportunity name cannot be empty")
        self.name = self.name.strip()[:500]
        
        if isinstance(self.status, str):
            self.status = OpportunityStatus(self.status)
        
        # Basic email validation
        if self.contact_email:
            self.contact_email = self.contact_email.strip().lower()
            if '@' not in self.contact_email:
                raise ValueError("Invalid email format")
    
    def to_db_dict(self) -> dict:
        data = asdict(self)
        data['status'] = self.status.value
        return {k: v for k, v in data.items() if v is not None}
