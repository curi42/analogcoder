from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.simulator_agent import (
    CONTROL_BLOCK_CHECK_KEY,
    SIMULATION_SYSTEM_PROMPT,
    _build_simulation_tool,
    _new_gate_record,
    simulate,
)
from analogcoder.control_block_gate import GATE_NAME
from analogcoder.schemas import SIMULATION_SCHEMA
from analogcoder.simulators.base import RawSimResult, SimulatorBackend


class FakeBackend(SimulatorBackend):
    def __init__(self):
        self.calls = []

    def run(self, netlist_path, testbench_config):
        self.calls.append(testbench_config["control_block"])
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
    tool_spec = _build_simulation_tool(
        FakeBackend(), "netlist.cir", ".control\n.endc", _new_gate_record()
    )

    result = await tool_spec.handler({"control_block": ".control\n.endc"})

    assert result["status"] == "success"
    assert result["measurements"] == {"gain_db": 20.0}


@pytest.mark.asyncio
async def test_simulate_returns_the_control_block_the_agent_settled_on():
    # 코너들이 이것을 물려받는다. 돌려주지 않으면(혹은 입력을 그대로 되돌려주면)
    # 코너는 수렴 재시도의 이득을 못 받고, 스펙 원문을 그대로 쓰게 된다.
    fake_result = {
        "measurements": {"gain_db": 20.0},
        "status": "success",
        "warnings": [],
        "control_block": ".options gmin=1e-10\n.ac dec 10 1 1meg",
    }
    fake_agent_backend = object()
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ):
        result = await simulate(
            "benchmarks/inverting_amp/netlist.cir",
            ".ac dec 10 1 1meg",
            FakeBackend(),
            fake_agent_backend,
        )

    # The original control block passed in did NOT contain the .options
    # adjustment - if simulate() echoed its input instead of the backend's
    # settled output, this would fail.
    assert result["control_block"] == ".options gmin=1e-10\n.ac dec 10 1 1meg"


def test_the_schema_declares_the_control_block_but_does_not_require_it():
    """선언은 하되 **required는 아니다** - 없으면 호출부가 폴백을 쓴다.

    required로 두면, measurements/status/warnings는 제대로 내고 control_block만
    빠뜨린 약한 모델이 검증 실패 → 수리 루프 소진 → AgentExecutionError →
    실행 전체 FAIL이 된다. 그것도 `pvt_corners`도 `corner_reduction`도 없어
    이 필드를 **아무도 읽지 않는** 스펙에서까지 그렇다. 시뮬레이터는 이
    저장소가 로컬 모델 경로에서 가장 약하다고 기록해 둔 도구 호출 에이전트다.

    폴백은 이미 있다(`corner_sim.py`의
    `agent_result.get("control_block") or tb.control_block`). required만이
    그것을 도달 불가능하게 만들고 있었다.

    **어떤 변형을 잡는가.** control_block을 required로 되돌리는 변형, 그리고
    property 선언 자체를 지우는 변형(그러면 수렴 재시도가 조정한 control block이
    코너로 전달되지 않는다).
    """
    assert "control_block" not in SIMULATION_SCHEMA["required"]
    assert SIMULATION_SCHEMA["properties"]["control_block"] == {"type": "string"}
    assert set(SIMULATION_SCHEMA["required"]) == {"measurements", "status", "warnings"}


def test_a_result_without_a_control_block_still_validates():
    """스키마가 실제로 통과시키는지를 jsonschema로 직접 확인한다 - required
    목록만 보는 단언은 스키마가 다른 방식(예: dependentRequired)으로 같은 것을
    강제해도 통과한다."""
    import jsonschema

    jsonschema.validate(
        {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []},
        SIMULATION_SCHEMA,
    )


# 폴백 자체가 실제로 동작한다는 것은
# tests/unit/test_corner_sim.py::test_the_spec_s_control_block_is_used_when_the_agent_supplies_none
# 이 이미 못박고 있다 - required를 뺀 지금에야 그 경로가 프로덕션에서 도달
# 가능해졌다.


