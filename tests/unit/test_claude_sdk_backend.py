import json
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import ResultMessage

from analogcoder.agents.backend import AgentExecutionError, ToolSpec
from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend, _wrap_tool


def _result_message(structured_output=None, is_error=False):
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        structured_output=structured_output,
    )


async def _fake_query_success(prompt, options):
    yield _result_message(structured_output={"ok": True})


async def _fake_query_error(prompt, options):
    yield _result_message(is_error=True)


async def _fake_query_no_result_message(prompt, options):
    return
    yield  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_run_returns_structured_output_with_no_tools():
    backend = ClaudeSDKBackend()
    with patch("analogcoder.agents.backends.claude_sdk.query", _fake_query_success):
        result = await backend.run("system prompt", "user prompt", {"type": "object"}, [])
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_run_raises_on_error_result():
    backend = ClaudeSDKBackend()
    with patch("analogcoder.agents.backends.claude_sdk.query", _fake_query_error):
        with pytest.raises(AgentExecutionError):
            await backend.run("system prompt", "user prompt", {"type": "object"}, [])


@pytest.mark.asyncio
async def test_run_raises_when_no_result_message():
    backend = ClaudeSDKBackend()
    with patch("analogcoder.agents.backends.claude_sdk.query", _fake_query_no_result_message):
        with pytest.raises(AgentExecutionError):
            await backend.run("system prompt", "user prompt", {"type": "object"}, [])


@pytest.mark.asyncio
async def test_run_wires_tools_into_mcp_server_and_allowed_tools():
    backend = ClaudeSDKBackend()
    captured = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield _result_message(structured_output={"ok": True})

    tool_spec = ToolSpec(
        name="my_tool",
        description="does a thing",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler=AsyncMock(return_value={"ok": True}),
    )

    with patch("analogcoder.agents.backends.claude_sdk.query", fake_query):
        await backend.run("system prompt", "user prompt", {"type": "object"}, [tool_spec])

    options = captured["options"]
    assert options.allowed_tools == ["mcp__agent_tools__my_tool"]
    assert "agent_tools" in options.mcp_servers


@pytest.mark.asyncio
async def test_wrap_tool_invokes_tool_spec_handler_and_serializes_result():
    handler = AsyncMock(return_value={"computed": 42})
    tool_spec = ToolSpec(
        name="compute",
        description="computes a thing",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
        handler=handler,
    )

    wrapped = _wrap_tool(tool_spec)
    result = await wrapped.handler({"x": 1})

    handler.assert_awaited_once_with({"x": 1})
    assert result == {"content": [{"type": "text", "text": json.dumps({"computed": 42})}]}
