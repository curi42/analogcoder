import pytest

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, AgentExecutionError

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class FakeBackend(AgentBackend):
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def run(self, system_prompt, user_prompt, output_schema, tools):
        if self._error:
            raise self._error
        return self._result


@pytest.mark.asyncio
async def test_run_agent_returns_backend_result_when_schema_valid():
    backend = FakeBackend(result={"ok": True})
    result = await run_agent("system", "user", SCHEMA, backend)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_run_agent_raises_when_backend_result_violates_schema():
    backend = FakeBackend(result={"ok": "not-a-boolean"})
    with pytest.raises(AgentExecutionError):
        await run_agent("system", "user", SCHEMA, backend)


@pytest.mark.asyncio
async def test_run_agent_propagates_agent_execution_error_from_backend():
    backend = FakeBackend(error=AgentExecutionError("backend unreachable"))
    with pytest.raises(AgentExecutionError, match="backend unreachable"):
        await run_agent("system", "user", SCHEMA, backend)
