import json
from dataclasses import asdict

from claude_agent_sdk import create_sdk_mcp_server, tool

from analogcoder.agents._sdk_utils import run_agent
from analogcoder.schemas import SIMULATION_SCHEMA
from analogcoder.simulators.base import SimulatorBackend

SIMULATION_SYSTEM_PROMPT = """You are a SPICE simulation specialist. You are given a
netlist file path and a target spec's control block (analysis + measure directives).
Call the run_simulation tool to execute the simulation. If it reports a
convergence_failure, you may retry by adjusting the .options portion of the control
block (e.g. gmin stepping, method=gear), up to 2 extra attempts, before reporting
the final result via the structured output schema. Never modify component values."""


def _build_simulation_tool(backend: SimulatorBackend, netlist_path: str):
    @tool(
        "run_simulation",
        "Run the netlist through the configured simulator backend",
        {"control_block": str},
    )
    async def _run(args):
        result = backend.run(netlist_path, {"control_block": args["control_block"]})
        return {"content": [{"type": "text", "text": json.dumps(asdict(result))}]}

    return _run


async def simulate(netlist_path: str, control_block: str, backend: SimulatorBackend) -> dict:
    sim_tool = _build_simulation_tool(backend, netlist_path)
    server = create_sdk_mcp_server("simulation", tools=[sim_tool])
    user_prompt = f"Netlist path: {netlist_path}\nControl block:\n{control_block}"
    return await run_agent(
        system_prompt=SIMULATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=SIMULATION_SCHEMA,
        mcp_servers={"simulation": server},
        allowed_tools=["mcp__simulation__run_simulation"],
    )
