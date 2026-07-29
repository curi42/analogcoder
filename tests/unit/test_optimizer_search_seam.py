"""탐색 이음매 - 오라클 / 전략 / 수락 규칙의 경계를 고정한다.

기존 optimizer 테스트들은 **좌표 하강이 무엇을 하는가**를 잰다. 이 파일은
다른 것을 잰다: 전략이 무엇을 받고, 무엇을 할 수 없는가. 로드맵 단계 3·4가
여기에 다른 탐색기를 꽂을 것이므로, 그때 깨져야 하는 것은 전략이지 수락
규칙이 아니다.

핵심 주장은 하나다 - **전략은 제안할 뿐 수락을 정하지 못한다.** 아래 두
테스트가 그것을 반대 방향에서 친다: 예산을 넘긴 후보를 내미는 전략과, 기준을
깨는 후보를 내미는 전략. 둘 다 전략은 아무 검사도 하지 않고, 거절은 전략
밖에서 나온다.
"""

import json
from types import SimpleNamespace

import pytest

from analogcoder.optimizer import (
    DEFAULT_STRATEGY,
    MAX_OPTIMIZE_STEPS,
    SEARCH_STRATEGIES,
    Evaluation,
    Knob,
    OptimizerAgents,
    ProposedStep,
    accept_step,
    area_within_budget,
    coordinate_descent,
    run_optimization,
)
from analogcoder.spec import Criterion, OptimizeSpec
from analogcoder.state import RunState

DECK = (
    "* t\n"
    ".subckt AMP a b vss\n"
    "M1 a b vss vss NCH w=2e-6 l=1e-6 m=4\n"
    ".ends AMP\n"
    "Xa p q 0 AMP\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)

KNOB = {"refdes": "AMP.M1", "param": "m", "direction": "decrease", "reasoning": "tail"}


def _spec(**overrides):
    tb = SimpleNamespace(
        name="tb",
        criteria=[Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0)],
        control_block=".control\nmeas dc iq_ua FIND i(Vdd) AT=27\n.endc\n",
    )
    base = dict(
        circuit_name="demo",
        testbenches=[tb],
        pvt_corners=None,
        optimize=OptimizeSpec(objective="iq_ua", area_budget=1.10, guard_band=0.2),
    )
    base.update(overrides)
    base["canonical"] = base["testbenches"][0]
    base["all_criteria"] = list(base["testbenches"][0].criteria)
    return SimpleNamespace(**base)


def _state(tmp_path, deck=DECK):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": deck})
    return state


def _simulate(sequence):
    seq = list(sequence)
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        value = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"measurements": {"iq_ua": value}, "status": "success", "warnings": []}

    return simulate, calls


async def _propose(structure_view, margins, objective, netlist_view):
    return {"candidates": [KNOB], "overall_reasoning": "x"}


def _events(state, step):
    with open(state.history_path) as f:
        return [json.loads(line) for line in f if json.loads(line)["step"] == step]


# --- 전략이 무엇을 받는가 --------------------------------------------------


