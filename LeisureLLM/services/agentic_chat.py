"""
Agentic chat pipeline — tool-calling loop for the web chat interface.

This module transforms the chat from a read-only RAG retrieval interface
into an operational command surface.  The LLM can invoke bounded tools
from the tool registry (create actions, advance leads, record decisions)
while the user retains confirmation control over all mutations.

The tool-calling protocol is prompt-based (not OpenAI function-calling)
so it works identically across all backends: Ollama, OpenAI, Anthropic,
OpenRouter, and custom endpoints.

Flow:
    1. User message → RAG retrieval → build context
    2. System prompt includes tool schemas + instructions
    3. LLM responds with text OR a <tool_call> block
    4. If tool call:
       a. Mutating tool → emit CONFIRMATION_REQUIRED event to frontend
       b. Read-only tool → execute, feed result back, LLM continues
    5. MULTI-STEP: Repeat steps 3-4 for up to MAX_TOOL_STEPS iterations
       (plan → execute → observe → replan)
    6. Stream final response to user
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.tool_registry import ToolRegistry, ToolResult

logger = logging.getLogger("AdminServer.agentic_chat")

# Pattern to extract tool calls from LLM output
_TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)

# ── Multi-step planning configuration ────────────────────────────────────────

MAX_TOOL_STEPS = 5      # Safety cap — max tool calls in a single user turn
MAX_OBSERVATION_LEN = 1500  # Truncate tool results fed back to the LLM


@dataclass
class ToolStep:
    """Record of a single step in a multi-step tool chain."""
    tool_name: str
    arguments: Dict[str, Any]
    result: ToolResult
    observation: str  # LLM-friendly summary of result


@dataclass
class PlanningState:
    """Tracks the state of a multi-step tool execution chain."""
    steps: List[ToolStep] = field(default_factory=list)
    pending_confirmation: Optional[Dict[str, Any]] = None
    done: bool = False

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def tools_invoked(self) -> List[str]:
        return [s.tool_name for s in self.steps]

    @property
    def artifact_refs(self) -> List[str]:
        refs: List[str] = []
        for s in self.steps:
            refs.extend(s.result.artifact_refs or [])
        return refs

    def build_observation_history(self) -> str:
        """Build a log of all prior steps for the LLM's working memory."""
        if not self.steps:
            return ""
        lines = ["## Tool Execution History"]
        for i, step in enumerate(self.steps, 1):
            lines.append(
                f"### Step {i}: {step.tool_name}\n"
                f"Arguments: {json.dumps(step.arguments, default=str)}\n"
                f"Result: {step.observation}"
            )
        lines.append(
            "\nBased on the results above, decide: "
            "do you need another tool call, or can you answer the user now? "
            "If done, respond naturally. If not, make the next tool call."
        )
        return "\n".join(lines)

# Pattern to extract tool calls from LLM output
_TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


def build_tool_system_prompt(
    base_system_prompt: str,
    registry: ToolRegistry,
    workflows_config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Augment the system prompt with tool descriptions and calling instructions.

    Parameters
    ----------
    base_system_prompt : str
        The existing RAG system prompt.
    registry : ToolRegistry
        The tool registry (filtered by config at query time).
    workflows_config : optional dict
        Flattened workflows.yaml config for gating tools.
    """
    tools = registry.list_tools(config=workflows_config)
    if not tools:
        return base_system_prompt

    tool_descriptions = []
    for tool in tools:
        params_desc = []
        for p in tool.parameters:
            req = " (required)" if p.required else " (optional)"
            enum_note = f" — one of: {', '.join(p.enum)}" if p.enum else ""
            params_desc.append(f"    - {p.name} ({p.type}{req}): {p.description}{enum_note}")
        params_block = "\n".join(params_desc) if params_desc else "    (no parameters)"
        mutates_note = " [MUTATING — requires user confirmation]" if tool.mutates else ""
        tool_descriptions.append(
            f"  {tool.name}: {tool.description}{mutates_note}\n"
            f"  Parameters:\n{params_block}"
        )

    tools_section = "\n\n".join(tool_descriptions)

    tool_prompt = f"""

## Available Tools

You have access to the following tools to take actions on the user's behalf.
When the user asks you to DO something (create a task, record a decision,
advance a lead, check overdue items, etc.), use the appropriate tool.

When you want to use a tool, output EXACTLY this format — nothing else before
or after the tag:

<tool_call>
{{"name": "tool_name", "arguments": {{"param1": "value1", "param2": "value2"}}}}
</tool_call>

RULES:
- Use tools when the user's intent is to take an action OR when you need information the retrieved context doesn't cover.
- If the user asks you to create a durable artifact, persist it with a tool instead of stopping at prose alone.
- Use create_action for action items, create_decision for decision records, create_lead for opportunities, and create_document for memos, briefs, reports, outlines, proposals, or plans.
- For questions well-answered by the retrieved context, answer directly.
- If the context is weak, missing, or the question is about external/current topics (industry standards, regulations, pricing, competitors, how-to guidance), USE the search_web tool proactively — don't guess or give a thin answer when you could search.
- Only call ONE tool at a time. Wait for the result before calling the next.
- Use exact parameter names from the tool definitions.
- If a tool requires an ID you don't have, ask the user or use a list tool first.
- After a tool executes, you will receive the result. You may then:
  a) Call ANOTHER tool if needed to complete the task (e.g. list → then create)
  b) Respond naturally to the user, summarizing what was done
