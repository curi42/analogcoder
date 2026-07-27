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
    # netlist_view itself must reach the prompt, not just structure_view/margins/objective.
    assert "w=2e-6" in user


@pytest.mark.asyncio
async def test_the_agent_is_told_not_to_propose_numbers():
    backend = FakeBackend()

    await propose_candidates("s", [], "iq_ua", "n", backend)

    system = backend.calls[0]["system"]
    # Pin the actual instruction, not mere word co-occurrence: "direction" and
    # "value" both appear in innocuous sentences too (e.g. "consider the value
    # each candidate brings"), so checking for the words alone would still pass
    # with the prohibition itself deleted. These two phrases are the
    # division-of-labour statement: deleting either must fail this test.
    assert "do not propose a numeric value" in system.lower()
    assert "deterministic search decides how far" in system.lower()


def test_the_prompt_tells_the_model_not_to_touch_testbench_sources():
    from analogcoder.agents.optimizer import OPTIMIZER_SYSTEM_PROMPT

    assert "testbench's own sources" in OPTIMIZER_SYSTEM_PROMPT


def test_the_schema_forbids_a_numeric_proposal():
    item = OPTIMIZER_SCHEMA["properties"]["candidates"]["items"]

    assert set(item["required"]) == {"refdes", "param", "direction", "reasoning"}
    assert "new_value" not in item["properties"]
    assert item["properties"]["direction"]["enum"] == ["increase", "decrease"]


def test_the_schema_structurally_rejects_an_extra_field_like_new_value():
    # required/properties alone only makes new_value non-required - jsonschema
    # permits extra keys by default, so a model could still smuggle a numeric
    # new_value through validation. additionalProperties: false is what makes
    # a numeric proposal actually structurally impossible, per the plan.
    import jsonschema

    candidate = {
        "refdes": "M1",
        "param": "w",
        "direction": "decrease",
        "reasoning": "x",
        "new_value": "5u",
    }
    payload = {"candidates": [candidate], "overall_reasoning": "y"}

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, OPTIMIZER_SCHEMA)
