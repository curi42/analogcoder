from analogcoder.agents._sdk_utils import run_agent
from analogcoder.schemas import TUNER_SCHEMA

TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Given the
circuit's structural analysis, the judge's pass/fail verdict, the history of past
tuning attempts in this run, and (if present) feedback on why your last proposal
was rejected, propose specific component parameter changes to fix the failing
criteria. Only propose changes to parameters listed in tunable_params. Respond via
the structured output schema."""


async def propose_tuning(
    analysis: dict,
    judge_result: dict,
    history: list[dict],
    rejection_feedback: str | None,
) -> dict:
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result: {judge_result}\n"
        f"Past attempts this run: {history}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=TUNER_SCHEMA,
    )