- For complex requests, THINK step by step: what information do you need first,
  then what action to take. Use read-only tools to gather data before mutating.
- You may chain up to {MAX_TOOL_STEPS} tool calls per request.

## Tool Definitions

{tools_section}
"""

    return base_system_prompt + tool_prompt


def extract_tool_call(llm_output: str) -> Optional[Dict[str, Any]]:
    """
    Parse a tool call from LLM output.

    Returns None if no tool call found, otherwise a dict with
    'name' and 'arguments' keys.
    """
    match = _TOOL_CALL_PATTERN.search(llm_output)
    if not match:
        return None

    try:
        call = json.loads(match.group(1))
        if "name" in call and "arguments" in call:
            return call
        if "name" in call:
            # Some models omit empty arguments
            call.setdefault("arguments", {})
            return call
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse tool call JSON: %s", exc)
    return None


def strip_tool_call(text: str) -> str:
    """Remove the <tool_call> block from text, returning any surrounding prose."""
    return _TOOL_CALL_PATTERN.sub("", text).strip()


async def execute_tool_call(
    registry: ToolRegistry,
    tool_call: Dict[str, Any],
    *,
    db: Any = None,
    confirmed: bool = False,
) -> ToolResult:
    """
    Execute a parsed tool call through the registry.

    For mutating tools without confirmation, returns a
    CONFIRMATION_REQUIRED result that the frontend should present.
    """
    name = tool_call.get("name", "")
    arguments = tool_call.get("arguments", {})

    # Clean up arguments: remove empty strings (LLM often sends "" for optional params)
    cleaned_args = {}
    for k, v in arguments.items():
        if isinstance(v, str) and v == "":
            continue
        cleaned_args[k] = v

    return await registry.execute(
        name,
        cleaned_args,
        source="chat",
        confirmed=confirmed,
        db=db,
    )


def format_tool_result_for_llm(tool_name: str, result: ToolResult) -> str:
    """Format a tool execution result for inclusion in the next LLM message."""
    return (
        f"[Tool Result: {tool_name}]\n"
        f"{result.to_llm_context()}\n"
        f"[End Tool Result]\n\n"
        f"Now respond to the user naturally, summarizing what was done."
    )


def build_confirmation_event(tool_call: Dict[str, Any], tool_description: str) -> Dict[str, Any]:
    """
    Build a confirmation event payload for the frontend.

    The frontend should display this to the user and send back
    a confirmation request to execute.
    """
    return {
        "tool_name": tool_call["name"],
        "arguments": tool_call["arguments"],
        "description": tool_description,
        "requires_confirmation": True,
    }


# =============================================================================
# MULTI-STEP PLANNING ENGINE
# =============================================================================

# Type alias for the LLM call function used by the planning loop
GenerateFn = Callable[..., Coroutine[Any, Any, str]]


async def run_planning_loop(
    *,
    registry: ToolRegistry,
    generate_fn: GenerateFn,
    initial_response: str,
    system_prompt: str,
    user_prompt: str,
    context: str,
    db: Any = None,
    on_tool_call: Optional[Callable] = None,
    on_tool_result: Optional[Callable] = None,
    on_status: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Multi-step plan→execute→observe→replan loop.

    After the initial LLM response, if it contains a tool call for a
    read-only tool, we execute it, feed the result back, and let the
    LLM decide whether to call another tool or respond.

    Parameters
    ----------
    registry : ToolRegistry
        The tool registry.
    generate_fn : async callable
        Async function(prompt, system_prompt) → str for LLM calls.
    initial_response : str
        The first LLM response (which may contain a tool call).
    system_prompt : str
        System prompt for follow-up LLM calls.
    user_prompt : str
        The original user question.
    context : str
        RAG context string.
    db : optional
        Database instance for tool execution.
    on_tool_call : optional callback
        Called with (tool_name, arguments) when a tool is about to execute.
    on_tool_result : optional callback
        Called with (tool_name, ToolResult) after execution.
    on_status : optional callback
        Called with a status string for progress updates.

    Returns
    -------
    dict with keys:
        - "final_text": str — the final prose response
        - "state": PlanningState — full execution trace
        - "needs_confirmation": bool — True if a mutating tool is pending
        - "confirmation_event": optional dict — confirmation payload
    """
    state = PlanningState()
    current_response = initial_response

    for _step in range(MAX_TOOL_STEPS):
        tool_call = extract_tool_call(current_response)
        if tool_call is None:
            # No more tool calls — LLM is done
            state.done = True
            break

        tool_name = tool_call["name"]
        tool_obj = registry.get(tool_name)

        if not tool_obj:
            # Unknown tool — stop the loop
            state.done = True
            break

        # Mutating tool → stop and ask for confirmation
        if tool_obj.mutates:
            state.pending_confirmation = tool_call
            return {
                "final_text": strip_tool_call(current_response),
                "state": state,
                "needs_confirmation": True,
                "confirmation_event": build_confirmation_event(
                    tool_call, tool_obj.description
                ),
            }

        # Read-only tool → execute immediately
        if on_tool_call:
            on_tool_call(tool_name, tool_call["arguments"])
        if on_status:
            on_status(f"Running {tool_name}…")

        result = await execute_tool_call(
            registry, tool_call, db=db, confirmed=True
        )

        if on_tool_result:
            on_tool_result(tool_name, result)

        obs_full = result.to_llm_context()
        if len(obs_full) > MAX_OBSERVATION_LEN:
            observation = (
                obs_full[:MAX_OBSERVATION_LEN]
                + f" [TRUNCATED: {len(obs_full) - MAX_OBSERVATION_LEN} chars omitted]"
            )
        else:
            observation = obs_full
        state.steps.append(ToolStep(
            tool_name=tool_name,
            arguments=tool_call["arguments"],
            result=result,
            observation=observation,
        ))

        # Feed observation back to LLM for next step
        if on_status:
            on_status("Thinking…")

        ctx_limit = 4000
        ctx_block = context[:ctx_limit]
        if len(context) > ctx_limit:
            ctx_block += f"\n[TRUNCATED: {len(context) - ctx_limit} chars omitted]"

        followup_prompt = (
            f"{state.build_observation_history()}\n\n"
            f"Original user question: {user_prompt}\n\n"
            f"Context:\n{ctx_block}\n\n"
            f"Based on the tool results above and the context, "
            f"either call another tool or respond to the user."
        )

        try:
            current_response = await generate_fn(
                prompt=followup_prompt,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            logger.warning("Planning loop LLM call failed at step %d: %s", _step + 1, exc)
            # Use the last observation as the response
            current_response = f"I executed {tool_name}. {observation}"
            state.done = True
            break

    # If we hit MAX_TOOL_STEPS, force completion
    if not state.done and not state.pending_confirmation:
        logger.info("Planning loop hit max steps (%d)", MAX_TOOL_STEPS)
        state.done = True

    final_text = strip_tool_call(current_response) or current_response
    return {
        "final_text": final_text,
        "state": state,
        "needs_confirmation": False,
        "confirmation_event": None,
    }
