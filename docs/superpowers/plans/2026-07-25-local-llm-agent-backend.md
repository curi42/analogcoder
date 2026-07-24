# Local LLM Agent Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple analogcoder's 5 agents from claude-agent-sdk so a lower-capability local/company LLM (reached via an OpenAI-compatible endpoint: base URL + bearer token + model name) can run the same agent team later, without touching agent prompt/schema logic.

**Architecture:** Introduce an `AgentBackend` adapter interface (same pattern as the existing `SimulatorBackend`). `ClaudeSDKBackend` wraps today's claude-agent-sdk logic unchanged (preserves the Claude Code subscription-based, no-separate-API-billing execution path). `OpenAICompatibleBackend` is a new, directly-testable implementation (e.g. against Ollama) that talks to any OpenAI-style `/chat/completions` endpoint, with its own tool-call loop and a schema-validation-with-repair retry loop to absorb weaker structured-output reliability. All 5 agent modules and the orchestrator's failure handling are updated to use this abstraction.

**Tech Stack:** Python >=3.11, claude-agent-sdk, httpx (new), jsonschema (promoted from dev to runtime dependency), pytest + pytest-asyncio.

## Global Constraints

- `AgentBackend`, `ToolSpec`, and `AgentExecutionError` live in exactly one place: `src/analogcoder/agents/backend.py`. Every other module imports them from there.
- `src/analogcoder/agents/_sdk_utils.py` is renamed to `src/analogcoder/agents/agent_runtime.py`. `run_agent()` there gains a required `backend: AgentBackend` parameter and an optional `tools: list[ToolSpec] | None = None` parameter, replacing the old `mcp_servers`/`allowed_tools` parameters. After calling `backend.run(...)`, `run_agent()` validates the result against `output_schema` with `jsonschema.validate`, raising `AgentExecutionError` on mismatch — this is a project-wide safety net, not specific to any one backend.
- Every agent module's public function (`analyze_netlist`, `judge_measurements`, `simulate`, `propose_tuning`, `verify_pre`, `verify_post`) gains a required `backend: AgentBackend` parameter. `simulate()`'s existing `SimulatorBackend` parameter is renamed from `backend` to `sim_backend` to disambiguate it from the new LLM `backend` parameter.
- `judge.py` and `simulator_agent.py` stop building claude-agent-sdk MCP servers directly; they build backend-agnostic `ToolSpec` objects and pass them to `run_agent(..., tools=[...])`.
- `OpenAICompatibleBackend`'s constants `MAX_TOOL_LOOP_TURNS = 6` and `MAX_STRUCTURED_OUTPUT_REPAIRS = 2` are defined once in `src/analogcoder/agents/backends/openai_compatible.py`.
- API tokens for `OpenAICompatibleBackend` are read from `os.environ` at call time, by a configurable environment variable name (constructor arg `api_key_env`) — never accepted as a CLI argument, never logged, never embedded in error messages.
- `orchestrator.py`'s `run_orchestration()` must catch `AgentExecutionError` and return a normal `{"status": "FAIL", ...}` result dict instead of letting it propagate. The existing PASS/FAIL control flow (already verified by a prior whole-branch review) must not change behavior for any currently-passing test.
- CLI exit code contract (0 = PASS, 1 = FAIL), `result.json`/`report.md` output contract, and `OrchestratorAgents` dataclass shape are unchanged.
- Every task must leave `pytest -q` fully green (the existing 41 passed / 1 skipped baseline, plus this plan's new tests) before its final commit.

---

### Task 1: `AgentBackend` interface, `ToolSpec`, `AgentExecutionError`

**Files:**
- Modify: `pyproject.toml`
- Create: `src/analogcoder/agents/backend.py`
- Test: `tests/unit/test_agent_backend.py`

**Interfaces:**
- Produces: `ToolSpec(name: str, description: str, parameters: dict, handler: Callable[[dict], Awaitable[dict]])` dataclass; `AgentBackend` ABC with `async def run(self, system_prompt: str, user_prompt: str, output_schema: dict, tools: list[ToolSpec]) -> dict`; `AgentExecutionError(RuntimeError)`.

- [ ] **Step 1: Update dependencies**

Edit `pyproject.toml`:

```toml
[project]
name = "analogcoder"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "claude-agent-sdk>=0.1.0",
    "pyyaml>=6.0",
    "jsonschema>=4.21",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[project.scripts]
analogcoder = "analogcoder.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/analogcoder"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

Run: `.venv/bin/pip install -q -e ".[dev]"`
Expected: installs `httpx` with no errors; `jsonschema` remains installed (now via main dependencies).

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_agent_backend.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.backend'`

- [ ] **Step 4: Write minimal implementation**

Create `src/analogcoder/agents/backend.py`:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable


class AgentExecutionError(RuntimeError):
    """Raised when an agent backend errors out or returns no usable output."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[dict]]


class AgentBackend(ABC):
    @abstractmethod
    async def run(
        self, system_prompt: str, user_prompt: str, output_schema: dict, tools: list[ToolSpec]
    ) -> dict:
        ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_backend.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/analogcoder/agents/backend.py tests/unit/test_agent_backend.py
git commit -m "feat: add AgentBackend interface, ToolSpec, and AgentExecutionError"
```

---

### Task 2: `ClaudeSDKBackend`

**Files:**
- Create: `src/analogcoder/agents/backends/__init__.py`
- Create: `src/analogcoder/agents/backends/claude_sdk.py`
- Test: `tests/unit/test_claude_sdk_backend.py`

**Interfaces:**
- Consumes: `AgentBackend`, `ToolSpec`, `AgentExecutionError` from `analogcoder.agents.backend` (Task 1).
- Produces: `ClaudeSDKBackend()` — a no-arg-constructor `AgentBackend` implementation. `_wrap_tool(tool_spec: ToolSpec)` — module-level helper returning an `SdkMcpTool` whose `.handler` is an async function that calls `tool_spec.handler` and returns `{"content": [{"type": "text", "text": json.dumps(result)}]}`.

- [ ] **Step 1: Write the failing test**

Create `src/analogcoder/agents/backends/__init__.py` (empty file).

Create `tests/unit/test_claude_sdk_backend.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_sdk_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.backends.claude_sdk'`

- [ ] **Step 3: Write minimal implementation**

Create `src/analogcoder/agents/backends/claude_sdk.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_sdk_backend.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/backends/__init__.py src/analogcoder/agents/backends/claude_sdk.py tests/unit/test_claude_sdk_backend.py
git commit -m "feat: add ClaudeSDKBackend implementing AgentBackend"
```

---

### Task 3: `OpenAICompatibleBackend`

**Files:**
- Create: `src/analogcoder/agents/backends/openai_compatible.py`
- Test: `tests/unit/test_openai_compatible_backend.py`

**Interfaces:**
- Consumes: `AgentBackend`, `ToolSpec`, `AgentExecutionError` from `analogcoder.agents.backend` (Task 1).
- Produces: `OpenAICompatibleBackend(base_url: str, api_key_env: str, model: str)` — an `AgentBackend` implementation with attributes `.base_url`, `.api_key_env`, `.model`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_openai_compatible_backend.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_openai_compatible_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.backends.openai_compatible'`

- [ ] **Step 3: Write minimal implementation**

Create `src/analogcoder/agents/backends/openai_compatible.py`:

```python
import json
import os

import httpx
import jsonschema

from analogcoder.agents.backend import AgentBackend, AgentExecutionError, ToolSpec

MAX_TOOL_LOOP_TURNS = 6
MAX_STRUCTURED_OUTPUT_REPAIRS = 2


class OpenAICompatibleBackend(AgentBackend):
    def __init__(self, base_url: str, api_key_env: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model

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

    async def _post(self, client: httpx.AsyncClient, messages: list[dict], tools: list[ToolSpec]) -> dict:
        payload = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = self._tools_payload(tools)
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

        async with httpx.AsyncClient() as client:
            message = None
            for _ in range(MAX_TOOL_LOOP_TURNS):
                message = await self._post(client, messages, tools)
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
                    message = await self._post(client, messages, [])
                    content = message.get("content") or ""

        raise AgentExecutionError("unreachable")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_openai_compatible_backend.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/backends/openai_compatible.py tests/unit/test_openai_compatible_backend.py
git commit -m "feat: add OpenAICompatibleBackend with tool-call loop and structured-output repair retry"
```

---

### Task 4: Rewire `agent_runtime.py` and all 5 agent modules onto `AgentBackend`

This task is one atomic unit: renaming the runtime module and updating every agent module happen together because they change each other's call contract. The tree will not be green until the final step of this task — that is expected within a single task.

**Files:**
- Rename+Modify: `src/analogcoder/agents/_sdk_utils.py` → `src/analogcoder/agents/agent_runtime.py`
- Rename+Modify: `tests/unit/test_sdk_utils.py` → `tests/unit/test_agent_runtime.py`
- Modify: `src/analogcoder/agents/analyzer.py`, `src/analogcoder/agents/tuner.py`, `src/analogcoder/agents/verifier.py`, `src/analogcoder/agents/judge.py`, `src/analogcoder/agents/simulator_agent.py`
- Modify: `tests/unit/test_analyzer_agent.py`, `tests/unit/test_tuner_agent.py`, `tests/unit/test_verifier_agent.py`, `tests/unit/test_judge_agent.py`, `tests/unit/test_simulator_agent.py`

**Interfaces:**
- Consumes: `AgentBackend`, `ToolSpec`, `AgentExecutionError` from `analogcoder.agents.backend` (Task 1).
- Produces: `run_agent(system_prompt: str, user_prompt: str, output_schema: dict, backend: AgentBackend, tools: list[ToolSpec] | None = None) -> dict` in `analogcoder.agents.agent_runtime`. Every agent function gains a required `backend: AgentBackend` parameter (see Global Constraints for exact signatures). Task 5 (orchestrator) and Task 6 (cli.py) depend on these exact signatures.

- [ ] **Step 1: Rename the runtime module**

```bash
git mv src/analogcoder/agents/_sdk_utils.py src/analogcoder/agents/agent_runtime.py
git mv tests/unit/test_sdk_utils.py tests/unit/test_agent_runtime.py
```

- [ ] **Step 2: Write the failing test for the new `run_agent` contract**

Replace the contents of `tests/unit/test_agent_runtime.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_runtime.py -v`
Expected: FAIL (`run_agent()` still has the old signature; `AttributeError`/`TypeError` on the `backend`-based calls)

- [ ] **Step 4: Rewrite `agent_runtime.py`**

Replace the contents of `src/analogcoder/agents/agent_runtime.py`:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_runtime.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Update `analyzer.py`**

Replace the contents of `src/analogcoder/agents/analyzer.py`:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import ANALYZER_SCHEMA

ANALYZER_SYSTEM_PROMPT = """You are a senior analog IC design engineer. Given a SPICE
netlist, identify the circuit type, break it into functional stages, explain the role
of each component, and list which components/parameters are safe to tune without
changing the circuit's topology. Respond only via the structured output schema."""


async def analyze_netlist(netlist_text: str, backend: AgentBackend) -> dict:
    user_prompt = f"Analyze this SPICE netlist:\n\n{netlist_text}"
    return await run_agent(
        system_prompt=ANALYZER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=ANALYZER_SCHEMA,
        backend=backend,
    )
```

Replace the contents of `tests/unit/test_analyzer_agent.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.analyzer import analyze_netlist


@pytest.mark.asyncio
async def test_analyze_netlist_calls_run_agent_with_netlist_text():
    fake_result = {
        "circuit_type": "inverting amplifier",
        "stages": [],
        "component_roles": {},
        "tunable_params": [],
    }
    fake_backend = object()
    with patch("analogcoder.agents.analyzer.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await analyze_netlist("Rin in vminus 1k\n.end\n", backend=fake_backend)

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "Rin in vminus 1k" in kwargs["user_prompt"]
    assert kwargs["output_schema"]["required"] == ["circuit_type", "stages", "component_roles", "tunable_params"]
    assert kwargs["backend"] is fake_backend
```

- [ ] **Step 7: Update `tuner.py`**

Replace the contents of `src/analogcoder/agents/tuner.py`:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import TUNER_SCHEMA

TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Given the
circuit's structural analysis, the judge's pass/fail verdict, the history of past
tuning attempts in this run, and (if present) feedback on why your last proposal
was rejected, propose specific component parameter changes to fix the failing
criteria. Only propose changes to parameters listed in tunable_params. Respond via
the structured output schema."""


async def propose_tuning(
    analysis: dict,
    judge_result: dict,
    history: list[dict],
    rejection_feedback: str | None,
    backend: AgentBackend,
) -> dict:
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result: {judge_result}\n"
        f"Past attempts this run: {history}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=TUNER_SCHEMA,
        backend=backend,
    )
```

Replace the contents of `tests/unit/test_tuner_agent.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.tuner import propose_tuning


@pytest.mark.asyncio
async def test_propose_tuning_includes_history_and_rejection_feedback_in_prompt():
    fake_result = {
        "proposed_changes": [
            {"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k", "reasoning": "increase gain"}
        ],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    fake_backend = object()
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await propose_tuning(
            analysis={"circuit_type": "inverting amplifier"},
            judge_result={"overall_pass": False},
            history=[{"outer_iter": 1, "recommendation": "rollback"}],
            rejection_feedback="last proposal changed a fixed component",
            backend=fake_backend,
        )

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "rollback" in kwargs["user_prompt"]
    assert "last proposal changed a fixed component" in kwargs["user_prompt"]
    assert kwargs["backend"] is fake_backend
```

- [ ] **Step 8: Update `verifier.py`**

Replace the contents of `src/analogcoder/agents/verifier.py`:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import VERIFIER_POST_SCHEMA, VERIFIER_PRE_SCHEMA

VERIFIER_SYSTEM_PROMPT = """You are a skeptical senior reviewer for analog circuit
tuning decisions. You check whether a proposed or applied change is justified by
the circuit analysis and simulation results, and whether it could cause unintended
side effects on other criteria."""


async def verify_pre(analysis: dict, judge_result: dict, proposal: dict, backend: AgentBackend) -> dict:
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result before tuning: {judge_result}\n"
        f"Proposed changes: {proposal}\n"
        "Decide whether to approve this proposal before it is applied."
    )
    return await run_agent(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VERIFIER_PRE_SCHEMA,
        backend=backend,
    )


async def verify_post(
    prev_judge_result: dict, new_judge_result: dict, applied_changes: list[dict], backend: AgentBackend
) -> dict:
    user_prompt = (
        f"Judge result before tuning: {prev_judge_result}\n"
        f"Judge result after applying and re-simulating: {new_judge_result}\n"
        f"Applied changes: {applied_changes}\n"
        "Decide whether the change should be kept or rolled back."
    )
    return await run_agent(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VERIFIER_POST_SCHEMA,
        backend=backend,
    )
```

Replace the contents of `tests/unit/test_verifier_agent.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.verifier import verify_post, verify_pre


@pytest.mark.asyncio
async def test_verify_pre_calls_run_agent_with_proposal():
    fake_result = {"approved": True, "concerns": [], "feedback": "reasonable"}
    fake_backend = object()
    with patch("analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await verify_pre(
            analysis={"circuit_type": "inverting amplifier"},
            judge_result={"overall_pass": False},
            proposal={"proposed_changes": [{"refdes": "Rf", "param": "value", "new_value": "11k"}]},
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["approved", "concerns", "feedback"]
    assert kwargs["backend"] is fake_backend


@pytest.mark.asyncio
async def test_verify_post_calls_run_agent_with_before_after_judge_results():
    fake_result = {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "gain fixed"}
    fake_backend = object()
    with patch("analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await verify_post(
            prev_judge_result={"overall_pass": False},
            new_judge_result={"overall_pass": True},
            applied_changes=[{"refdes": "Rf", "param": "value", "new_value": "11k"}],
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["improved", "regressed_criteria", "recommendation", "feedback"]
    assert kwargs["backend"] is fake_backend
```

- [ ] **Step 9: Update `judge.py`**

Replace the contents of `src/analogcoder/agents/judge.py`:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, ToolSpec
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.schemas import JUDGE_SCHEMA
from analogcoder.spec import Criterion

JUDGE_SYSTEM_PROMPT = """You are an analog circuit judge. You are given simulation
measurements and a list of pass/fail criteria. Call the evaluate_criteria tool to
compute results precisely, then report them via the structured output schema. Do
not compute pass/fail comparisons yourself; always use the tool."""


def _build_judge_tool(criteria: list[Criterion]) -> ToolSpec:
    async def _evaluate(args: dict) -> dict:
        return evaluate_criteria(args["measurements"], criteria)

    return ToolSpec(
        name="evaluate_criteria",
        description="Compare measurements against target criteria",
        parameters={
            "type": "object",
            "properties": {"measurements": {"type": "object"}},
            "required": ["measurements"],
        },
        handler=_evaluate,
    )


async def judge_measurements(measurements: dict, criteria: list[Criterion], backend: AgentBackend) -> dict:
    judge_tool = _build_judge_tool(criteria)
    user_prompt = f"Measurements: {measurements}\nCriteria: {criteria}"
    return await run_agent(
        system_prompt=JUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=JUDGE_SCHEMA,
        backend=backend,
        tools=[judge_tool],
    )
```

Replace the contents of `tests/unit/test_judge_agent.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.judge import _build_judge_tool, judge_measurements
from analogcoder.spec import Criterion


@pytest.mark.asyncio
async def test_judge_measurements_calls_run_agent_with_measurements_and_criteria():
    fake_result = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
    }
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    fake_backend = object()

    with patch("analogcoder.agents.judge.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await judge_measurements({"gain_db": 20.0}, criteria, backend=fake_backend)

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["tools"][0].name == "evaluate_criteria"
    assert "gain_db" in kwargs["user_prompt"]
    assert kwargs["backend"] is fake_backend


@pytest.mark.asyncio
async def test_judge_tool_handler_calls_evaluate_criteria():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    tool_spec = _build_judge_tool(criteria)

    result = await tool_spec.handler({"measurements": {"gain_db": 20.0}})

    assert result["overall_pass"] is True
    assert result["criteria"][0]["name"] == "gain"
```

- [ ] **Step 10: Update `simulator_agent.py`**

Replace the contents of `src/analogcoder/agents/simulator_agent.py`:

```python
from dataclasses import asdict

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, ToolSpec
from analogcoder.schemas import SIMULATION_SCHEMA
from analogcoder.simulators.base import SimulatorBackend

SIMULATION_SYSTEM_PROMPT = """You are a SPICE simulation specialist. You are given a
netlist file path and a target spec's control block (analysis + measure directives).
Call the run_simulation tool to execute the simulation. If it reports a
convergence_failure, you may retry by adjusting the .options portion of the control
block (e.g. gmin stepping, method=gear), up to 2 extra attempts, before reporting
the final result via the structured output schema. Never modify component values."""


def _build_simulation_tool(sim_backend: SimulatorBackend, netlist_path: str) -> ToolSpec:
    async def _run(args: dict) -> dict:
        result = sim_backend.run(netlist_path, {"control_block": args["control_block"]})
        return asdict(result)

    return ToolSpec(
        name="run_simulation",
        description="Run the netlist through the configured simulator backend",
        parameters={
            "type": "object",
            "properties": {"control_block": {"type": "string"}},
            "required": ["control_block"],
        },
        handler=_run,
    )


async def simulate(
    netlist_path: str, control_block: str, sim_backend: SimulatorBackend, backend: AgentBackend
) -> dict:
    sim_tool = _build_simulation_tool(sim_backend, netlist_path)
    user_prompt = f"Netlist path: {netlist_path}\nControl block:\n{control_block}"
    return await run_agent(
        system_prompt=SIMULATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=SIMULATION_SCHEMA,
        backend=backend,
        tools=[sim_tool],
    )
```

Replace the contents of `tests/unit/test_simulator_agent.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.simulator_agent import _build_simulation_tool, simulate
from analogcoder.simulators.base import RawSimResult, SimulatorBackend


class FakeBackend(SimulatorBackend):
    def run(self, netlist_path, testbench_config):
        return RawSimResult(status="success", measurements={"gain_db": 20.0}, raw_log="ok", warnings=[])


@pytest.mark.asyncio
async def test_simulate_calls_run_agent_with_netlist_path_and_control_block():
    fake_result = {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}
    fake_agent_backend = object()
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ) as mock_run:
        result = await simulate(
            "benchmarks/inverting_amp/netlist.cir",
            ".control\nac dec 10 1 1meg\n.endc",
            FakeBackend(),
            fake_agent_backend,
        )

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "benchmarks/inverting_amp/netlist.cir" in kwargs["user_prompt"]
    assert "ac dec 10 1 1meg" in kwargs["user_prompt"]
    assert kwargs["tools"][0].name == "run_simulation"
    assert kwargs["backend"] is fake_agent_backend


@pytest.mark.asyncio
async def test_simulation_tool_handler_calls_sim_backend_run():
    tool_spec = _build_simulation_tool(FakeBackend(), "netlist.cir")

    result = await tool_spec.handler({"control_block": ".control\n.endc"})

    assert result["status"] == "success"
    assert result["measurements"] == {"gain_db": 20.0}
```

- [ ] **Step 11: Run the full unit test suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: all unit tests PASS (integration test is a separate directory, untouched by this task)

- [ ] **Step 12: Commit**

```bash
git add src/analogcoder/agents/agent_runtime.py tests/unit/test_agent_runtime.py \
        src/analogcoder/agents/analyzer.py tests/unit/test_analyzer_agent.py \
        src/analogcoder/agents/tuner.py tests/unit/test_tuner_agent.py \
        src/analogcoder/agents/verifier.py tests/unit/test_verifier_agent.py \
        src/analogcoder/agents/judge.py tests/unit/test_judge_agent.py \
        src/analogcoder/agents/simulator_agent.py tests/unit/test_simulator_agent.py
git rm src/analogcoder/agents/_sdk_utils.py tests/unit/test_sdk_utils.py 2>/dev/null || true
git commit -m "refactor: rename _sdk_utils to agent_runtime and thread AgentBackend through all 5 agents"
```

(The `git rm` is a no-op safeguard — `git mv` in Step 1 already staged the rename; this step ensures no stray file is left if the working tree diverged.)

---

### Task 5: Orchestrator failure handling for `AgentExecutionError`

**Files:**
- Modify: `src/analogcoder/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `AgentExecutionError` from `analogcoder.agents.backend` (Task 1). No change to `OrchestratorAgents` or the public `run_orchestration` signature.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_orchestrator.py` (append after the existing imports, and add the two new test functions at the end of the file):

Add this import near the top, alongside the existing imports:

```python
from analogcoder.agents.backend import AgentExecutionError
```

Append these two tests at the end of the file:

```python
@pytest.mark.asyncio
async def test_agent_execution_error_before_loop_returns_fail_with_zero_iterations(tmp_path):
    async def failing_analyze(netlist_text):
        raise AgentExecutionError("boom")

    agents = make_agents(analyze=failing_analyze)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["final_criteria"] == []
    assert result["failure_reason"] == "agent execution error: boom"


@pytest.mark.asyncio
async def test_agent_execution_error_mid_loop_reports_last_completed_iteration(tmp_path):
    call_count = {"n": 0}

    async def simulate_then_fail(netlist_text, spec):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise AgentExecutionError("simulator backend unreachable")
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    agents = make_agents(simulate=simulate_then_fail, judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["failure_reason"] == "agent execution error: simulator backend unreachable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: the two new tests FAIL — `AgentExecutionError` propagates out of `run_orchestration` uncaught instead of producing a FAIL result.

- [ ] **Step 3: Update `orchestrator.py`**

Replace the contents of `src/analogcoder/orchestrator.py`:

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.netlist import apply_changes
from analogcoder.state import RunState

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3


@dataclass
class OrchestratorAgents:
    analyze: Callable
    simulate: Callable
    judge: Callable
    tune: Callable
    verify_pre: Callable
    verify_post: Callable


def _final_result(
    status: str, state: RunState, iterations_used: int, judge_result: dict | None, failure_reason: str | None = None
) -> dict:
    result = {
        "status": status,
        "final_netlist_path": state.current_netlist_path(),
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"] if judge_result else [],
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


async def run_orchestration(initial_netlist_text: str, spec, state: RunState, agents: OrchestratorAgents) -> dict:
    state.push_netlist_version(initial_netlist_text)
    outer_iter = 0
    judge_result: dict = {}

    try:
        analysis = await agents.analyze(initial_netlist_text)
        state.log_event("analysis", analysis)

        tuning_history: list[dict] = []

        for outer_iter in range(1, MAX_OUTER_ITERATIONS + 1):
            with open(state.current_netlist_path()) as f:
                netlist_text = f.read()

            sim_result = await agents.simulate(netlist_text, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

            judge_result = await agents.judge(sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, **judge_result})

            if judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, judge_result)

            approved_proposal = None
            rejection_feedback = None
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                proposal = await agents.tune(analysis, judge_result, tuning_history, rejection_feedback)
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                review = await agents.verify_pre(analysis, judge_result, proposal)
                state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

                if review["approved"]:
                    approved_proposal = proposal
                    break
                rejection_feedback = review["feedback"]

            if approved_proposal is None:
                return _final_result(
                    "FAIL", state, outer_iter, judge_result, failure_reason="tuning proposal repeatedly rejected"
                )

            new_netlist_text = apply_changes(netlist_text, approved_proposal["proposed_changes"])
            state.push_netlist_version(new_netlist_text)

            new_sim_result = await agents.simulate(new_netlist_text, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, "post_tuning": True, **new_sim_result})

            new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, "post_tuning": True, **new_judge_result})

            post_review = await agents.verify_post(
                judge_result, new_judge_result, approved_proposal["proposed_changes"]
            )
            state.log_event("verify_post", {"outer_iter": outer_iter, **post_review})

            tuning_history.append({
                "outer_iter": outer_iter,
                "proposal": approved_proposal,
                "recommendation": post_review["recommendation"],
            })

            if post_review["recommendation"] == "rollback":
                state.rollback()
                judge_result = new_judge_result
                continue

            if new_judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, new_judge_result)

            judge_result = new_judge_result

        return _final_result("FAIL", state, MAX_OUTER_ITERATIONS, judge_result, failure_reason="max iterations reached")
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result, failure_reason=f"agent execution error: {exc}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: PASS (all tests, including the 2 new ones — 7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "fix: return a clean FAIL result instead of crashing when an agent backend errors out"
```

---

### Task 6: CLI wiring for `--agent-backend`

**Files:**
- Modify: `src/analogcoder/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `ClaudeSDKBackend` (Task 2), `OpenAICompatibleBackend` (Task 3), the updated agent function signatures (Task 4).
- Produces: `_build_agent_backend(args) -> AgentBackend` in `analogcoder.cli`, used by `_run()`.

- [ ] **Step 1: Write the failing tests**

Replace the contents of `tests/unit/test_cli.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.cli import _build_agent_backend, _run, build_arg_parser


def test_arg_parser_requires_netlist_and_spec():
    parser = build_arg_parser()
    args = parser.parse_args(["--netlist", "n.cir", "--spec", "s.yaml"])
    assert args.netlist == "n.cir"
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"
    assert args.agent_backend == "claude"


def test_build_agent_backend_returns_claude_backend_by_default():
    parser = build_arg_parser()
    args = parser.parse_args(["--netlist", "n.cir", "--spec", "s.yaml"])
    backend = _build_agent_backend(args)
    assert isinstance(backend, ClaudeSDKBackend)


def test_build_agent_backend_returns_openai_compatible_backend_when_configured():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--netlist", "n.cir",
            "--spec", "s.yaml",
            "--agent-backend", "openai-compatible",
            "--llm-base-url", "http://local",
            "--llm-model", "glm-5.2",
        ]
    )
    backend = _build_agent_backend(args)
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url == "http://local"
    assert backend.model == "glm-5.2"
    assert backend.api_key_env == "LOCAL_LLM_API_KEY"


def test_build_agent_backend_raises_when_openai_compatible_missing_config():
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--netlist", "n.cir", "--spec", "s.yaml", "--agent-backend", "openai-compatible"]
    )
    with pytest.raises(ValueError):
        _build_agent_backend(args)


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    netlist_path = tmp_path / "netlist.cir"
    netlist_path.write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\nanalyses: [\"ac\"]\ncontrol_block: |\n  .control\n  .endc\ncriteria: []\n"
    )

    fake_result = {
        "status": "PASS",
        "final_netlist_path": str(tmp_path / "runs" / "r1" / "netlist_v0.cir"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--netlist", str(netlist_path), "--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)):
        result = await _run(args)

    assert result == fake_result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: FAIL — `_build_agent_backend` does not exist yet, `--agent-backend` is not a recognized argument.

