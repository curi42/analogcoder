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

That gate covers independent sources (V/I) at the top level and nothing else. The
testbench's other parts are not fenced off and no gate can tell them apart from the
circuit: a top-level resistor IS the circuit in an inverting-amplifier testbench, and is
pure measurement apparatus in a transistor-level one. So it is on you. A top-level load
(a `Cload` on the output), a coupling element (a `Cin` in series with the input) or a
loop-break element (an `Lfb`/`Cfb` pair opening a feedback loop for an AC measurement)
belongs to the measurement, not to the design. Shrinking a load capacitor improves phase
margin and bandwidth without improving anything you would tape out - it manufactures
margin, which this phase then spends on the objective. Do not propose one; propose knobs
inside the device under test.

refdes must identify exactly one component, qualified by its full subckt path when it
sits inside one (e.g. "BUF_N.Xcc"). param must be exactly a parameter name as it
appears on that component's line in the netlist below - or the string "value" when
that component's size is a plain NUMERIC positional token with no "name=" prefix
(e.g. "Rdeg out vn 10k"), which is how the structure view's tunable line writes it.
A positional token that is not a number is the component's model or subckt name;
"value" is not a knob there, so pick one of its named parameters instead.

The parameter must be on that component's own line. A name another instance of the
same model writes is not enough here: the search has to read a starting value out of
this component's line to step from, and inventing one is not something this project
does. Such a knob is discarded before it is ever measured.

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
