import json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool

from analogcoder.agents.backend import AgentBackend, AgentExecutionError, ToolSpec


def _wrap_tool(tool_spec: ToolSpec):
    @tool(tool_spec.name, tool_spec.description, tool_spec.parameters)
    async def _handler(args):
        result = await tool_spec.handler(args)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return _handler


class ClaudeSDKBackend(AgentBackend):
    async def run(
        self, system_prompt: str, user_prompt: str, output_schema: dict, tools: list[ToolSpec]
    ) -> dict:
        mcp_servers = {}
        allowed_tools = []
        if tools:
            wrapped_tools = [_wrap_tool(t) for t in tools]
            mcp_servers = {"agent_tools": create_sdk_mcp_server("agent_tools", tools=wrapped_tools)}
            allowed_tools = [f"mcp__agent_tools__{t.name}" for t in tools]

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            output_format={"type": "json_schema", "schema": output_schema},
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
        )

        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, ResultMessage):
                if message.is_error or message.structured_output is None:
                    raise AgentExecutionError(
                        f"agent query failed: subtype={message.subtype} errors={message.errors}"
                    )
                return message.structured_output

        raise AgentExecutionError("agent query stream ended without a ResultMessage")