- [ ] **Step 3: Update `cli.py`**

Replace the contents of `src/analogcoder/cli.py`:

```python
import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.analyzer import analyze_netlist
from analogcoder.agents.backend import AgentBackend
from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.report import write_report_md, write_result_json
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analogcoder")
    parser.add_argument("--netlist", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--simulator", choices=["ngspice"], default="ngspice")
    parser.add_argument("--agent-backend", choices=["claude", "openai-compatible"], default="claude")
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--run-dir", default=None)
    return parser


def _build_agent_backend(args) -> AgentBackend:
    if args.agent_backend == "claude":
        return ClaudeSDKBackend()
    if not args.llm_base_url or not args.llm_model:
        raise ValueError("--llm-base-url and --llm-model are required when --agent-backend=openai-compatible")
    return OpenAICompatibleBackend(base_url=args.llm_base_url, api_key_env="LOCAL_LLM_API_KEY", model=args.llm_model)


async def _run(args) -> dict:
    with open(args.netlist) as f:
        netlist_text = f.read()
    spec = load_spec(args.spec)

    run_dir = args.run_dir or os.path.join("runs", uuid.uuid4().hex[:8])
    state = RunState(run_dir=run_dir)
    sim_backend = NgspiceBackend()
    agent_backend = _build_agent_backend(args)

    async def simulate_fn(current_netlist_text, spec_arg):
        return await agent_simulate(state.current_netlist_path(), spec_arg.control_block, sim_backend, agent_backend)

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.criteria, agent_backend)

    async def analyze_fn(netlist_text_arg):
        return await analyze_netlist(netlist_text_arg, agent_backend)

    async def tune_fn(analysis, judge_result, history, rejection_feedback):
        return await propose_tuning(analysis, judge_result, history, rejection_feedback, agent_backend)

    async def verify_pre_fn(analysis, judge_result, proposal):
        return await verify_pre(analysis, judge_result, proposal, agent_backend)

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backend)

    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
    )

    return await run_orchestration(netlist_text, spec, state, agents)


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(_run(args))

    run_dir = os.path.dirname(result["final_netlist_path"])
    write_result_json(run_dir, result)
    write_report_md(run_dir, result)

    print(f"Status: {result['status']}")
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the full unit test suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: all unit tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/cli.py tests/unit/test_cli.py
git commit -m "feat: add --agent-backend CLI flag to select ClaudeSDKBackend or OpenAICompatibleBackend"
```

