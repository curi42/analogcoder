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
