# tests/integration/test_end_to_end.py
import os

import pytest

from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "inverting_amp")


@pytest.mark.asyncio
async def test_inverting_amp_benchmark_passes_immediately(tmp_path, monkeypatch):
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))
    with open(os.path.join(BENCHMARK_DIR, "netlist.cir")) as f:
        netlist_text = f.read()

    state = RunState(run_dir=str(tmp_path))
    backend = NgspiceBackend()

    # The real simulation agent needs a live netlist path on disk, which only
    # exists once the orchestrator has pushed a version — so route it through
    # state.current_netlist_path() exactly like the CLI does in Task 16.
    async def simulate_fn(current_netlist_text, spec_arg):
        return await agent_simulate(state.current_netlist_path(), spec_arg.control_block, backend)

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.criteria)

    async def fake_analyze(netlist_text_arg):
        return {"circuit_type": "inverting amplifier", "stages": [], "component_roles": {}, "tunable_params": []}

    # This benchmark is designed to pass on the first simulation, so tune/verify
    # should never be invoked; make that an explicit assertion by failing loudly
    # if they are.
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("tuning/verification should not run for a passing benchmark")

    agents = OrchestratorAgents(
        analyze=fake_analyze,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=fail_if_called,
        verify_pre=fail_if_called,
        verify_post=fail_if_called,
    )

    # These two calls hit the real Claude Agent SDK (simulate_fn -> agent_simulate,
    # judge_fn -> judge_measurements). If ANTHROPIC_API_KEY / SDK auth is not
    # configured in this environment, skip rather than fail the suite.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("requires a configured Claude Agent SDK credential to run live agents")

    result = await run_orchestration(netlist_text, spec, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert result["final_criteria"][0]["pass"] is True
