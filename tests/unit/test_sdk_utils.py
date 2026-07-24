from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from analogcoder.agents._sdk_utils import AgentExecutionError, run_agent


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
async def test_run_agent_returns_structured_output():
    with patch("analogcoder.agents._sdk_utils.query", _fake_query_success):
        result = await run_agent("system prompt", "user prompt", {"type": "object"})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_run_agent_raises_on_error_result():
    with patch("analogcoder.agents._sdk_utils.query", _fake_query_error):
        with pytest.raises(AgentExecutionError):
            await run_agent("system prompt", "user prompt", {"type": "object"})


@pytest.mark.asyncio
async def test_run_agent_raises_when_no_result_message():
    with patch("analogcoder.agents._sdk_utils.query", _fake_query_no_result_message):
        with pytest.raises(AgentExecutionError):
            await run_agent("system prompt", "user prompt", {"type": "object"})
