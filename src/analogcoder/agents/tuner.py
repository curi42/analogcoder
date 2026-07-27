from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import TOPOLOGY_SCHEMA, TUNER_SCHEMA
from analogcoder.topologies import Topology

TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Given the
current netlist, the circuit's structure, the judge's pass/fail verdict,
the history of past tuning attempts in this run, and (if present) feedback on why
your last proposal was rejected, propose specific component parameter changes to
fix the failing criteria.

The structure view lists every block, but only the blocks currently in focus are
expanded. An expanded block carries a "tunable" line of addresses written as
"refdes=<R> param=<P>" - those two are SEPARATE schema fields, never one dotted
string. That line is the set of addresses visible in this view, NOT the set of
legal changes: blocks whose bodies are folded away in the netlist below (shown as
"* ... (N components elided)") have their own addresses, and you may propose a
change to any component in the netlist, including one inside a folded block, by
naming it with its full "<PATH>.<refdes>" path. The focus is a relevance hint and
can be wrong; if the fix for a failing criterion lives in a block that is not
expanded, propose it anyway.

The one exception is the top level's "stimulus (not tunable)" line: those are the
testbench's own independent sources. Changing them changes the measurement rather
than the circuit (scaling an AC source scales every gain measurement), so they are
never a fix and a deterministic gate rejects them.

old_value and new_value MUST be concrete, literal SPICE values taken from and
written in the same form as the current netlist (e.g. "10k", "4.7u", "100n") -
never a description, formula, percentage, or placeholder like "unknown" or "N/A".
Read the actual current value for the component you are changing directly from
the netlist below before proposing new_value. For example, if the netlist has
"Rf vminus vout 10k" and you are changing Rf, old_value is "10k" and new_value
must be a specific replacement value such as "15k", not "increase Rf".

param MUST be exactly the string "value" when the component's value is a plain
positional token, which is the common case (e.g. "Rf vminus vout 10k" - the
value 10k is the last token with no "name=" prefix, so param is "value", not
"resistance" or "resistance value"). Only use a different param string when the
netlist itself writes that parameter as "name=value" (e.g. "M1 d g s b W=10u
L=1u" - to change the width you would use param="W"), and in that case param
must be exactly that name as it appears in the netlist, nothing else.

refdes MUST identify exactly one component. When the component sits inside a
.subckt, qualify it with the subckt's full path as "<PATH>.<refdes>" (e.g.
"BUF_N.Xcc" for the Xcc inside ".subckt BUF_N ...", or "OUTER.INNER.M1" for
an M1 inside a .subckt INNER nested within .subckt OUTER). The path must be
complete: a partial path such as "INNER.M1" for a component in OUTER.INNER
is rejected. An unqualified refdes that appears in more than one scope is
also rejected as ambiguous. Note the scope is the subckt definition:
changing it changes every instance of that subckt.

Respond via the structured output schema."""


async def propose_tuning(
    structure_view: str,
    judge_result: dict,
    history: list[dict],
    rejection_feedback: str | None,
    netlist_text: str,
    backend: AgentBackend,
) -> dict:
    user_prompt = (
        f"Current netlist:\n{netlist_text}\n"
        f"Circuit structure (derived deterministically): {structure_view}\n"
        f"Judge result: {judge_result}\n"
        f"Past attempts this run: {history}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=TUNER_SCHEMA,
        backend=backend,
    )


TOPOLOGY_TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Parameter
tuning has been tried repeatedly and failed to meet the target criteria. You must now
choose ONE topology from the list of available, pre-verified topologies below to replace
the amplifier's internal structure.

topology_id MUST be exactly one of the ids listed as available - never invent a new id,
never reuse a topology_id that is not in the available list (it has likely already been
tried and rejected). Base your choice on which listed topology's description most
directly addresses the currently failing criteria.

Respond via the structured output schema."""


async def propose_topology_swap(
    structure_view: str,
    judge_result: dict,
    available_topologies: list[Topology],
    rejection_feedback: str | None,
    backend: AgentBackend,
) -> dict:
    topology_descriptions = "\n".join(
        f"- {t.id}: {t.description} (addresses: {t.addresses})" for t in available_topologies
    )
    user_prompt = (
        f"Circuit structure (derived deterministically): {structure_view}\n"
        f"Judge result: {judge_result}\n"
        f"Available topologies:\n{topology_descriptions}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=TOPOLOGY_TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=TOPOLOGY_SCHEMA,
        backend=backend,
    )
