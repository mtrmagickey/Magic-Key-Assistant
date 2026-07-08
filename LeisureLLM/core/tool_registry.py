"""
Tool Registry — Protocol-based tool system for agentic operations.

This is MKA's harness layer: a bounded set of tools the LLM can invoke
through the chat interface or autonomous workflows.  Each tool maps to
an existing service capability (artifact CRUD, knowledge search, web
research) and is gated by workflows.yaml configuration.

Design principles:
    - Tools are the *only* way the LLM mutates state (artifact contract)
    - Every tool execution is logged for auditability
    - Tool availability is config-driven (disabled tools are invisible to the LLM)
    - Tools produce structured results, not prose
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# TOOL PROTOCOL
# =============================================================================


class ToolCategory(str, Enum):
    """Broad categories for grouping and gating tools."""
    ARTIFACTS = "artifacts"      # create/update actions, decisions, leads, meetings
    KNOWLEDGE = "knowledge"      # search KB, create gaps
    PIPELINE = "pipeline"        # lead stage changes, follow-ups
    SYSTEM = "system"            # health checks, info queries


@dataclass(frozen=True)
class ToolParameter:
    """Schema for a single tool parameter."""
    name: str
    type: str           # "string", "integer", "number", "boolean"
    description: str
    required: bool = True
    enum: Optional[List[str]] = None
    default: Any = None


@dataclass(frozen=True)
class ToolResult:
    """Structured result from tool execution."""
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    artifact_refs: List[str] = field(default_factory=list)  # e.g. ["[action#42]"]

    def to_llm_context(self) -> str:
        """Format result for inclusion in LLM context."""
        parts = []
        if self.success:
            parts.append(f"✓ {self.message}" if self.message else "✓ Success")
        else:
            parts.append(f"✗ {self.message}" if self.message else "✗ Failed")
        if self.artifact_refs:
            parts.append(f"Records: {', '.join(self.artifact_refs)}")
        if self.data:
            # Include key data points without overwhelming context
            for k, v in self.data.items():
                if isinstance(v, (str, int, float, bool)) or isinstance(v, list) and len(v) <= 5:
                    parts.append(f"  {k}: {v}")
        return "\n".join(parts)


# Type alias for async tool executor functions
ToolExecutor = Callable[..., Coroutine[Any, Any, ToolResult]]


@dataclass
class Tool:
    """
    A single callable tool in the registry.

    Each tool wraps an existing service capability with:
    - A schema the LLM can reason about (name, description, parameters)
    - An async executor that performs the actual work
    - Category and config gating for availability control
    """
    name: str
    description: str
    category: ToolCategory
    parameters: List[ToolParameter]
    executor: ToolExecutor

    # Config key in workflows.yaml that must be enabled for this tool
    # e.g. "work.action_items.enabled" — if falsy, tool is hidden from LLM
    config_gate: Optional[str] = None

    # Whether this tool mutates state (shown in confirmation gate)
    mutates: bool = False

    def to_openai_schema(self) -> Dict[str, Any]:
        """Export as OpenAI function-calling tool schema."""
        properties = {}
        required = []
        for p in self.parameters:
            prop: Dict[str, Any] = {
                "type": p.type,
                "description": p.description,
            }
            if p.enum:
                prop["enum"] = p.enum
            if p.default is not None:
                prop["default"] = p.default
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


# =============================================================================
# TOOL EXECUTION LOG
# =============================================================================


@dataclass
class ToolExecution:
    """Audit record for a single tool invocation."""
    tool_name: str
    arguments: Dict[str, Any]
    result: ToolResult
    executed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source: str = "chat"  # "chat", "autonomous", "workflow"
    confirmed_by_user: bool = False


# =============================================================================
# REGISTRY
# =============================================================================


class ToolRegistry:
    """
    Central registry of all available tools.

    The registry is the harness: it determines what the LLM can do.
    Tools are registered at startup and filtered by config at query time.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._execution_log: List[ToolExecution] = []

    def register(self, tool: Tool) -> None:
        """Register a tool. Overwrites if name exists."""
        self._tools[tool.name] = tool
        logger.info("Tool registered: %s [%s] mutates=%s", tool.name, tool.category.value, tool.mutates)

    def unregister(self, name: str) -> bool:
        """Remove a tool. Returns True if it existed."""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(
        self,
        *,
        category: Optional[ToolCategory] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> List[Tool]:
        """
        List available tools, optionally filtered by category and config.

        Parameters
        ----------
        category : optional
            Only return tools in this category.
        config : optional
            Flattened workflows config dict.  Tools whose config_gate
            resolves to a falsy value are excluded.
        """
        tools = list(self._tools.values())

        if category:
            tools = [t for t in tools if t.category == category]

        if config is not None:
            tools = [t for t in tools if self._check_gate(t, config)]

        return tools

    def get_openai_tools_schema(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Get OpenAI-format tool schemas for all available tools."""
        return [t.to_openai_schema() for t in self.list_tools(config=config)]

    @staticmethod
    def _validate_arguments(tool: Tool, arguments: Dict[str, Any]) -> Optional[str]:
        """Validate tool arguments against the declared parameter schema.

        Returns an error message string if validation fails, None if OK.
        """
        schema = {p.name: p for p in tool.parameters}
        allowed_names = set(schema.keys())

        # ── Reject unknown / reserved argument names ──
        # "db" is injected by the registry — never allow the caller to supply it.
        _RESERVED = {"db", "self", "cls", "kwargs", "args"}
        for key in arguments:
            if key in _RESERVED:
                return f"Reserved argument name '{key}' is not allowed"
            if key not in allowed_names:
                return f"Unknown argument '{key}' for tool '{tool.name}'"

        # ── Check required parameters are present ──
        for pname, param in schema.items():
            if param.required and pname not in arguments:
                return f"Missing required argument '{pname}' for tool '{tool.name}'"

        # ── Type and enum checks ──
        _TYPE_MAP = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
        }
        for key, value in arguments.items():
            param = schema.get(key)
            if param is None:
                continue  # already caught above

            expected = _TYPE_MAP.get(param.type)
            if expected and not isinstance(value, expected):
                # Allow int where number is expected
                return (
                    f"Argument '{key}' must be {param.type}, "
                    f"got {type(value).__name__}"
                )

            if param.enum and value not in param.enum:
                return (
                    f"Argument '{key}' must be one of {param.enum}, "
                    f"got '{value}'"
                )

        return None  # all good

    async def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        source: str = "chat",
        confirmed: bool = False,
        db: Any = None,
    ) -> ToolResult:
        """
        Execute a tool by name with the given arguments.

        Parameters
        ----------
        name : str
            Tool name (must be registered).
        arguments : dict
            Arguments matching the tool's parameter schema.
        source : str
            Where this execution was triggered from.
        confirmed : bool
            Whether the user confirmed this action (for mutating tools).
        db : optional
            Database instance to pass to the executor.
        """
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, message=f"Unknown tool: {name}")

        # ── Schema validation (before confirmation gate so bad calls fail fast) ──
        validation_error = self._validate_arguments(tool, arguments)
        if validation_error:
            logger.warning("Tool %s argument validation failed: %s", name, validation_error)
            return ToolResult(success=False, message=validation_error)

        # Mutating tools always require a gate check.
        # Interactive (chat): needs explicit user confirmation.
        # Autonomous: allowed through but logged for auditability.
        if tool.mutates and not confirmed:
            if source == "chat":
                return ToolResult(
                    success=False,
                    message="CONFIRMATION_REQUIRED",
                    data={
                        "tool": name,
                        "arguments": arguments,
                        "description": tool.description,
                    },
                )
            # Autonomous sources proceed but get an audit trail
            logger.info(
                "Autonomous mutation: tool=%s source=%s args=%s",
                name, source, json.dumps(arguments, default=str),
            )

        try:
            # Inject db if the executor expects it
            if db is not None:
                result = await tool.executor(db=db, **arguments)
            else:
                result = await tool.executor(**arguments)
        except Exception as exc:
            logger.error("Tool %s execution failed: %s", name, exc, exc_info=True)
            result = ToolResult(success=False, message=f"Execution error: {exc}")

        # Log the execution
        execution = ToolExecution(
            tool_name=name,
            arguments=arguments,
            result=result,
            source=source,
            confirmed_by_user=confirmed,
        )
        self._execution_log.append(execution)

        # Persist to DB if available
        if db is not None:
            await self._persist_execution(db, execution)

        return result

    async def _persist_execution(self, db: Any, execution: ToolExecution) -> None:
        """Write execution record to the tool_executions table."""
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO tool_executions
                       (tool_name, arguments, success, message, artifact_refs,
                        source, confirmed_by_user, executed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        execution.tool_name,
                        json.dumps(execution.arguments, default=str),
                        execution.result.success,
                        execution.result.message,
                        json.dumps(execution.result.artifact_refs),
                        execution.source,
                        execution.confirmed_by_user,
                        execution.executed_at,
                    ),
                )
                await conn.commit()
        except Exception as exc:
            logger.warning("Failed to persist tool execution: %s", exc)

    @staticmethod
    def _check_gate(tool: Tool, config: Dict[str, Any]) -> bool:
        """Check whether a tool's config gate is satisfied."""
        if not tool.config_gate:
            return True  # No gate = always available

        # Walk dotted path: "work.action_items.enabled" → config["work"]["action_items"]["enabled"]
        parts = tool.config_gate.split(".")
        node: Any = config
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return False  # Path doesn't exist → treat as disabled
        return bool(node)

    def get_recent_executions(self, limit: int = 20) -> List[ToolExecution]:
        """Get recent in-memory execution log entries."""
        return self._execution_log[-limit:]

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def summary(self) -> Dict[str, Any]:
        """Summary for health/status display."""
        by_category: Dict[str, int] = {}
        mutating = 0
        for t in self._tools.values():
            by_category[t.category.value] = by_category.get(t.category.value, 0) + 1
            if t.mutates:
                mutating += 1
        return {
            "total": self.tool_count,
            "by_category": by_category,
            "mutating": mutating,
            "recent_executions": len(self._execution_log),
        }
