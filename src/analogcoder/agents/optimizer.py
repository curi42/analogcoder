from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import OPTIMIZER_SCHEMA

OPTIMIZER_SYSTEM_PROMPT = """You are an analog circuit optimization specialist. The
circuit already meets every criterion in its specification. Your job is to find where
its remaining margin can be spent to reduce the stated objective - normally the
quiescent current.

Propose a RANKED list of candidate knobs, best first. For each, give the refdes, the
parameter, and the direction ("decrease" or "increase") - and nothing else.

Do NOT propose a numeric value. You are choosing WHICH knob and WHICH direction; a
deterministic search decides how far to move it and measures the result. A proposal
carrying a value will be rejected.

Direction is not always "decrease": lengthening a channel reduces current at a fixed
width, so "increase" on an `l` is a legitimate way to cut current. Reason about the
circuit, not about the word.

Rank by expected effect on the objective. A device that sets a bias current - a
current-mirror leg, a tail source - moves the objective directly. A device that only
sets matching or drive strength usually does not. The derived structure below reports
matched patterns (differential pairs, current mirrors, stacked pairs) and which block
drives or senses each net; use them.

Do not propose a change to the testbench's own sources. Lowering a supply reduces
current without improving the circuit, and it will be rejected by a deterministic gate.

refdes must identify exactly one component, qualified by its full subckt path when it
sits inside one (e.g. "BUF_N.Xcc"). param must be exactly a parameter name as it
appears on that component's line in the netlist below.

Respond via the structured output schema."""


async def propose_candidates(
    structure_view: str,
    margins: list[dict],
    objective: str,
    netlist_view: str,
    backend: AgentBackend,
) -> dict:
    user_prompt = (
        f"Objective to minimise: {objective}\n"
        f"Current netlist:\n{netlist_view}\n"
        f"Circuit structure (derived deterministically):\n{structure_view}\n"
        f"Criteria and how much margin each has left: {margins}"
    )
    return await run_agent(
        system_prompt=OPTIMIZER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=OPTIMIZER_SCHEMA,
        backend=backend,
    )
