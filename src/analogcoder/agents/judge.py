from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend, ToolSpec
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.schemas import JUDGE_SCHEMA
from analogcoder.spec import Criterion

JUDGE_SYSTEM_PROMPT = """You are an analog circuit judge. You are given simulation
measurements and a list of pass/fail criteria. Call the evaluate_criteria tool to
compute results precisely, then report them via the structured output schema. Do
not compute pass/fail comparisons yourself; always use the tool."""


def _build_judge_tool(criteria: list[Criterion]) -> ToolSpec:
    async def _evaluate(args: dict) -> dict:
        return evaluate_criteria(args["measurements"], criteria)

    return ToolSpec(
        name="evaluate_criteria",
        description="Compare measurements against target criteria",
        parameters={
            "type": "object",
            "properties": {"measurements": {"type": "object"}},
            "required": ["measurements"],
        },
        handler=_evaluate,
    )


async def judge_measurements(measurements: dict, criteria: list[Criterion], backend: AgentBackend) -> dict:
    judge_tool = _build_judge_tool(criteria)
    user_prompt = f"Measurements: {measurements}\nCriteria: {criteria}"
    return await run_agent(
        system_prompt=JUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=JUDGE_SCHEMA,
        backend=backend,
        tools=[judge_tool],
    )