# ---------------------------------------------------------------------------
# 신뢰 경계 - LLM이 준 control block
#
# `control_block_gate.py`가 판정 규칙을 갖고 그 짝 테스트가 규칙 자체를
# 고정한다. 여기서 고정하는 것은 **이 에이전트가 그 게이트를 실제로 두
# 자리에 붙였는가**다: 실행 직전(도구 핸들러)과 재사용 직전(반환값).
# ---------------------------------------------------------------------------

RCE_CONTROL_BLOCK = ".control\nop\nshell touch /tmp/analogcoder-pwned\n.endc"
SPEC_CONTROL_BLOCK = ".control\nac dec 10 1 1meg\nmeas ac gain_db find vdb(vout) at=1\n.endc"


@pytest.mark.asyncio
async def test_the_tool_handler_never_reaches_the_simulator_when_the_gate_rejects():
    """감사가 실증한 경로다: `.control` 안의 `shell`은 임의 명령을 돌리고
    시뮬레이션은 정상 종료한다. 게이트가 없으면 이 문자열이 그대로
    `SimulatorBackend.run`으로 간다."""
    sim_backend = FakeBackend()
    tool_spec = _build_simulation_tool(
        sim_backend, "netlist.cir", SPEC_CONTROL_BLOCK, _new_gate_record()
    )

    await tool_spec.handler({"control_block": RCE_CONTROL_BLOCK})

    assert sim_backend.calls == []


@pytest.mark.asyncio
async def test_an_accepted_control_block_reaches_the_simulator_byte_for_byte():
    """게이트는 판정만 한다 - 정규화한 문자열을 대신 넘기면 `.options` 조정이
    조용히 사라지거나 덱이 미묘하게 달라진다."""
    sim_backend = FakeBackend()
    tool_spec = _build_simulation_tool(
        sim_backend, "netlist.cir", SPEC_CONTROL_BLOCK, _new_gate_record()
    )
    adjusted = SPEC_CONTROL_BLOCK.replace(".control", ".control\n.options gmin=1e-10")

    await tool_spec.handler({"control_block": adjusted})

    assert sim_backend.calls == [adjusted]


@pytest.mark.asyncio
async def test_the_tool_handler_reports_the_rejection_so_the_agent_can_retry():
    """조용히 빈 결과를 돌려주면 에이전트는 회로가 실패했다고 읽는다.
    거부는 `status="error"`와 사유를 담은 로그로 **말해야** 한다. 그리고
    이것은 시뮬레이터가 낸 결과가 아니므로 캐시에 담기면 안 된다."""
    tool_spec = _build_simulation_tool(
        FakeBackend(), "netlist.cir", SPEC_CONTROL_BLOCK, _new_gate_record()
    )

    result = await tool_spec.handler({"control_block": RCE_CONTROL_BLOCK})

    assert result["status"] == "error"
    assert result["measurements"] == {}
    assert "shell" in result["raw_log"]
    assert result["cacheable"] is False
    assert result["failure_kind"] == "control_block_rejected"


@pytest.mark.asyncio
async def test_a_tool_level_rejection_reaches_the_result_record():
    record = _new_gate_record()
    tool_spec = _build_simulation_tool(
        FakeBackend(), "netlist.cir", SPEC_CONTROL_BLOCK, record
    )

    await tool_spec.handler({"control_block": SPEC_CONTROL_BLOCK})
    await tool_spec.handler({"control_block": RCE_CONTROL_BLOCK})

    assert record["tool_calls"] == 2
    assert [rejection["reason"] for rejection in record["tool_rejections"]] == [
        "command_not_allowed"
    ]


