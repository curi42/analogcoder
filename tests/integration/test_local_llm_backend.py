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

    async def tune_fn(analysis, judge_result, history, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            analysis, judge_result, history, rejection_feedback, netlist_text_arg, agent_backend
        )

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