---

### Task 7: Skip-gated integration test against a real local LLM server

**Files:**
- Create: `tests/integration/test_local_llm_backend.py`

**Interfaces:**
- Consumes: `OpenAICompatibleBackend` (Task 3), the updated agent signatures (Task 4), `NgspiceBackend` (existing), `benchmarks/inverting_amp` fixture (existing).

- [ ] **Step 1: Write the test**

Create `tests/integration/test_local_llm_backend.py`:

```python
import os

import pytest

from analogcoder.agents.analyzer import analyze_netlist
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOCAL_LLM_BASE_URL"),
    reason="requires LOCAL_LLM_BASE_URL (and LOCAL_LLM_API_KEY) pointed at a real OpenAI-compatible server",
)


@pytest.mark.asyncio
async def test_inverting_amp_benchmark_with_local_llm_backend(tmp_path):
    agent_backend = OpenAICompatibleBackend(
        base_url=os.environ["LOCAL_LLM_BASE_URL"],
        api_key_env="LOCAL_LLM_API_KEY",
        model=os.environ.get("LOCAL_LLM_MODEL", "glm-5.2"),
    )
    sim_backend = NgspiceBackend()
    state = RunState(run_dir=str(tmp_path))

    async def simulate_fn(netlist_text, spec_arg):
        return await agent_simulate(state.current_netlist_path(), spec_arg.control_block, sim_backend, agent_backend)

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.criteria, agent_backend)

    async def analyze_fn(netlist_text):
        return await analyze_netlist(netlist_text, agent_backend)

    async def tune_fn(analysis, judge_result, history, rejection_feedback):
        return await propose_tuning(analysis, judge_result, history, rejection_feedback, agent_backend)

    async def verify_pre_fn(analysis, judge_result, proposal):
        return await verify_pre(analysis, judge_result, proposal, agent_backend)

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backend)

    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
    )

    with open("benchmarks/inverting_amp/netlist.cir") as f:
        netlist_text = f.read()
    spec = load_spec("benchmarks/inverting_amp/spec.yaml")

    result = await run_orchestration(netlist_text, spec, state, agents)

    assert result["status"] in ("PASS", "FAIL")
```

- [ ] **Step 2: Run test to verify it skips cleanly**

Run: `.venv/bin/python -m pytest tests/integration/test_local_llm_backend.py -v`
Expected: SKIPPED (1 skipped) — `LOCAL_LLM_BASE_URL` is not set in this environment.

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `62 passed, 2 skipped` (the existing `ANTHROPIC_API_KEY`-gated end-to-end test plus this new `LOCAL_LLM_BASE_URL`-gated test)

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_local_llm_backend.py
git commit -m "test: add skip-gated integration test for OpenAICompatibleBackend against a real local server"
```
