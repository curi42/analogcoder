from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query


class AgentExecutionError(RuntimeError):
    """Raised when an agent query errors out or returns no structured output."""


async def run_agent(
    system_prompt: str,
    user_prompt: str,
    output_schema: dict,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
) -> dict:
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        output_format={"type": "json_schema", "schema": output_schema},
        mcp_servers=mcp_servers or {},
        allowed_tools=allowed_tools or [],
    )

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, ResultMessage):
            if message.is_error or message.structured_output is None:
                raise AgentExecutionError(
                    f"agent query failed: subtype={message.subtype} errors={message.errors}"
                )
            return message.structured_output

    raise AgentExecutionError("agent query stream ended without a ResultMessage")
