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
