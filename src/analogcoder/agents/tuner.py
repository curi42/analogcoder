from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import TOPOLOGY_SCHEMA, TUNER_SCHEMA
from analogcoder.topologies import Topology

TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Given the
current netlist, the circuit's structural analysis, the judge's pass/fail verdict,
the history of past tuning attempts in this run, and (if present) feedback on why
your last proposal was rejected, propose specific component parameter changes to
fix the failing criteria. Only propose changes to parameters listed in
tunable_params.

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

Respond via the structured output schema."""


async def propose_tuning(
    analysis: dict,
    judge_result: dict,
    history: list[dict],
    rejection_feedback: str | None,
    netlist_text: str,
    backend: AgentBackend,
) -> dict:
    user_prompt = (
        f"Current netlist:\n{netlist_text}\n"
        f"Circuit analysis: {analysis}\n"
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
    analysis: dict,
    judge_result: dict,
    available_topologies: list[Topology],
    rejection_feedback: str | None,
    backend: AgentBackend,
) -> dict:
    topology_descriptions = "\n".join(
        f"- {t.id}: {t.description} (addresses: {t.addresses})" for t in available_topologies
    )
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
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
