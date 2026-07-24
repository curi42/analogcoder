import jsonschema

from analogcoder.agents.backend import AgentBackend, AgentExecutionError, ToolSpec


async def run_agent(
    system_prompt: str,
    user_prompt: str,
    output_schema: dict,
    backend: AgentBackend,
    tools: list[ToolSpec] | None = None,
) -> dict:
    result = await backend.run(system_prompt, user_prompt, output_schema, tools or [])
    try:
        jsonschema.validate(result, output_schema)
    except jsonschema.ValidationError as exc:
        raise AgentExecutionError(
            f"backend returned output that does not match the schema: {exc.message}"
        ) from exc
    return result
