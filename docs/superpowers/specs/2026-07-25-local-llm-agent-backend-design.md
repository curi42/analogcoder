# 로컬 LLM 에이전트 백엔드 추상화 설계

## 배경 및 목표

현재 analogcoder의 5개 에이전트(분석/시뮬레이션/판정/튜닝/검증)는 모두 `claude-agent-sdk`를 통해 Claude Code 구독으로 실행된다. 향후 내부망 환경에서는 인터넷/Claude 접근이 없고, 대신 내부망 LLM 게이트웨이(엔드포인트 URL + API 토큰 + 모델명, 예: `http://<내부망-LLM-게이트웨이>` + `glm-5.2`)를 통해 상대적으로 성능이 낮은 로컬/내부망 LLM으로 동일한 에이전트 팀을 돌려야 한다.

이번 작업의 목표는 에이전트 실행 로직을 특정 LLM 제공자(claude-agent-sdk)에 종속되지 않도록 추상화하고, 내부망 API와 동일한 접속 방식(URL + 토큰 + 모델명, OpenAI 호환 Chat Completions 패턴)을 쓰는 백엔드 하나를 지금 구현해 실제로 검증 가능하게 만드는 것이다. 내부망 API의 정확한 스펙은 아직 미정이므로, 그 자체를 지금 구현하지는 않는다.

### 범위

- `AgentBackend` 추상 인터페이스 도입 (`SimulatorBackend`와 동일한 어댑터 패턴)
- 기존 claude-agent-sdk 로직을 `ClaudeSDKBackend`로 이전 (동작 변경 없음, Claude Code 구독 기반 저비용 실행 유지)
- OpenAI 호환 Chat Completions API를 쓰는 `OpenAICompatibleBackend` 신규 구현 (URL + 토큰(env var) + 모델명 설정, 도구 호출 루프, 구조화 출력 검증/복구 재시도 포함) — Ollama 등 로컬 서버로 지금 검증 가능
- 약한 모델에서 더 잦아질 것으로 예상되는 "구조화 출력 실패" 상황에서 orchestrator/cli가 크래시하지 않고 깨끗한 FAIL 결과를 남기도록 수정

### 명시적으로 범위 밖

- 내부망 API 연동 (스펙 미정, 추후 별도 작업)
- OpenAICompatibleBackend가 아닌 다른 프로토콜(gRPC 등)의 백엔드
- 프롬프트 자체의 모델별 튜닝 (예: glm-5.2 전용 프롬프트 최적화)

## 아키텍처 원칙

에이전트 실행 로직(프롬프트, 스키마, 도구 정의)과 "그 프롬프트를 어떤 LLM에 어떻게 보내는가"를 분리한다. 5개 에이전트 모듈은 `AgentBackend` 인터페이스에만 의존하고, 어떤 백엔드가 실제로 붙어있는지 몰라도 된다. 이는 기존 `SimulatorBackend`(ngspice→HSPICE 교체 대비) 설계와 동일한 원칙이다.

```
┌───────────────────────────────────────────────────────────┐
│  5개 에이전트 모듈 (analyzer, judge, simulator_agent,          │
│                     tuner, verifier)                        │
│  - 프롬프트/스키마/도구(ToolSpec) 정의만 담당                    │
└───────────────────────────────────────────────────────────┘
                          │ backend: AgentBackend
                          ▼
              ┌───────────────────────┐
              │   agent_runtime.py     │  run_agent(): 백엔드 위임 +
              │                        │  공용 스키마 검증(안전장치)
              └───────────────────────┘
                 │                    │
                 ▼                    ▼
      ┌────────────────────┐   ┌──────────────────────────┐
      │  ClaudeSDKBackend    │   │  OpenAICompatibleBackend  │
      │  (claude-agent-sdk)  │   │  (httpx, URL+토큰+모델명)  │
      └────────────────────┘   └──────────────────────────┘
```

## 컴포넌트 상세

### `AgentBackend` / `ToolSpec` (`src/analogcoder/agents/backend.py`)

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    handler: Callable[[dict], Awaitable[dict]]

class AgentBackend(ABC):
    @abstractmethod
    async def run(
        self, system_prompt: str, user_prompt: str, output_schema: dict, tools: list[ToolSpec]
    ) -> dict: ...
