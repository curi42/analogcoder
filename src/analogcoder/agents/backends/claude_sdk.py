import json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool

from analogcoder.agents.backend import AgentBackend, AgentExecutionError, ToolSpec


def _wrap_tool(tool_spec: ToolSpec):
    @tool(tool_spec.name, tool_spec.description, tool_spec.parameters)
    async def _handler(args):
        result = await tool_spec.handler(args)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return _handler


DEFAULT_CLAUDE_MODEL = "sonnet"


class ClaudeSDKBackend(AgentBackend):
    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL):
        # Always pinned explicitly, never left unset. An unset model inherits
        # the bundled claude CLI's configured default, so a change to the
        # user's ~/.claude/settings.json silently changes which model every
        # agent runs on - that is how a whole verification run ended up on
        # Opus. Dev runs must not be MORE capable than the production target
        # model, or the pipeline's reliability looks better than it will be.
        self.model = model

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
            model=self.model,
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
