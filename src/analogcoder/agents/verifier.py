from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import VERIFIER_POST_SCHEMA, VERIFIER_PRE_SCHEMA

VERIFIER_SYSTEM_PROMPT = """You are a skeptical senior reviewer for analog circuit
tuning decisions. You check whether a proposed or applied change is justified by
the circuit analysis and simulation results, and whether it could cause unintended
side effects on other criteria."""


async def verify_pre(analysis: dict, judge_result: dict, proposal: dict, backend: AgentBackend) -> dict:
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result before tuning: {judge_result}\n"
        f"Proposed changes: {proposal}\n"
        "Decide whether to approve this proposal before it is applied."
    )
    return await run_agent(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VERIFIER_PRE_SCHEMA,
        backend=backend,
    )


async def verify_post(
    prev_judge_result: dict, new_judge_result: dict, applied_changes: list[dict], backend: AgentBackend
) -> dict:
    user_prompt = (
        f"Judge result before tuning: {prev_judge_result}\n"
        f"Judge result after applying and re-simulating: {new_judge_result}\n"
        f"Applied changes: {applied_changes}\n"
        "Decide whether the change should be kept or rolled back."
    )
    return await run_agent(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VERIFIER_POST_SCHEMA,
        backend=backend,
    )
