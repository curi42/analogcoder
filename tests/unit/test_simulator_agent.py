from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.simulator_agent import _build_simulation_tool, simulate
from analogcoder.schemas import SIMULATION_SCHEMA
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
