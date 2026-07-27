import pytest

from analogcoder.agents.optimizer import propose_candidates
from analogcoder.schemas import OPTIMIZER_SCHEMA


class FakeBackend:
    """Conforms to the positional AgentBackend.run(system_prompt, user_prompt,
    output_schema, tools) signature that agents.agent_runtime.run_agent actually
    calls with - see tests/unit/test_agent_runtime.py's FakeBackend."""

    def __init__(self):
        self.calls = []

    async def run(self, system_prompt, user_prompt, output_schema, tools=None):
        self.calls.append({"system": system_prompt, "user": user_prompt, "schema": output_schema})
        return {
            "candidates": [
                {
                    "refdes": "AMP.M1",
                    "param": "m",
                    "direction": "decrease",
                    "reasoning": "tail current source",
                }
            ],
            "overall_reasoning": "cut the tail first",
        }


@pytest.mark.asyncio
async def test_the_agent_receives_the_objective_and_the_margins():
    backend = FakeBackend()

    await propose_candidates(
        "circuit: demo\nblocks:\n  AMP …",
        [{"name": "iq", "actual": 235.0, "target": "<=300.0"}],
        "iq_ua",
        "* deck\nM1 d g s b NCH w=2e-6\n",
        backend,
    )

    user = backend.calls[0]["user"]
    assert "iq_ua" in user
    assert "235" in user
    assert "AMP" in user


@pytest.mark.asyncio
async def test_the_agent_is_told_not_to_propose_numbers():
    backend = FakeBackend()

    await propose_candidates("s", [], "iq_ua", "n", backend)

    system = backend.calls[0]["system"]
    assert "direction" in system.lower()
    # 수치를 내지 말라는 지시가 프롬프트에 있어야 한다. 약한 모델이
    # two_stage_opamp에서 Cc를 거꾸로 움직여 10 iteration을 태운 전력이 있다.
    assert "value" in system.lower()


def test_the_schema_forbids_a_numeric_proposal():
    item = OPTIMIZER_SCHEMA["properties"]["candidates"]["items"]

    assert set(item["required"]) == {"refdes", "param", "direction", "reasoning"}
    assert "new_value" not in item["properties"]
    assert item["properties"]["direction"]["enum"] == ["increase", "decrease"]
