import json
import os

import httpx
import jsonschema

from analogcoder.agents.backend import AgentBackend, AgentExecutionError, ToolSpec

MAX_TOOL_LOOP_TURNS = 6
MAX_STRUCTURED_OUTPUT_REPAIRS = 2


class OpenAICompatibleBackend(AgentBackend):
    def __init__(self, base_url: str, api_key_env: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.timeout = timeout

    def _headers(self) -> dict:
        token = os.environ[self.api_key_env]
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _tools_payload(self, tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]

    async def _post(
        self, client: httpx.AsyncClient, messages: list[dict], tools: list[ToolSpec], output_schema: dict
    ) -> dict:
        payload = {"model": self.model, "messages": messages}
        if tools:
            # A schema-constrained response_format suppresses tool_calls on some
            # OpenAI-compatible servers (observed on Ollama: the model fabricates
            # schema-shaped output instead of calling the tool). Only constrain
            # output on turns where no tool is being offered.
            payload["tools"] = self._tools_payload(tools)
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "agent_output", "schema": output_schema},
            }
        response = await client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]

    async def run(
        self, system_prompt: str, user_prompt: str, output_schema: dict, tools: list[ToolSpec]
    ) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tools_by_name = {t.name: t for t in tools}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            message = None
            for _ in range(MAX_TOOL_LOOP_TURNS):
                message = await self._post(client, messages, tools, output_schema)
                tool_calls = message.get("tool_calls")
                if not tool_calls:
                    break
                messages.append(message)
                for call in tool_calls:
                    tool_spec = tools_by_name.get(call["function"]["name"])
                    if tool_spec is None:
                        raise AgentExecutionError(
                            f"model requested unknown tool: {call['function']['name']}"
                        )
                    args = json.loads(call["function"]["arguments"])
                    result = await tool_spec.handler(args)
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
                    )
            else:
                raise AgentExecutionError(f"tool call loop exceeded {MAX_TOOL_LOOP_TURNS} turns")

            content = message.get("content") or ""
            for attempt in range(MAX_STRUCTURED_OUTPUT_REPAIRS + 1):
                try:
                    candidate = json.loads(content)
                    jsonschema.validate(candidate, output_schema)
                    return candidate
                except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
                    if attempt == MAX_STRUCTURED_OUTPUT_REPAIRS:
                        raise AgentExecutionError(
                            f"model output did not match schema after {MAX_STRUCTURED_OUTPUT_REPAIRS} "
                            f"repair attempts: {exc}"
                        ) from exc
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your last response did not match the required JSON schema: {exc}. "
                                "Respond again with corrected JSON only, no other text."
                            ),
                        }
                    )
                    message = await self._post(client, messages, [], output_schema)
                    content = message.get("content") or ""

        raise AgentExecutionError("unreachable")
