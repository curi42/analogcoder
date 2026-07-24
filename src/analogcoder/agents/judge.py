import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from analogcoder.agents._sdk_utils import run_agent
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.schemas import JUDGE_SCHEMA
from analogcoder.spec import Criterion

JUDGE_SYSTEM_PROMPT = """You are an analog circuit judge. You are given simulation
measurements and a list of pass/fail criteria. Call the evaluate_criteria tool to
compute results precisely, then report them via the structured output schema. Do
not compute pass/fail comparisons yourself; always use the tool."""


def _build_judge_tool(criteria: list[Criterion]):
    @tool(
        "evaluate_criteria",
        "Compare measurements against target criteria",
        {"measurements": dict},
    )
    async def _evaluate(args):
        result = evaluate_criteria(args["measurements"], criteria)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return _evaluate


async def judge_measurements(measurements: dict, criteria: list[Criterion]) -> dict:
    judge_tool = _build_judge_tool(criteria)
    server = create_sdk_mcp_server("judge", tools=[judge_tool])
    user_prompt = f"Measurements: {measurements}\nCriteria: {criteria}"
    return await run_agent(
        system_prompt=JUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=JUDGE_SCHEMA,
        mcp_servers={"judge": server},
        allowed_tools=["mcp__judge__evaluate_criteria"],
    )
