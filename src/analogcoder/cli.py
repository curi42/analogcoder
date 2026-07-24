import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.analyzer import analyze_netlist
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
    parser.add_argument("--run-dir", default=None)
    return parser


async def _run(args) -> dict:
    with open(args.netlist) as f:
        netlist_text = f.read()
    spec = load_spec(args.spec)

    run_dir = args.run_dir or os.path.join("runs", uuid.uuid4().hex[:8])
    state = RunState(run_dir=run_dir)
    backend = NgspiceBackend()

    async def simulate_fn(current_netlist_text, spec_arg):
        return await agent_simulate(state.current_netlist_path(), spec_arg.control_block, backend)

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.criteria)

    agents = OrchestratorAgents(
        analyze=analyze_netlist,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=propose_tuning,
        verify_pre=verify_pre,
        verify_post=verify_post,
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
