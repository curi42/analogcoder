import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.analyzer import analyze_netlist
from analogcoder.agents.backend import AgentBackend
from analogcoder.agents.backends.claude_sdk import DEFAULT_CLAUDE_MODEL, ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.netlist import resolve_includes
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.report import write_report_md, write_result_json
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analogcoder")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--simulator", choices=["ngspice"], default="ngspice")
    parser.add_argument("--agent-backend", choices=["claude", "openai-compatible"], default="claude")
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    parser.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="AGENT=MODEL",
        help=f"override one agent's model; AGENT is one of {', '.join(AGENT_NAMES)}",
    )
    parser.add_argument("--run-dir", default=None)
    return parser


AGENT_NAMES = ("analyzer", "simulator", "judge", "tuner", "verifier")


def _build_agent_backend(args, model: str | None = None) -> AgentBackend:
    if args.agent_backend == "claude":
        return ClaudeSDKBackend(model=model or getattr(args, "claude_model", DEFAULT_CLAUDE_MODEL))
    if not args.llm_base_url or not args.llm_model:
        raise ValueError("--llm-base-url and --llm-model are required when --agent-backend=openai-compatible")
    return OpenAICompatibleBackend(base_url=args.llm_base_url, api_key_env="LOCAL_LLM_API_KEY", model=args.llm_model)


def _build_agent_backends(args) -> dict[str, AgentBackend]:
    """One backend instance per agent, so a single agent can be dropped to a
    weaker model independently. Agent modules stay untouched - cli.py already
    injects a backend per agent, so the model choice lives entirely here."""
    overrides = {}
    for raw in getattr(args, "agent_model", []) or []:
        name, _, model = raw.partition("=")
        if name not in AGENT_NAMES:
            raise ValueError(f"unknown agent '{name}' in --agent-model; expected one of {list(AGENT_NAMES)}")
        overrides[name] = model

    return {name: _build_agent_backend(args, overrides.get(name)) for name in AGENT_NAMES}


async def _run(args) -> dict:
    spec = load_spec(args.spec)
    # Includes are absolutized here, at the one point netlist text enters the
    # system, because everything downstream relocates that text away from the
    # directory it was read from (RunState stages it into the run dir, then
    # NgspiceBackend stages that into a temp dir) and a bare relative
    # .include stops resolving the moment it moves.
    initial_netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            initial_netlist_texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))

    run_dir = args.run_dir or os.path.join("runs", uuid.uuid4().hex[:8])
    state = RunState(run_dir=run_dir, testbench_names=[tb.name for tb in spec.testbenches])
    sim_backend = NgspiceBackend()
    agent_backends = _build_agent_backends(args)

    async def simulate_fn(netlist_texts, spec_arg):
        merged_measurements = {}
        by_testbench = {}
        paths = state.current_netlist_paths()
        for tb in spec_arg.testbenches:
            result = await agent_simulate(paths[tb.name], tb.control_block, sim_backend, agent_backends["simulator"])
            merged_measurements.update(result["measurements"])
            by_testbench[tb.name] = result
        return {"measurements": merged_measurements, "by_testbench": by_testbench}

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.all_criteria, agent_backends["judge"])

    async def analyze_fn(netlist_text_arg):
        return await analyze_netlist(netlist_text_arg, agent_backends["analyzer"])

    async def tune_fn(analysis, judge_result, history, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            analysis, judge_result, history, rejection_feedback, netlist_text_arg, agent_backends["tuner"]
        )

    async def verify_pre_fn(analysis, judge_result, proposal, netlist_text_arg):
        return await verify_pre(analysis, judge_result, proposal, netlist_text_arg, agent_backends["verifier"])

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backends["verifier"])

    async def propose_topology_fn(analysis, judge_result, available_topologies, rejection_feedback):
        return await propose_topology_swap(
            analysis, judge_result, available_topologies, rejection_feedback, agent_backends["tuner"]
        )

    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=propose_topology_fn,
    )

    if spec.pvt_corners is not None:
        baseline_sweep = run_full_pvt_sweep(initial_netlist_texts, spec, sim_backend)
        state.log_event("pvt_baseline_sweep", baseline_sweep)

    result = await run_orchestration(initial_netlist_texts, spec, state, agents)

    if spec.pvt_corners is not None:
        final_netlist_texts = state.current_netlist_texts()
        final_sweep = run_full_pvt_sweep(final_netlist_texts, spec, sim_backend)
        state.log_event("pvt_final_sweep", final_sweep)
        result["pvt_sweep"] = final_sweep
        if not final_sweep["overall_pass"]:
            result["status"] = "FAIL"
            result["failure_reason"] = f"final PVT sweep failed: {final_sweep['summary']}"

    return result


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(_run(args))

    run_dir = result["run_dir"]
    write_result_json(run_dir, result)
    write_report_md(run_dir, result)

    print(f"Status: {result['status']}")
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