```

`ToolSpec`은 특정 LLM 프로토콜에 묶이지 않은 도구 선언이다. `judge.py`/`simulator_agent.py`는 지금처럼 claude-agent-sdk의 `@tool`/MCP 서버를 직접 만드는 대신 `ToolSpec` 리스트를 구성해 넘긴다.

### `ClaudeSDKBackend` (`src/analogcoder/agents/backends/claude_sdk.py`)

현재 `_sdk_utils.py`가 하던 일을 그대로 옮긴다: `ToolSpec` 리스트를 claude-agent-sdk의 `@tool` + `create_sdk_mcp_server`로 변환하고, `query()`를 호출해 `ResultMessage.structured_output`을 반환한다. 동작은 기존과 100% 동일하며, Claude Code 구독 기반 실행(별도 API 과금 없음)이 그대로 유지된다.

### `OpenAICompatibleBackend` (`src/analogcoder/agents/backends/openai_compatible.py`)

```python
class OpenAICompatibleBackend(AgentBackend):
    def __init__(self, base_url: str, api_key_env: str, model: str): ...
```

- `api_key_env`는 토큰이 담긴 환경변수의 "이름"만 받는다 (예: `LOCAL_LLM_API_KEY`). 실제 토큰 값은 매 호출 시점에 `os.environ`에서 읽어 `Authorization: Bearer {token}` 헤더로만 사용하고, 로그·에러 메시지에는 절대 노출하지 않는다.

**도구 호출 루프**
1. `POST {base_url}/chat/completions`에 `messages=[system, user]`, `tools=[ToolSpec → OpenAI function-calling 포맷]` 전송
2. 응답에 `tool_calls`가 있으면 각각 대응하는 `ToolSpec.handler`를 실행하고, 결과를 `role=tool` 메시지로 추가해 재요청
3. `tool_calls` 없는 최종 응답이 오면 종료. `MAX_TOOL_LOOP_TURNS = 6`(모듈 상수) 초과 시 `AgentExecutionError`

**구조화 출력 검증 및 복구 재시도**
1. 최종 응답 텍스트를 JSON으로 파싱하고 `jsonschema.validate(candidate, output_schema)`로 검증
2. 파싱 실패 또는 스키마 위반 시, 에러 내용을 포함한 복구 프롬프트("이전 응답이 스키마에 맞지 않았습니다: `<에러>`. JSON만 다시 응답하세요")를 추가해 재요청
3. `MAX_STRUCTURED_OUTPUT_REPAIRS = 2`회까지 재시도. 그래도 실패하면 `AgentExecutionError`

### `agent_runtime.py` (기존 `_sdk_utils.py`에서 이름 변경)

```python
async def run_agent(
    system_prompt: str,
    user_prompt: str,
    output_schema: dict,
    backend: AgentBackend,
    tools: list[ToolSpec] | None = None,
) -> dict:
```

선택된 `backend.run(...)`을 호출하고, 반환값을 `jsonschema.validate()`로 한 번 더 검증하는 공용 안전장치를 둔다 (백엔드가 이미 자체 검증을 했더라도, 오케스트레이터가 "정해진 JSON 필드만 신뢰한다"는 기존 아키텍처 원칙을 코드 레벨에서 보장). `AgentExecutionError`는 이름 변경 없이 그대로 유지 (기존 5개 에이전트 모듈이 이미 이 클래스를 import).

### 5개 에이전트 모듈 변경

각 공개 함수가 `backend: AgentBackend` 키워드 인자를 추가로 받는다. 시그니처 예:

```python
async def analyze_netlist(netlist_text: str, backend: AgentBackend) -> dict: ...
async def judge_measurements(measurements: dict, criteria: list[Criterion], backend: AgentBackend) -> dict: ...
async def simulate(netlist_path: str, control_block: str, backend: AgentBackend, sim_backend: SimulatorBackend) -> dict: ...
async def propose_tuning(analysis: dict, judge_result: dict, history: list[dict], rejection_feedback: str | None, backend: AgentBackend) -> dict: ...
async def verify_pre(analysis: dict, judge_result: dict, proposal: dict, backend: AgentBackend) -> dict: ...
async def verify_post(prev_judge_result: dict, new_judge_result: dict, applied_changes: list[dict], backend: AgentBackend) -> dict: ...
```

(`simulate`는 시뮬레이터 어댑터(`SimulatorBackend`)와 이번에 새로 추가되는 LLM 어댑터(`AgentBackend`)를 둘 다 받으므로 파라미터명을 `sim_backend`/`backend`로 구분한다.)

`judge.py`/`simulator_agent.py`는 `_build_judge_tool`/`_build_simulation_tool`이 MCP 서버 대신 `ToolSpec`을 반환하도록 바뀐다.

## CLI 및 설정 연동

```
--agent-backend {claude,openai-compatible}   기본값: claude
--llm-base-url URL      (--agent-backend openai-compatible 선택 시 필수)
--llm-model NAME        (--agent-backend openai-compatible 선택 시 필수)
```

API 토큰은 CLI 인자로 받지 않는다. 고정된 환경변수 이름 `LOCAL_LLM_API_KEY`를 문서화하고, `OpenAICompatibleBackend`가 그 이름으로 값을 읽는다.

`cli.py`의 `_run()`은 선택된 `AgentBackend` 인스턴스 하나를 만들어, `OrchestratorAgents`를 구성하는 6개 클로저(`analyze_fn`, `simulate_fn`, `judge_fn`, `tune_fn`, `verify_pre_fn`, `verify_post_fn`) 모두가 이를 캡처해서 각 에이전트 함수에 전달하도록 한다. `orchestrator.py`의 `OrchestratorAgents` 데이터클래스와 `run_orchestration()`의 제어 흐름 자체는 변경하지 않는다 (이미 검증된 로직 보존).

## 에이전트 실패 시 크래시 방지

**현재 문제**: `AgentExecutionError`가 재시도 후에도 해소되지 않으면 `run_orchestration()` 밖으로 전파되어 `cli.py`의 `main()`이 트레이스백과 함께 종료되고, `result.json`/`report.md`가 남지 않는다. 약한 로컬 모델에서는 이 실패가 더 잦아질 것으로 예상된다.

**수정**: `run_orchestration()`의 본문(초기 netlist 버전 push 이후) 전체를 `try/except AgentExecutionError`로 감싼다. 예외를 잡으면 `_final_result("FAIL", state, iterations_used=<에러 시점까지 완료된 outer_iter, 없으면 0>, judge_result=<마지막으로 성공한 판정 결과, 없으면 None>, failure_reason=f"agent execution error: {exc}")`를 반환한다. `_final_result`는 `judge_result=None`일 때 `final_criteria: []`를 넣도록 보강한다. `cli.py`는 변경 불필요 — `run_orchestration`이 항상 정상적인 result dict를 반환하므로 기존 `write_result_json`/`write_report_md`/종료 코드 흐름이 이 경로도 그대로 커버한다.

## 테스트 전략

**단위 테스트**
- `ClaudeSDKBackend`: 기존 `test_sdk_utils.py`의 테스트를 이 클래스 대상으로 이전, 동작 무변경 확인
- `OpenAICompatibleBackend`: httpx 호출을 mock하여 (1) 도구 호출 루프 정상 동작, (2) 스키마 위반 → 복구 재시도 → 성공, (3) 복구 재시도 소진 → `AgentExecutionError`, (4) 도구 루프 턴 수 초과 → `AgentExecutionError`, (5) 토큰이 환경변수에서만 읽히고 로그에 노출되지 않음을 검증
- 5개 에이전트 모듈: fake `AgentBackend`를 주입해 `judge.py`/`simulator_agent.py`가 올바른 `ToolSpec`(이름/파라미터 스키마/핸들러)을 구성하는지 검증
- `orchestrator.py`: fake 에이전트가 `AgentExecutionError`를 던질 때 `run_orchestration`이 크래시하지 않고 FAIL 결과를 정상 반환하는지 검증

**통합 테스트 (선택적, skip-gated)**
- `test_end_to_end.py`의 `ANTHROPIC_API_KEY` 스킵 패턴과 동일하게, `LOCAL_LLM_BASE_URL` 환경변수가 없으면 스킵되는 테스트를 추가. Ollama 등 실제 OpenAI 호환 로컬 서버를 띄워두면 이 테스트로 약한 로컬 모델이 전체 파이프라인(분석→시뮬레이션→판정)을 실제로 통과하는지 지금 검증할 수 있다.

## 향후 확장

- 내부망 API 스펙이 확정되면, OpenAI 호환이면 `OpenAICompatibleBackend`를 그대로 재사용하고, 아니면 `AgentBackend`를 구현하는 백엔드를 하나 더 추가한다 (기존 5개 에이전트 모듈은 무변경)
- 도구 호출을 지원하지 않는 백엔드를 위한 프롬프트 기반 폴백(도구 없이 텍스트 지시만으로 스키마 출력 유도)은 실제 필요성이 확인되면 별도로 설계한다
