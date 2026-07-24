from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import ANALYZER_SCHEMA

ANALYZER_SYSTEM_PROMPT = """You are a senior analog IC design engineer. Given a SPICE
netlist, identify the circuit type, break it into functional stages, explain the role
of each component, and list which components/parameters are safe to tune without
changing the circuit's topology. Respond only via the structured output schema."""


async def analyze_netlist(netlist_text: str, backend: AgentBackend) -> dict:
    user_prompt = f"Analyze this SPICE netlist:\n\n{netlist_text}"
    return await run_agent(
        system_prompt=ANALYZER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=ANALYZER_SCHEMA,
        backend=backend,
    )
