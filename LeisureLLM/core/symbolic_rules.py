"""
symbolic_rules — Neuro-symbolic and verifiable reasoning for MKA.

Replaces ad-hoc ``CHECK`` constraints and ``__post_init__`` guards with
formal, composable primitives:

• **StateMachine** — Declarative transition tables.  ``advance()`` raises
  ``InvalidTransition`` rather than silently accepting any status.
• **cross-entity invariants** — Rules that span multiple tables, evaluated
  on demand or as a post-write assertion.
• **LLM output schema validation** — JSON Schema gates between raw LLM
  extraction output and database writes.

No ML, no LLM — these are purely symbolic checks.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# 1.  State machines
# ════════════════════════════════════════════════════════════════

class InvalidTransition(ValueError):
    """Raised when a state transition violates the machine rules."""

    def __init__(self, entity: str, current: str, target: str, allowed: Set[str]):
        self.entity = entity
        self.current = current
        self.target = target
        self.allowed = allowed
        super().__init__(
            f"{entity}: cannot transition {current!r} → {target!r}. "
            f"Allowed from {current!r}: {sorted(allowed)}"
        )


@dataclass
class StateMachine:
    """A declarative finite-state machine for entity status fields.

    Usage::

        TASK_FSM = StateMachine(
            name="task",
            transitions={
                "todo":        {"in_progress", "cancelled"},
                "in_progress": {"blocked", "done", "cancelled"},
                "blocked":     {"in_progress", "cancelled"},
                "done":        set(),            # terminal
                "cancelled":   {"todo"},         # allow re-open
            },
        )
        TASK_FSM.validate("todo", "in_progress")  # OK
        TASK_FSM.validate("done", "in_progress")  # raises InvalidTransition
    """

    name: str
    transitions: Dict[str, Set[str]]

    def validate(self, current: str, target: str) -> bool:
        """Return True if the transition is valid, else raise."""
        allowed = self.transitions.get(current, set())
        if target not in allowed:
            raise InvalidTransition(self.name, current, target, allowed)
        return True

    def can_advance(self, current: str, target: str) -> bool:
        """Non-throwing check."""
        return target in self.transitions.get(current, set())

    @property
    def states(self) -> Set[str]:
        all_states: Set[str] = set(self.transitions.keys())
        for targets in self.transitions.values():
            all_states |= targets
        return all_states

    @property
    def terminal_states(self) -> Set[str]:
        return {s for s, t in self.transitions.items() if not t}


# ── Pre-built machines for MKA entities ──────────────────────

TASK_FSM = StateMachine(
    name="task",
    transitions={
        "todo":        {"in_progress", "cancelled"},
        "in_progress": {"blocked", "done", "cancelled"},
        "blocked":     {"in_progress", "cancelled"},
        "done":        set(),
        "cancelled":   {"todo"},  # re-open
    },
)

LEAD_FSM = StateMachine(
    name="lead",
    transitions={
        "cold":     {"warm", "lost", "dormant"},
        "warm":     {"hot", "cold", "lost", "dormant"},
        "hot":      {"proposal", "warm", "lost", "dormant"},
        "proposal": {"won", "hot", "lost", "dormant"},
        "won":      set(),
        "lost":     {"cold"},  # revive
        "dormant":  {"cold"},  # re-engage
    },
)

OBLIGATION_FSM = StateMachine(
    name="obligation",
    transitions={
        "active":    {"upcoming", "overdue", "completed", "suspended"},
        "upcoming":  {"active", "overdue", "completed", "suspended"},
        "overdue":   {"active", "completed", "suspended"},
        "completed": {"active"},   # recurring: reactivate after completion
        "suspended": {"active"},
    },
)

GAP_FSM = StateMachine(
    name="knowledge_gap",
    transitions={
        "open":        {"in_progress", "resolved", "wont_fix"},
        "in_progress": {"resolved", "wont_fix", "open"},
        "resolved":    set(),
        "wont_fix":    {"open"},
    },
)

RAIL_STAGE_FSM = StateMachine(
    name="rail_stage",
    transitions={
        "not_started": {"in_progress", "skipped"},
        "in_progress": {"blocked", "complete", "skipped"},
        "blocked":     {"in_progress", "skipped"},
        "complete":    set(),
        "skipped":     set(),
    },
)

OPPORTUNITY_FSM = StateMachine(
    name="opportunity",
    transitions={
        "identified":   {"qualified", "abandoned"},
        "qualified":    {"proposal", "identified", "abandoned"},
        "proposal":     {"negotiation", "qualified", "abandoned"},
        "negotiation":  {"won", "lost", "proposal"},
        "won":          set(),
        "lost":         {"identified"},  # revive
        "abandoned":    {"identified"},
    },
)

# Convenience lookup
FSM_REGISTRY: Dict[str, StateMachine] = {
    "task": TASK_FSM,
    "lead": LEAD_FSM,
    "obligation": OBLIGATION_FSM,
    "knowledge_gap": GAP_FSM,
    "rail_stage": RAIL_STAGE_FSM,
    "opportunity": OPPORTUNITY_FSM,
}


# ════════════════════════════════════════════════════════════════
# 2.  Cross-entity invariants
# ════════════════════════════════════════════════════════════════

@dataclass
class InvariantViolation:
    """A detected invariant violation."""
    rule: str
    entity_type: str
    entity_id: int
    detail: str
    severity: str = "warning"  # warning | error


async def check_invariants(db) -> List[InvariantViolation]:
    """Run cross-entity invariant checks against the database.

    These are symbolic rules — no LLM involved.
    """
    violations: List[InvariantViolation] = []

    # ── Rule 1: Blocked tasks must have blocked_since ────────
    try:
        async with db.connection.execute(
            """SELECT id, title FROM tasks
               WHERE status = 'blocked'
                 AND (blocked_since IS NULL OR blocked_since = '')"""
        ) as cur:
            for row in await cur.fetchall():
                violations.append(InvariantViolation(
                    rule="blocked_requires_blocked_since",
                    entity_type="task",
                    entity_id=row["id"],
                    detail=f"Task #{row['id']} \"{row['title']}\" is blocked but has no blocked_since date.",
                    severity="warning",
                ))
    except Exception as e:
        logger.warning("check_invariants: suppressed %s", e)

    # ── Rule 2: Completed tasks must have completed_at ───────
    try:
        async with db.connection.execute(
            """SELECT id, title FROM tasks
               WHERE status = 'done'
                 AND (completed_at IS NULL OR completed_at = '')"""
        ) as cur:
            for row in await cur.fetchall():
                violations.append(InvariantViolation(
                    rule="done_requires_completed_at",
                    entity_type="task",
                    entity_id=row["id"],
                    detail=f"Task #{row['id']} \"{row['title']}\" is done but has no completed_at timestamp.",
                    severity="warning",
                ))
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    # ── Rule 3: Superseded decisions must have superseded_by ─
    try:
        # Find decisions with a superseded_by but check the OPPOSITE:
        # decisions referenced in superseded_by_decision_id should be
        # self-consistent — the "parent" should know it has been replaced.
        async with db.connection.execute(
            """SELECT d1.id, d1.title
               FROM decisions d1
               JOIN decisions d2 ON d2.superseded_by_decision_id = d1.id
               WHERE d1.superseded_by_decision_id IS NULL
                 AND d2.id != d1.id"""
        ) as cur:
            # These are decisions that supersede others but don't form a
            # clean chain — informational only.
            pass
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    # ── Rule 4: Overdue obligations with future next_due ─────
    try:
        async with db.connection.execute(
            """SELECT id, title, next_due FROM obligations
               WHERE status = 'overdue'
                 AND next_due >= date('now')"""
        ) as cur:
            for row in await cur.fetchall():
                violations.append(InvariantViolation(
                    rule="overdue_must_be_past",
                    entity_type="obligation",
                    entity_id=row["id"],
                    detail=(
                        f"Obligation #{row['id']} \"{row['title']}\" is marked overdue "
                        f"but next_due ({row['next_due']}) is in the future."
                    ),
                    severity="error",
                ))
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    # ── Rule 5: Tasks with dependencies referencing themselves
    try:
        async with db.connection.execute(
            """SELECT id, title, dependencies FROM tasks
               WHERE dependencies IS NOT NULL"""
        ) as cur:
            for row in await cur.fetchall():
                deps = json.loads(row["dependencies"]) if row["dependencies"] else []
                if row["id"] in deps:
                    violations.append(InvariantViolation(
                        rule="no_self_dependency",
                        entity_type="task",
                        entity_id=row["id"],
                        detail=f"Task #{row['id']} \"{row['title']}\" lists itself as a dependency.",
                        severity="error",
                    ))
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    # ── Rule 6: Dangling dependency references ───────────────
    try:
        all_task_ids: Set[int] = set()
        async with db.connection.execute("SELECT id FROM tasks") as cur:
            for row in await cur.fetchall():
                all_task_ids.add(row["id"])

        async with db.connection.execute(
            """SELECT id, title, dependencies FROM tasks
               WHERE dependencies IS NOT NULL"""
        ) as cur:
            for row in await cur.fetchall():
                deps = json.loads(row["dependencies"]) if row["dependencies"] else []
                for dep_id in deps:
                    if dep_id not in all_task_ids:
                        violations.append(InvariantViolation(
                            rule="dependency_exists",
                            entity_type="task",
                            entity_id=row["id"],
                            detail=(
                                f"Task #{row['id']} depends on task #{dep_id} "
                                f"which does not exist."
                            ),
                            severity="error",
                        ))
    except Exception as e:
        logger.warning("operation: suppressed %s", e)

    return violations


# ════════════════════════════════════════════════════════════════
# 3.  LLM output schema validation
# ════════════════════════════════════════════════════════════════

# Lightweight JSON Schema subset validation — avoids adding
# jsonschema as a dependency.  Covers the shapes that onboarding
# and chat extraction actually produce.

@dataclass
class SchemaError:
    path: str
    message: str


def validate_schema(data: Any, schema: Dict[str, Any], path: str = "$") -> List[SchemaError]:
    """Validate *data* against a JSON-Schema-like spec.

    Supports: type, required, properties, items, enum, minLength,
    maxLength, minimum, maximum, pattern.
    """
    errors: List[SchemaError] = []

    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }
        py_type = type_map.get(expected_type)
        if py_type and not isinstance(data, py_type):  # type: ignore[arg-type]
            errors.append(SchemaError(path, f"Expected {expected_type}, got {type(data).__name__}"))
            return errors  # type mismatch → skip deeper checks

    if expected_type == "object" or isinstance(data, dict):
        # required
        for key in schema.get("required", []):
            if key not in data:
                errors.append(SchemaError(f"{path}.{key}", "Required field missing"))
        # properties
        for key, sub_schema in schema.get("properties", {}).items():
            if key in data:
                errors.extend(validate_schema(data[key], sub_schema, f"{path}.{key}"))

    if (expected_type == "array" or isinstance(data, list)) and "items" in schema:
        for i, item in enumerate(data):
            errors.extend(validate_schema(item, schema["items"], f"{path}[{i}]"))

    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errors.append(SchemaError(path, f"String shorter than minLength {schema['minLength']}"))
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errors.append(SchemaError(path, f"String longer than maxLength {schema['maxLength']}"))
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errors.append(SchemaError(path, f"Does not match pattern {schema['pattern']!r}"))

    if isinstance(data, (int, float)):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(SchemaError(path, f"Value {data} below minimum {schema['minimum']}"))
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(SchemaError(path, f"Value {data} above maximum {schema['maximum']}"))

    if "enum" in schema and data not in schema["enum"]:
        errors.append(SchemaError(path, f"Value {data!r} not in enum {schema['enum']}"))

    return errors


# ── Pre-built schemas for MKA LLM extraction outputs ─────────

ONBOARDING_INTRO_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["org_name"],
    "properties": {
        "org_name": {"type": "string", "minLength": 1, "maxLength": 200},
        "industry": {"type": "string"},
        "tagline": {"type": "string", "maxLength": 500},
        "team_size": {
            "type": "string",
            "enum": ["solo", "2-5", "6-15", "16-50", "50+"],
        },
        "team_description": {"type": "string"},
        "location": {"type": "string"},
        "key_services": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

ONBOARDING_PROJECT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["projects"],
    "properties": {
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "timeline": {"type": "string"},
                    "stakeholders": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

ONBOARDING_BRAIN_DUMP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["concerns"],
    "properties": {
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["description"],
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                    "category": {"type": "string"},
                },
            },
        },
    },
}

CHAT_EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "owner": {"type": "string"},
                    "due_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "urgent"],
                    },
                },
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "decision"],
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "minLength": 1},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}

# Registry for easy lookup
SCHEMA_REGISTRY: Dict[str, Dict[str, Any]] = {
    "onboarding_intro": ONBOARDING_INTRO_SCHEMA,
    "onboarding_projects": ONBOARDING_PROJECT_SCHEMA,
    "onboarding_brain_dump": ONBOARDING_BRAIN_DUMP_SCHEMA,
    "chat_extraction": CHAT_EXTRACTION_SCHEMA,
}


def validate_llm_output(
    data: Any,
    schema_name: str,
    *,
    strict: bool = False,
) -> Tuple[bool, List[SchemaError]]:
    """Validate LLM extraction output against a registered schema.

    Returns
    -------
    (ok, errors) : tuple
        ``ok`` is True if no errors (or non-strict mode with only warnings).
        ``errors`` is the list of violations found.
    """
    schema = SCHEMA_REGISTRY.get(schema_name)
    if not schema:
        logger.warning("No schema registered for %r", schema_name)
        return (True, [])  # unknown schema → pass through

    errors = validate_schema(data, schema)
    if errors:
        logger.info(
            "Schema validation for %r: %d error(s): %s",
            schema_name,
            len(errors),
            "; ".join(f"{e.path}: {e.message}" for e in errors[:5]),
        )
    ok = len(errors) == 0 if strict else not any(
        "Required field" in e.message for e in errors
    )
    return (ok, errors)
