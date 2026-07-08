"""Tests for services.agentic_chat — multi-step planning loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from services.agentic_chat import (
    MAX_TOOL_STEPS,
    PlanningState,
    ToolStep,
    build_confirmation_event,
    extract_tool_call,
    run_planning_loop,
    strip_tool_call,
)

# ── PlanningState ─────────────────────────────────────────────────────────────

class TestPlanningState:
    def test_initial_state(self):
        state = PlanningState()
        assert state.steps == []
        assert state.done is False
        assert state.pending_confirmation is None

    def test_build_observation_history_empty(self):
        state = PlanningState()
        assert state.build_observation_history() == ""

    def test_build_observation_history_with_steps(self):
        result = MagicMock()
        result.to_llm_context.return_value = "some data"
        state = PlanningState()
        state.steps.append(ToolStep(
            tool_name="list_items",
            arguments={"filter": "active"},
            result=result,
            observation="some data",
        ))
        history = state.build_observation_history()
        assert "list_items" in history
        assert "some data" in history


# ── extract_tool_call ─────────────────────────────────────────────────────────

class TestExtractToolCall:
    def test_extracts_valid_tool_call(self):
        text = 'ok <tool_call>{"name": "search", "arguments": {"q": "test"}}</tool_call>'
        result = extract_tool_call(text)
        assert result is not None
        assert result["name"] == "search"
        assert result["arguments"]["q"] == "test"

    def test_returns_none_when_no_tool_call(self):
        assert extract_tool_call("Just a normal response.") is None

    def test_handles_malformed_json(self):
        text = "<tool_call>{not valid json}</tool_call>"
        result = extract_tool_call(text)
        assert result is None

    def test_handles_missing_arguments_key(self):
        text = '<tool_call>{"name": "do_thing"}</tool_call>'
        result = extract_tool_call(text)
        assert result is not None
        assert result["arguments"] == {}


# ── strip_tool_call ───────────────────────────────────────────────────────────

class TestStripToolCall:
    def test_strips_tool_call_block(self):
        text = 'Here is the answer. <tool_call>{"name": "x", "arguments": {}}</tool_call>'
        assert strip_tool_call(text) == "Here is the answer."

    def test_no_tool_call_returns_text(self):
        assert strip_tool_call("just text") == "just text"


# ── build_confirmation_event ──────────────────────────────────────────────────

class TestBuildConfirmationEvent:
    def test_builds_event(self):
        tool_call = {"name": "delete_item", "arguments": {"id": "123"}}
        event = build_confirmation_event(tool_call, "Deletes an item")
        assert event["tool_name"] == "delete_item"
        assert event["description"] == "Deletes an item"
        assert event["requires_confirmation"] is True
        assert event["arguments"] == {"id": "123"}


# ── run_planning_loop ─────────────────────────────────────────────────────────

def _make_mock_registry(tools=None):
    """Create a mock ToolRegistry with given tool definitions."""
    registry = MagicMock()

    def _get(name):
        if tools and name in tools:
            return tools[name]
        return None

    registry.get = _get
    return registry


def _make_tool(mutates=False, description="A tool"):
    """Create a mock tool object."""
    tool = MagicMock()
    tool.mutates = mutates
    tool.description = description
    return tool


def _make_tool_result(success=True, message="ok", data=None, artifact_refs=None):
    result = MagicMock()
    result.success = success
    result.message = message
    result.data = data or {}
    result.artifact_refs = artifact_refs or []
    result.to_llm_context.return_value = f"Result: {message}"
    return result


class TestRunPlanningLoop:
    @pytest.mark.asyncio
    async def test_no_tool_call_returns_text(self):
        """If initial response has no tool call, return it directly."""
        registry = _make_mock_registry()
        gen = AsyncMock(return_value="Final answer")

        result = await run_planning_loop(
            registry=registry,
            generate_fn=gen,
            initial_response="Just a text response",
            system_prompt="sys",
            user_prompt="what?",
            context="ctx",
        )

        assert result["needs_confirmation"] is False
        assert result["final_text"] == "Just a text response"
        gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_mutating_tool_returns_confirmation(self):
        """Mutating tool should stop loop and return confirmation."""
        delete_tool = _make_tool(mutates=True, description="Deletes stuff")
        registry = _make_mock_registry(tools={"delete": delete_tool})

        result = await run_planning_loop(
            registry=registry,
            generate_fn=AsyncMock(),
            initial_response='<tool_call>{"name": "delete", "arguments": {"id": "1"}}</tool_call>',
            system_prompt="sys",
            user_prompt="delete it",
            context="ctx",
        )

        assert result["needs_confirmation"] is True
        assert result["confirmation_event"]["tool_name"] == "delete"

    @pytest.mark.asyncio
    async def test_read_only_tool_executes_and_replans(self):
        """Read-only tool should execute, feed result back, and get final response."""
        search_tool = _make_tool(mutates=False)
        registry = _make_mock_registry(tools={"search": search_tool})
        tool_result = _make_tool_result(message="Found 3 items")

        with patch("services.agentic_chat.execute_tool_call", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = tool_result

            gen = AsyncMock(return_value="Based on the results, here are 3 items.")

            result = await run_planning_loop(
                registry=registry,
                generate_fn=gen,
                initial_response='<tool_call>{"name": "search", "arguments": {"q": "test"}}</tool_call>',
                system_prompt="sys",
                user_prompt="search",
                context="ctx",
            )

        assert result["needs_confirmation"] is False
        assert "3 items" in result["final_text"]
        gen.assert_called_once()

    @pytest.mark.asyncio
    async def test_multi_step_chain(self):
        """Two read-only tools chained: search → get_details → final answer."""
        search_tool = _make_tool(mutates=False)
        details_tool = _make_tool(mutates=False)
        registry = _make_mock_registry(tools={
            "search": search_tool,
            "get_details": details_tool,
        })

        search_result = _make_tool_result(message="Found item-42")
        details_result = _make_tool_result(message="Item 42: Pool maintenance")

        call_count = 0

        async def mock_exec(reg, tool_call, db=None, confirmed=True):
            nonlocal call_count
            call_count += 1
            if tool_call["name"] == "search":
                return search_result
            return details_result

        with patch("services.agentic_chat.execute_tool_call", side_effect=mock_exec):
            gen_responses = [
                '<tool_call>{"name": "get_details", "arguments": {"id": "42"}}</tool_call>',
                "Item 42 is for pool maintenance.",
            ]
            gen = AsyncMock(side_effect=gen_responses)

            result = await run_planning_loop(
                registry=registry,
                generate_fn=gen,
                initial_response='<tool_call>{"name": "search", "arguments": {"q": "pool"}}</tool_call>',
                system_prompt="sys",
                user_prompt="tell me about pool",
                context="ctx",
            )

        assert result["needs_confirmation"] is False
        assert "pool maintenance" in result["final_text"]
        assert call_count == 2
        assert gen.call_count == 2

    @pytest.mark.asyncio
    async def test_unknown_tool_stops_loop(self):
        """Unknown tool name should stop the loop."""
        registry = _make_mock_registry(tools={})

        result = await run_planning_loop(
            registry=registry,
            generate_fn=AsyncMock(),
            initial_response='<tool_call>{"name": "nonexistent", "arguments": {}}</tool_call>',
            system_prompt="sys",
            user_prompt="do thing",
            context="ctx",
        )

        assert result["needs_confirmation"] is False
        # Final text is whatever strip_tool_call returns (may include raw call if stripping fails)
        assert result["final_text"] is not None

    @pytest.mark.asyncio
    async def test_callbacks_invoked(self):
        """on_tool_call, on_tool_result, on_status should all be called."""
        tool = _make_tool(mutates=False)
        registry = _make_mock_registry(tools={"search": tool})
        tool_result = _make_tool_result()

        on_call = MagicMock()
        on_result = MagicMock()
        on_status = MagicMock()

        with patch("services.agentic_chat.execute_tool_call", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = tool_result

            result = await run_planning_loop(
                registry=registry,
                generate_fn=AsyncMock(return_value="done"),
                initial_response='<tool_call>{"name": "search", "arguments": {"q": "x"}}</tool_call>',
                system_prompt="sys",
                user_prompt="search",
                context="ctx",
                on_tool_call=on_call,
                on_tool_result=on_result,
                on_status=on_status,
            )

        on_call.assert_called_once_with("search", {"q": "x"})
        on_result.assert_called_once_with("search", tool_result)
        assert on_status.call_count >= 2  # "Running search…" + "Thinking…"

    @pytest.mark.asyncio
    async def test_max_steps_enforced(self):
        """Loop should terminate after MAX_TOOL_STEPS even if LLM keeps calling tools."""
        tool = _make_tool(mutates=False)
        registry = _make_mock_registry(tools={"loop_tool": tool})
        tool_result = _make_tool_result()

        call_text = '<tool_call>{"name": "loop_tool", "arguments": {}}</tool_call>'

        with patch("services.agentic_chat.execute_tool_call", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = tool_result
            gen = AsyncMock(return_value=call_text)

            result = await run_planning_loop(
                registry=registry,
                generate_fn=gen,
                initial_response=call_text,
                system_prompt="sys",
                user_prompt="loop",
                context="ctx",
            )

        # Should have executed MAX_TOOL_STEPS times
        assert mock_exec.call_count == MAX_TOOL_STEPS
        assert result["state"].done is True

    @pytest.mark.asyncio
    async def test_llm_error_uses_last_observation(self):
        """If the LLM call fails mid-loop, use the last tool observation."""
        tool = _make_tool(mutates=False)
        registry = _make_mock_registry(tools={"search": tool})
        tool_result = _make_tool_result(message="search found items")

        with patch("services.agentic_chat.execute_tool_call", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = tool_result
            gen = AsyncMock(side_effect=RuntimeError("LLM down"))

            result = await run_planning_loop(
                registry=registry,
                generate_fn=gen,
                initial_response='<tool_call>{"name": "search", "arguments": {}}</tool_call>',
                system_prompt="sys",
                user_prompt="search",
                context="ctx",
            )

        assert result["state"].done is True
        assert "search" in result["final_text"]