@pytest.mark.asyncio
async def test_the_simulate_result_always_carries_the_gate_record():
    """게이트가 아무것도 안 잡았을 때 어떻게 보이는가. 이 키가 없으면
    "검사했고 통과"와 "검사가 사라졌다"가 같아 보인다. cli.py의 simulate_fn이
    이 결과를 `by_testbench`에 그대로 싣고 orchestrator가 `simulation`
    이벤트로 `history.jsonl`에 쓰므로, 여기 실으면 실행 기록에 남는다."""
    fake_result = {
        "measurements": {"gain_db": 20.0},
        "status": "success",
        "warnings": [],
        "control_block": SPEC_CONTROL_BLOCK,
    }
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ):
        result = await simulate(
            "benchmarks/inverting_amp/netlist.cir",
            SPEC_CONTROL_BLOCK,
            FakeBackend(),
            object(),
        )

    record = result[CONTROL_BLOCK_CHECK_KEY]
    assert record["gate"] == GATE_NAME
    assert record["tool_calls"] == 0
    assert record["tool_rejections"] == []
    assert record["returned"]["accepted"] is True
    assert record["returned"]["lines_checked"] == 4
    assert record["returned_dropped"] is False


@pytest.mark.asyncio
async def test_a_rejected_returned_control_block_is_dropped_so_the_corner_fallback_fires():
    """반환된 control block은 `corner_sim`이 **이후 모든 코너에 그대로
    재사용**한다. 그러니 도구 게이트만으로는 부족하다 - 에이전트가 무해한
    블록을 돌리고 유해한 블록을 보고할 수 있다.

    거부는 실행을 끝내지 않는다. 필드를 떨어뜨리면
    `agent_result.get("control_block") or tb.control_block`이라는 이미 있는
    폴백이 발화해 스펙 원문이 쓰인다 - "실패한 확대는 확대하지 않은 것보다
    나쁘면 안 된다"와 같은 정책이다."""
    fake_result = {
        "measurements": {"gain_db": 20.0},
        "status": "success",
        "warnings": [],
        "control_block": RCE_CONTROL_BLOCK,
    }
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ):
        result = await simulate(
            "benchmarks/inverting_amp/netlist.cir",
            SPEC_CONTROL_BLOCK,
            FakeBackend(),
            object(),
        )

    assert "control_block" not in result
    record = result[CONTROL_BLOCK_CHECK_KEY]
    assert record["returned"]["accepted"] is False
    assert record["returned"]["reason"] == "command_not_allowed"
    assert record["returned_dropped"] is True


@pytest.mark.asyncio
async def test_a_returned_control_block_that_rewrites_a_meas_line_is_dropped():
    """측정 무결성. 회로를 안 고치고 측정을 고치는 경로이며,
    `check_stimulus_untouched`가 막으려던 것과 같은 부류다."""
    fake_result = {
        "measurements": {"gain_db": 20.0},
        "status": "success",
        "warnings": [],
        "control_block": SPEC_CONTROL_BLOCK.replace("at=1", "at=1e6"),
    }
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ):
        result = await simulate(
            "benchmarks/inverting_amp/netlist.cir",
            SPEC_CONTROL_BLOCK,
            FakeBackend(),
            object(),
        )

    assert "control_block" not in result
    assert result[CONTROL_BLOCK_CHECK_KEY]["returned"]["reason"] == "measurements_altered"


@pytest.mark.asyncio
async def test_the_record_distinguishes_no_control_block_from_a_checked_one():
    """`control_block`은 스키마상 required가 아니다 - 약한 모델은 그것을
    빠뜨린다. 그 경우 `returned`는 `None`이지 "통과한 판정"이 아니다."""
    fake_result = {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ):
        result = await simulate(
            "benchmarks/inverting_amp/netlist.cir",
            SPEC_CONTROL_BLOCK,
            FakeBackend(),
            object(),
        )

    assert result[CONTROL_BLOCK_CHECK_KEY]["returned"] is None
    assert result[CONTROL_BLOCK_CHECK_KEY]["returned_dropped"] is False


def test_the_prompt_states_the_boundary_the_gate_enforces():
    """게이트와 그것을 비추는 프롬프트가 어긋나면 belt-and-braces가 덫이
    된다는 것을 CLAUDE.md가 기록한다(`verify_pre`의 param 규칙). 프롬프트는
    게이트보다 **엄격한** 쪽이어야 하고, 여기서는 "`.options`만"이 그것이다."""
    prompt = " ".join(SIMULATION_SYSTEM_PROMPT.lower().split())

    assert ".options" in prompt
    assert "shell" in prompt
    assert "meas" in prompt
