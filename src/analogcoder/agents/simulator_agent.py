from dataclasses import asdict

from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, ToolSpec
from analogcoder.schemas import SIMULATION_SCHEMA
from analogcoder.simulators.base import SimulatorBackend

SIMULATION_SYSTEM_PROMPT = """You are a SPICE simulation specialist. You are given a
netlist file path and a target spec's control block (analysis + measure directives).
Call the run_simulation tool to execute the simulation. If it reports a
convergence_failure, you may retry by adjusting the .options portion of the control
block (e.g. gmin stepping, method=gear), up to 2 extra attempts, before reporting
the final result via the structured output schema. Never modify component values."""


def _build_simulation_tool(sim_backend: SimulatorBackend, netlist_path: str) -> ToolSpec:
    async def _run(args: dict) -> dict:
        result = sim_backend.run(netlist_path, {"control_block": args["control_block"]})
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
    sim_tool = _build_simulation_tool(sim_backend, netlist_path)
    user_prompt = f"Netlist path: {netlist_path}\nControl block:\n{control_block}"
    return await run_agent(
        system_prompt=SIMULATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=SIMULATION_SCHEMA,
        backend=backend,
        tools=[sim_tool],
    )
