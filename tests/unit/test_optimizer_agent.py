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


def test_the_prompt_warns_about_testbench_passives_the_gate_cannot_see():
    # 최종 리뷰 Finding 4. check_stimulus_untouched와 structure.py의 주소록
    # 생략은 **최상위 V/I만** 덮는다. benchmarks/two_stage_opamp/netlist.cir의
    # 최상위 Lfb(1MH 루프 차단 인덕터), Cin, Cload는 순수한 테스트벤치 부속인데
    # 주소록에 들어 있고 최적화가 건드릴 수 있다 - Cload를 줄이면 DUT를 하나도
    # 안 건드리고 phase margin과 UGBW가 좋아지고, 최적화는 그 조작된 마진을
    # 전류로 바꾼다. `Vin AC 1 -> AC 100`과 같은 모양이다.
    #
    # 게이트를 넓히는 것은 답이 아니다: benchmarks/inverting_amp에서는 최상위
    # Rin/Rf/Eopamp가 **회로 그 자체**라 "최상위 수동소자"라는 규칙은 이
    # 프로젝트가 금하는 추측이 된다. 고칠 자리는 프롬프트다.
    from analogcoder.agents.optimizer import OPTIMIZER_SYSTEM_PROMPT

    prompt = OPTIMIZER_SYSTEM_PROMPT.lower()
    assert "load" in prompt and "loop-break" in prompt
    # 게이트가 막아 준다고 말하면 안 된다 - 막지 못한다.
    assert "no gate" in prompt or "not blocked" in prompt or "cannot" in prompt


_POSITIONAL_VALUE_DECK = """* positional value deck
.subckt OPAMP2STAGE vp vn out
Rdeg out vn 10k
M1 out vn 0 0 nfet W=10 L=1
.ends
Xa a b c OPAMP2STAGE
.end
"""


def test_the_optimizer_prompt_admits_the_positional_value_address_its_own_view_prints():
    """어느 변형을 잡는가: param을 "그 소자 줄에 적힌 파라미터 이름"으로만
    규정한 프롬프트.

    최적화 오라클은 param="value"를 끝까지 지원하고(_deck_token/_current_value),
    같은 프롬프트에 딸려 가는 구조 뷰의 tunable 줄이 `param=value`를 그대로
    인쇄한다. 프롬프트만 그것을 빼면 게이트보다 **엄격한** 거울이 되어 위치
    토큰으로 값을 갖는 모든 R/C 노브가 탐색에서 사라진다 - inverting_amp처럼
    최상위 저항이 회로 자체인 덱에서는 그것이 노브 전부다.

    아래 앞 두 단언이 코드의 실제 규칙이고, 뒤가 프롬프트가 그것을 반복하는지다."""
    from analogcoder.agents.optimizer import OPTIMIZER_SYSTEM_PROMPT
    from analogcoder.optimizer import _current_value, _deck_token
    from analogcoder.optimizer import index_baseline_components

    component = index_baseline_components(_POSITIONAL_VALUE_DECK)["OPAMP2STAGE.Rdeg"]
    assert _deck_token(component, "value") == "value"
    assert _current_value(component, "value") == 10000.0

    prompt = " ".join(OPTIMIZER_SYSTEM_PROMPT.lower().split())
    assert '"value"' in prompt
    assert "positional" in prompt


def test_the_optimizer_prompt_keeps_the_own_line_requirement_the_oracle_enforces():
    """어느 변형을 잡는가: 튜너의 동료 규칙을 최적화 프롬프트에도 복사하는 구현.

    두 프롬프트가 여기서 갈라지는 것은 실수가 아니다. check_param_applicability의
    동료 규칙은 `Xq1.m`을 **적용 가능**하다고 판정하지만, 최적화는 그 자리에서
    한 걸음 옮길 **출발 값**이 필요하고 Xq1의 줄에는 m=이 없다 - SearchOracle.
    knob_state가 바로 그 사유로 노브를 버린다. 프롬프트가 그것보다 느슨해지면
    탐색이 매번 주소 단계에서 버려질 노브를 순위에 올린다."""
    from analogcoder.agents.optimizer import OPTIMIZER_SYSTEM_PROMPT

    prompt = " ".join(OPTIMIZER_SYSTEM_PROMPT.lower().split())
    assert "on that component's line" in prompt
