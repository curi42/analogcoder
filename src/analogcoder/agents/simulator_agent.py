from dataclasses import asdict

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, ToolSpec
from analogcoder.control_block_gate import GATE_NAME, check_control_block
from analogcoder.schemas import SIMULATION_SCHEMA
from analogcoder.simulators.base import RawSimResult, SimulatorBackend

# 이 에이전트가 낸 결과에 게이트 기록이 실리는 키. cli.py의 simulate_fn이
# 결과를 `by_testbench[<tb>]`에 그대로 싣고, orchestrator가 그것을
# `simulation` 이벤트로 `history.jsonl`에 쓴다 - 그래서 이 키 하나로
# 게이트의 판정이 다른 게이트들과 같은 급으로 실행 기록에 남는다.
CONTROL_BLOCK_CHECK_KEY = "control_block_check"

# 게이트가 거부한 실행의 failure_kind. `simulators/base.FAILURE_KINDS`에
# **일부러 등록하지 않는다** - 미분류 종류는 `is_cacheable`가 닫으므로
# (fail-closed) 이 실패가 내용 주소 캐시에 굳지 않는다. 그리고 이것은
# 시뮬레이터가 낸 결과가 아니라 시뮬레이터에 **가기 전에** 난 거부라
# 그 표에 들어갈 사실도 아니다.
CONTROL_BLOCK_REJECTED_KIND = "control_block_rejected"

SIMULATION_SYSTEM_PROMPT = """You are a SPICE simulation specialist. You are given a
netlist file path and a target spec's control block (analysis + measure directives).
Call the run_simulation tool to execute the simulation. If it reports a
convergence_failure, you may retry by adjusting the .options portion of the control
block (e.g. gmin stepping, method=gear), up to 2 extra attempts, before reporting
the final result via the structured output schema. Never modify component values.
Always report the control block you actually used in your final structured
output's control_block field - the original if you did not change it, or the
adjusted one if you retried. Other simulations reuse it verbatim.

The control block is checked by a deterministic gate before it is executed and
again before it is reused. The .options lines are the only ones you may add,
change or drop: every other line - meas, let, alter, set, and the analysis line
itself - must come through unchanged, in the same order. Do not add any command
outside the analysis/measure/print/set/.options vocabulary the given control
block already uses; in particular shell, system, sh, source, write, wrdata,
echo and cd are rejected, as are .include, .temp and .end. A rejected control
block is not simulated, and a rejected report is discarded in favour of the
original - so an edit outside this boundary costs you the retry and gains
nothing."""


def _new_gate_record() -> dict:
    """게이트가 이번 시뮬레이션에서 무엇을 했는지 - **아무것도 안 했을 때도
    낼 수 있는 모양**이다.

    `tool_calls=0, tool_rejections=[], returned=None`은 "게이트는 돌았고 볼
    것이 없었다"이고, 키 자체의 부재는 "게이트가 없다"이다. 이 저장소에서
    조용히 무력해진 검사가 아홉 번이고 아홉 번 다 실행 기록만으로는 알아챌
    수 없었다.
    """
    return {
        "gate": GATE_NAME,
        "tool_calls": 0,
        "tool_rejections": [],
        "returned": None,
        "returned_dropped": False,
    }


def _build_simulation_tool(
    sim_backend: SimulatorBackend,
    netlist_path: str,
    reference_control_block: str,
    record: dict,
) -> ToolSpec:
    """도구 핸들러 = **실행 표면의 신뢰 경계**.

    `reference_control_block`은 사람이 쓴 스펙 원문이다. 다른 넷 게이트들이
    "적용 전, LLM 호출 전"에 있는 것과 달리 이 문자열은 LLM이 **부르면서**
    건네주는 것이라 그 자리가 없다 - 여기가 그것이 프로세스를 띄우기 직전의
    마지막 지점이다.
    """

    async def _run(args: dict) -> dict:
        candidate = args["control_block"]
        record["tool_calls"] += 1
        verdict = check_control_block(candidate, reference_control_block)
        if not verdict.accepted:
            record["tool_rejections"].append(verdict.as_event())
            # 조용히 빈 결과를 돌려주면 에이전트는 회로가 실패했다고 읽는다.
            # 사유를 raw_log에 실어 재시도가 무엇을 고쳐야 하는지 말해 준다 -
            # area/refdes 게이트가 튜너에게 되먹이는 피드백과 같은 성격이다.
            rejected = RawSimResult(
                status="error",
                measurements={},
                raw_log=(
                    f"control block rejected by the {GATE_NAME} gate "
                    f"[{verdict.reason}]: {verdict.detail}"
                ),
                warnings=[],
                cacheable=False,
                failure_kind=CONTROL_BLOCK_REJECTED_KIND,
            )
            return asdict(rejected)
        result = sim_backend.run(netlist_path, {"control_block": candidate})
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
    record = _new_gate_record()
    sim_tool = _build_simulation_tool(sim_backend, netlist_path, control_block, record)
    user_prompt = f"Netlist path: {netlist_path}\nControl block:\n{control_block}"
    result = await run_agent(
        system_prompt=SIMULATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=SIMULATION_SCHEMA,
        backend=backend,
        tools=[sim_tool],
    )

    # 도구 게이트만으로는 부족하다: 에이전트가 무해한 블록을 **돌리고** 유해한
    # 블록을 **보고할** 수 있다. 그리고 보고된 쪽이 corner_sim에서 이후 모든
    # 코너에 그대로 재사용된다.
    #
    # 거부해도 실행을 끝내지 않는다. 필드를 떨어뜨리면 이미 있는 폴백
    # (`agent_result.get("control_block") or tb.control_block`)이 발화해 스펙
    # 원문이 쓰인다 - "실패한 확대는 확대하지 않은 것보다 나쁘면 안 된다"와
    # 같은 정책이고, 재사용이 이 필드의 유일한 용도이므로 떨어뜨리는 것이
    # 이 자리의 완전한 조치다.
    returned = result.get("control_block")
    if returned is not None:
        verdict = check_control_block(returned, control_block)
        record["returned"] = verdict.as_event()
        if not verdict.accepted:
            del result["control_block"]
            record["returned_dropped"] = True

    # 결과 dict을 **그 자리에서** 늘린다. 새 dict을 만들면 호출부가 들고 있는
    # 객체와 갈라진다.
    result[CONTROL_BLOCK_CHECK_KEY] = record
    return result