@pytest.mark.asyncio
async def test_the_strategy_receives_the_ranked_knobs_the_budget_and_a_working_oracle(tmp_path):
    """전략이 받는 것은 노브 목록, 전역 예산, 그리고 덱을 읽어 주는 오라클이다.

    셋 다 없으면 다른 탐색기를 쓸 수 없다: 노브가 없으면 무엇을 움직일지 모르고,
    예산이 없으면 언제 멈출지 모르며, 오라클이 없으면 현재 값을 알 수 없어
    스텝을 계산할 자리가 없다."""
    seen = {}

    async def strategy(run):
        seen["knobs"] = list(run.knobs)
        seen["remaining"] = run.remaining_steps
        seen["simulations_before"] = run.simulations
        knob = run.knobs[0]
        assert run.spend_step(knob)
        seen["remaining_after_spend"] = run.remaining_steps
        state = run.knob_state(knob)
        seen["state"] = state
        await run.attempt([ProposedStep(knob, state, 3.0)])
        seen["simulations_after"] = run.simulations

    simulate, _ = _simulate([235.0, 200.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=strategy)

    await run_optimization({"tb": DECK}, _spec(), _state(tmp_path), agents)

    assert seen["knobs"] == [Knob(refdes="AMP.M1", param="m", direction="decrease")]
    # 예산은 전역이고 모듈 상수에서 온다 - 전략마다 다른 예산을 주면 두 탐색기를
    # 비교하는 실험이 성립하지 않는다.
    assert seen["remaining"] == MAX_OPTIMIZE_STEPS
    assert seen["remaining_after_spend"] == MAX_OPTIMIZE_STEPS - 1
    # 오라클이 읽어 준 것은 덱에 적힌 사실이다: 철자 그대로의 토큰, 현재 값,
    # 그리고 이것이 개수 파라미터라는 것(m은 ±1이지 ×0.9가 아니다).
    assert seen["state"].token == "m"
    assert seen["state"].value == 4.0
    assert seen["state"].integer is True
    # 기준선 시뮬레이션은 오라클 밖에서 돈다 - 오라클이 세는 것은 **후보**를
    # 재는 데 쓴 것뿐이다. 두 전략의 "몇 번 쟀는가"를 비교하려면 그 경계가
    # 흐려지면 안 된다.
    assert seen["simulations_before"] == 0
    assert seen["simulations_after"] == 1


@pytest.mark.asyncio
async def test_the_default_strategy_is_coordinate_descent_and_is_named_for_what_it_is(tmp_path):
    """기본 전략을 이름으로도 고를 수 있어야 한다 - 하니스가 이름으로 고른다."""
    assert SEARCH_STRATEGIES[DEFAULT_STRATEGY] is coordinate_descent

    simulate, _ = _simulate([235.0, 200.0, 200.0, 200.0])
    explicit = OptimizerAgents(
        propose=_propose, simulate=simulate, search_strategy=SEARCH_STRATEGIES[DEFAULT_STRATEGY]
    )
    result = await run_optimization({"tb": DECK}, _spec(), _state(tmp_path), explicit)

    assert result["status"] == "OPTIMIZED"
    assert result["steps_accepted"] == 1


# --- 전략은 수락을 정하지 못한다 -------------------------------------------


@pytest.mark.asyncio
async def test_an_over_budget_candidate_is_rejected_by_the_accept_rule_not_by_the_strategy(
    tmp_path,
):
    """면적 예산을 넘긴 후보를 **아무 검사 없이** 내미는 전략.

    전략은 값을 계산해 attempt에 넘기기만 한다 - 예산도, 면적도 보지 않는다.
    그런데도 거절되어야 하고, 덱은 되돌아가야 하며, 시뮬레이션은 아예 돌지
    않아야 한다. m 4 -> 5는 면적 1.25배로, 개수 티어(2.0배)는 통과하지만
    예산 1.10배는 넘는다 - 그래서 막는 것이 에어리어 게이트가 아니라 예산임이
    확정된다."""
    seen = {}

    async def reckless(run):
        knob = run.knobs[0]
        run.spend_step(knob)
        state = run.knob_state(knob)
        seen["outcome"] = await run.attempt([ProposedStep(knob, state, 5.0)])

    simulate, calls = _simulate([235.0, 1.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=reckless)
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert seen["outcome"].accepted is False
    assert "over the 1.1x budget" in seen["outcome"].reason
    assert result["steps_accepted"] == 0
    assert result["steps_rejected"] == 1
    # 되돌려졌다 - 전략이 롤백을 부르지 않았는데도.
    assert "m=4" in state.current_netlist_texts()["tb"]
    # 기준선 하나뿐이다: 예산 초과는 시뮬레이션 **앞에서** 걸린다.
    assert calls["n"] == 1
    step = _events(state, "optimize_step")[0]
    assert step["accepted"] is False
    assert step["gate"] is None  # 에어리어 게이트가 아니라 예산이다
    assert step["objective"] is None


@pytest.mark.asyncio
async def test_a_candidate_that_regresses_a_criterion_is_not_accepted(tmp_path):
    """기준을 깨는 후보 역시 전략이 아니라 수락 규칙이 막는다.

    전략은 시뮬레이션 결과를 보지도 않는다 - attempt가 돌려주는 것을 무시하고
    끝낸다. 그래도 거절되고 되돌아가야 한다. 이것이 없으면 새 탐색기 하나가
    통과하던 설계를 실패로 만들 수 있다."""
    seen = {}

    async def reckless(run):
        knob = run.knobs[0]
        run.spend_step(knob)
        state = run.knob_state(knob)
        seen["outcome"] = await run.attempt([ProposedStep(knob, state, 3.0)])

    # 400uA는 iq<=300을 깬다. 목적값 자체는 235 -> 400이라 어차피 올라가지만,
    # 사유는 목적값이 아니라 **기준**이어야 한다 - 어느 규칙이 막았는지가
    # 갈리면 다음 사람이 잘못된 자리를 고친다.
    simulate, _ = _simulate([235.0, 400.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=reckless)
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert seen["outcome"].accepted is False
    assert "criteria no longer pass" in seen["outcome"].reason
    assert result["status"] == "UNCHANGED"
    assert result["steps_rejected"] == 1
    assert "m=4" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_strategy_cannot_write_the_counters_or_the_objective_baseline(tmp_path):
    """집계와 목적값 기준점은 전략이 **쓸 수 없다**.

    쓸 수 있으면 계약이 문서상의 약속으로 내려앉는다: best_objective를 올려
    두면 더 나쁜 후보가 수락되고, accepted를 조작하면 실행이 보고하는 수락 수가
    실제로 돌려주는 넷리스트를 설명하지 못한다 - 이 저장소가 final_criteria에서
    이미 한 번 겪은 모양이다. 그래서 대입이 조용히 먹히는 대신 터진다."""
    seen = {}

    async def liar(run):
        knob = run.knobs[0]
        run.spend_step(knob)
        state = run.knob_state(knob)
        await run.attempt([ProposedStep(knob, state, 3.0)])
        for attr, value in (("accepted", 99), ("rejected", 0), ("best_objective", 1e9)):
            try:
                setattr(run, attr, value)
            except AttributeError:
                seen.setdefault("blocked", []).append(attr)

    simulate, _ = _simulate([235.0, 400.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=liar)
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert seen["blocked"] == ["accepted", "rejected", "best_objective"]
    assert result["steps_accepted"] == 0
    assert result["steps_rejected"] == 1
    assert "m=4" in state.current_netlist_texts()["tb"]
    assert result["objective_after"] == result["objective_before"]


# --- 수락 규칙 자체 --------------------------------------------------------


def test_the_accept_rule_reads_facts_and_never_touches_the_deck():
    """수락 규칙은 순수 함수다. 상태도 시뮬레이터도 필요 없다 - 그래서 어떤
    전략을 붙이든 같은 답을 준다."""
    passing = Evaluation(
        pushed=True,
        area=1.0,
        objective=200.0,
        measurements={"iq_ua": 200.0},
        verdict={"overall_pass": True, "summary": "ok", "criteria": []},
        violations=[],
    )
    assert accept_step(passing, 235.0, "iq_ua") == (True, None)
    # 목적값이 내려가지 않으면 수락하지 않는다.
    ok, reason = accept_step(passing, 200.0, "iq_ua")
    assert ok is False and "not below the current best" in reason
    # 가드밴드 위반은 기준이 통과해도 거절이다.
    ok, reason = accept_step(
        Evaluation(**{**passing.__dict__, "violations": ["iq_ua 200 vs guarded limit 190"]}),
        235.0,
        "iq_ua",
    )
    assert ok is False and "guarded limit" in reason
    # 오라클이 끝까지 못 잰 경우, 그 사유가 곧 거절 사유다.
    ok, reason = accept_step(Evaluation(blocked="simulation raised"), 235.0, "iq_ua")
    assert ok is False and reason == "simulation raised"


def test_the_area_budget_is_off_when_the_starting_area_is_zero_and_that_is_deliberate():
    """area_before가 0이면 비율이 정의되지 않는다. 실제로 도달하는 경우고
    (래퍼 셀 덱), _optimize가 area_coverage로 그 사실을 남긴다 - 여기서
    임의로 막으면 그 기록이 거짓이 된다."""
    assert area_within_budget(100.0, 0.0, 1.1) == (True, None)
    ok, reason = area_within_budget(2.0, 1.0, 1.1)
    assert ok is False and "over the 1.1x budget" in reason
    assert area_within_budget(1.05, 1.0, 1.1) == (True, None)


# --- 고정 순위 주입: 실행에서 LLM을 통째로 뺀다 ----------------------------


@pytest.mark.asyncio
async def test_an_injected_knob_ranking_makes_no_agent_call_at_all(tmp_path):
    """주입된 순위가 있으면 propose는 **한 번도** 불리지 않는다.

    이것이 단계 3·4의 A/B가 성립하는 근거 전부다. LLM이 한 번이라도 끼면 같은
    입력에서 다른 순위가 나올 수 있고(이 저장소는 한 넷리스트에서 93/26/1개의
    역할을 받은 전례가 있다), 그 분산이 탐색기 차이보다 커진다. 그래서 "덜
    부른다"가 아니라 "안 부른다"를 고정한다 - propose가 불리면 즉시 터진다."""

    async def forbidden(structure_view, margins, objective, netlist_view):
        raise AssertionError("the optimizer called the ranking agent despite an injected ranking")

    simulate, _ = _simulate([235.0, 200.0, 200.0, 200.0])
    agents = OptimizerAgents(
        propose=forbidden,
        simulate=simulate,
        knob_ranking=[KNOB],
    )
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert result["steps_accepted"] == 1
    assert "m=3" in state.current_netlist_texts()["tb"]
    # 그리고 그 사실이 이력에 남는다. 남지 않으면 "고정 순위로 돈 실행"과
    # "에이전트가 마침 같은 순위를 낸 실행"이 history.jsonl에서 같은 모양이
    # 된다 - 이 저장소가 아홉 번 당한 조용한 무력화와 같은 구멍이다.
    proposals = _events(state, "optimize_proposal")
    assert len(proposals) == 1
    assert proposals[0]["source"] == "fixed"
    assert proposals[0]["candidates"] == [KNOB]


@pytest.mark.asyncio
async def test_without_an_injected_ranking_the_agent_is_still_what_ranks(tmp_path):
    """기본 경로는 바뀌지 않는다. 이것이 깨지면 실제 실행에서 최적화가
    노브를 고르지 못한다."""
    calls = []

    async def propose(structure_view, margins, objective, netlist_view):
        calls.append((structure_view, netlist_view))
        return {"candidates": [KNOB], "overall_reasoning": "x"}

    simulate, _ = _simulate([235.0, 200.0, 200.0, 200.0])
    agents = OptimizerAgents(propose=propose, simulate=simulate)
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert len(calls) == 1
    # 에이전트에게 실제 덱이 갔는가. 뷰가 비면 순위는 근거 없는 추측이 된다.
    assert "M1" in calls[0][1]
    assert result["steps_accepted"] == 1
    assert _events(state, "optimize_proposal")[0]["source"] == "agent"


@pytest.mark.asyncio
async def test_the_injected_ranking_is_ordered_and_the_strategy_sees_that_order(tmp_path):
    """순위는 순서다. 하니스가 두 전략에 **같은** 순위를 주는 것이 통제의
    전부이므로, 그 순서가 전략에 그대로 도착해야 한다."""
    ranking = [
        {"refdes": "AMP.M1", "param": "l", "direction": "increase", "reasoning": "a"},
        {"refdes": "AMP.M1", "param": "m", "direction": "decrease", "reasoning": "b"},
    ]
    seen = {}

    async def strategy(run):
        seen["knobs"] = list(run.knobs)

    async def forbidden(*args):
        raise AssertionError("no agent call expected")

    simulate, _ = _simulate([235.0])
    agents = OptimizerAgents(
        propose=forbidden, simulate=simulate, knob_ranking=ranking, search_strategy=strategy
    )

    await run_optimization({"tb": DECK}, _spec(), _state(tmp_path), agents)

    assert [k.param for k in seen["knobs"]] == ["l", "m"]
    assert [k.direction for k in seen["knobs"]] == ["increase", "decrease"]


# --- 거절이 왜 거절인가 ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_rejection_count_is_split_by_reason_and_the_split_adds_up(tmp_path):
    """'Steps: 0 accepted, 4 rejected'는 네 가지 서로 다른 사실의 합이다.

    (a) 노브가 주소 게이트에 막혔다, (b) 그 주소는 합법인데 덱에 출발 값이
    없다, (c) 전략이 이 노브를 소진했다, (d) 후보를 실제로 재고 수락 규칙이
    떨어뜨렸다 - 그리고 (a)(b)(c)는 시뮬레이션을 **한 번도** 쓰지 않는다.
    한 숫자로 접으면 "탐색이 열심히 했지만 여지가 없었다"와 "탐색이 노브에
    접근조차 못 했다"가 구별되지 않는다. 이 저장소가 topology_unavailable에
    사유 코드를 붙인 것과, area_check/refdes_check가 같은 키를 써서 사후
    구별이 안 됐던 것과 같은 부류다."""
    from analogcoder.optimizer import REJECTION_REASONS

    # M1은 l=을 안 쓰고 같은 모델의 M2가 쓴다 - check_param_applicability의
    # 동료 규칙이 admit 하는 바로 그 모양이라, 주소는 합법인데 한 걸음 옮길
    # 출발 값이 없다. 게이트에 막힌 것과 **다른 사실**이다.
    peer_deck = (
        "* t\n"
        ".subckt AMP a b vss\n"
        "M1 a b vss vss NCH w=2e-6 m=4\n"
        "M2 a b vss vss NCH w=2e-6 m=4 l=1e-6\n"
        ".ends AMP\n"
        "Xa p q 0 AMP\n"
        "Vdd vdd 0 DC 1.8\n"
        ".end\n"
    )

    async def reckless(run):
        knob = run.knobs[0]
        # (a) 그런 소자가 없다 - refdes 게이트.
        assert run.knob_state(Knob("NOSUCH", "m", "decrease")) is None
        # (b) 주소는 합법이나 M1의 줄에 l=이 없다.
        assert run.knob_state(Knob("AMP.M1", "l", "decrease")) is None
        # (c) 전략이 소진 판정.
        state = run.knob_state(knob)
        run.exhausted(knob, state, "no further value")
        # (d) 재고 나서 수락 규칙에 떨어진다(iq가 올라간다).
        run.spend_step(knob)
        await run.attempt([ProposedStep(knob, state, 3.0)])

    simulate, _ = _simulate([235.0, 400.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=reckless)
    state = _state(tmp_path, deck=peer_deck)

    result = await run_optimization({"tb": peer_deck}, _spec(), state, agents)

    by_reason = result["rejected_by_reason"]
    # 키는 **전부** 있어야 한다. 걸리지 않은 사유가 키째 사라지면 "이 사유로는
    # 거절이 없었다"와 "이 사유가 코드에서 없어졌다"가 같은 모양이 된다.
    assert set(by_reason) == set(REJECTION_REASONS)
    assert by_reason["knob_gate"] == 1
    assert by_reason["knob_no_value"] == 1
    assert by_reason["exhausted"] == 1
    assert by_reason["not_accepted"] == 1
    assert by_reason["area_growth"] == 0
    assert by_reason["area_budget"] == 0
    assert by_reason["simulation_failed"] == 0
    assert by_reason["corner_walked_back"] == 0
    # 합이 총계와 어긋나면 둘 중 하나가 거짓말이다.
    assert sum(by_reason.values()) == result["steps_rejected"] == 4


@pytest.mark.asyncio
async def test_an_over_budget_candidate_is_counted_as_the_budget_not_as_the_accept_rule(tmp_path):
    """예산 초과는 시뮬레이션 **앞에서** 걸린다 - 재고 나서 떨어진 후보와
    같은 칸에 세면 "이 탐색이 몇 번 쟀는가"를 결과에서 읽을 수 없다."""

    async def reckless(run):
        knob = run.knobs[0]
        run.spend_step(knob)
        await run.attempt([ProposedStep(knob, run.knob_state(knob), 5.0)])

    simulate, calls = _simulate([235.0, 1.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=reckless)
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["rejected_by_reason"]["area_budget"] == 1
    assert result["rejected_by_reason"]["not_accepted"] == 0
    assert calls["n"] == 1  # 기준선 하나뿐 - 후보는 재지 않았다


@pytest.mark.asyncio
async def test_a_run_that_rejects_nothing_still_reports_every_reason_at_zero(tmp_path):
    """아무것도 거절되지 않은 실행에서도 사유 표는 통째로 실린다."""
    from analogcoder.optimizer import REJECTION_REASONS

    async def passive(run):
        return None

    simulate, _ = _simulate([235.0])
    agents = OptimizerAgents(propose=_propose, simulate=simulate, search_strategy=passive)
    state = _state(tmp_path)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["rejected_by_reason"] == {name: 0 for name in REJECTION_REASONS}
    assert result["steps_rejected"] == 0
