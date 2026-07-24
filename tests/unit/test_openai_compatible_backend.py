import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from analogcoder.agents.backend import AgentExecutionError, ToolSpec
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _response(message: dict):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": message}]})
    return resp


@pytest.mark.asyncio
async def test_run_returns_valid_structured_output(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-token")
    backend = OpenAICompatibleBackend(base_url="http://local", api_key_env="TEST_LLM_KEY", model="glm-5.2")

    mock_post = AsyncMock(return_value=_response({"role": "assistant", "content": json.dumps({"ok": True})}))
    with patch("httpx.AsyncClient.post", mock_post):
        result = await backend.run("system", "user", SCHEMA, [])

    assert result == {"ok": True}
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_run_executes_tool_call_then_returns_final_output(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-token")
    backend = OpenAICompatibleBackend(base_url="http://local", api_key_env="TEST_LLM_KEY", model="glm-5.2")

    handler = AsyncMock(return_value={"computed": 42})
    tool = ToolSpec(
        name="compute",
        description="computes a thing",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
        handler=handler,
    )

    tool_call_response = _response(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "compute", "arguments": json.dumps({"x": 1})},
                }
            ],
        }
    )
    final_response = _response({"role": "assistant", "content": json.dumps({"ok": True})})

    mock_post = AsyncMock(side_effect=[tool_call_response, final_response])
    with patch("httpx.AsyncClient.post", mock_post):
        result = await backend.run("system", "user", SCHEMA, [tool])

    assert result == {"ok": True}
    handler.assert_awaited_once_with({"x": 1})


@pytest.mark.asyncio
async def test_run_repairs_invalid_schema_output_then_succeeds(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-token")
    backend = OpenAICompatibleBackend(base_url="http://local", api_key_env="TEST_LLM_KEY", model="glm-5.2")

    bad_response = _response({"role": "assistant", "content": "not json"})
    good_response = _response({"role": "assistant", "content": json.dumps({"ok": True})})

    mock_post = AsyncMock(side_effect=[bad_response, good_response])
    with patch("httpx.AsyncClient.post", mock_post):
        result = await backend.run("system", "user", SCHEMA, [])

    assert result == {"ok": True}
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_run_raises_after_exhausting_structured_output_repairs(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-token")
    backend = OpenAICompatibleBackend(base_url="http://local", api_key_env="TEST_LLM_KEY", model="glm-5.2")

    always_bad = _response({"role": "assistant", "content": "not json"})
    mock_post = AsyncMock(return_value=always_bad)
    with patch("httpx.AsyncClient.post", mock_post):
        with pytest.raises(AgentExecutionError):
            await backend.run("system", "user", SCHEMA, [])

    assert mock_post.call_count == 3  # initial attempt + 2 repairs


@pytest.mark.asyncio
async def test_run_raises_when_tool_loop_exceeds_max_turns(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-token")
    backend = OpenAICompatibleBackend(base_url="http://local", api_key_env="TEST_LLM_KEY", model="glm-5.2")

    handler = AsyncMock(return_value={"computed": 1})
    tool = ToolSpec(
        name="compute", description="d", parameters={"type": "object", "properties": {}}, handler=handler
    )

    endless_tool_call = _response(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "compute", "arguments": "{}"}}
            ],
        }
    )
    mock_post = AsyncMock(return_value=endless_tool_call)
    with patch("httpx.AsyncClient.post", mock_post):
        with pytest.raises(AgentExecutionError):
            await backend.run("system", "user", SCHEMA, [tool])

    assert mock_post.call_count == 6  # MAX_TOOL_LOOP_TURNS
