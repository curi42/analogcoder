import pytest

from analogcoder.agents.backend import AgentBackend, AgentExecutionError, ToolSpec


def test_tool_spec_holds_name_description_parameters_and_handler():
    async def handler(args):
        return {"ok": True}

    spec = ToolSpec(
        name="my_tool",
        description="does a thing",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )

    assert spec.name == "my_tool"
    assert spec.description == "does a thing"
    assert spec.parameters == {"type": "object", "properties": {}}
    assert spec.handler is handler


def test_agent_backend_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        AgentBackend()


def test_agent_backend_subclass_must_implement_run():
    class IncompleteBackend(AgentBackend):
        pass

    with pytest.raises(TypeError):
        IncompleteBackend()


def test_agent_execution_error_is_a_runtime_error():
    assert issubclass(AgentExecutionError, RuntimeError)
