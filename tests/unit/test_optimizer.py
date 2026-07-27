import json
from types import SimpleNamespace

import pytest

from analogcoder.optimizer import OptimizerAgents, run_optimization
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


def _spec(**overrides):
    tb = SimpleNamespace(
        name="tb",
        criteria=[Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0)],
        control_block=".control\nmeas dc iq_ua FIND i(Vdd) AT=27\n.endc\n",
    )
    base = dict(
        circuit_name="demo",
        testbenches=[tb],
        pvt_corners=None,   # Task 6이 이 속성을 읽는다. 없으면 AttributeError.
        optimize=OptimizeSpec(objective="iq_ua", area_budget=1.10, guard_band=0.2),
    )
    base.update(overrides)
    base["canonical"] = base["testbenches"][0]
    base["all_criteria"] = list(base["testbenches"][0].criteria)
    return SimpleNamespace(**base)


def _agents(measure_sequence, candidates=None):
    """measure_sequence: 시뮬레이션 호출마다 돌려줄 iq_ua 값."""
    seq = list(measure_sequence)
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        value = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"measurements": {"iq_ua": value}, "status": "success", "warnings": []}

    async def propose(structure_view, margins, objective, netlist_view):
        return {
            "candidates": candidates
            if candidates is not None
            else [{"refdes": "AMP.M1", "param": "m", "direction": "decrease",
                   "reasoning": "tail"}],
            "overall_reasoning": "x",
        }

    return OptimizerAgents(propose=propose, simulate=simulate), calls


@pytest.mark.asyncio
async def test_a_spec_without_an_optimize_block_is_skipped_and_says_so(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([200.0])

    result = await run_optimization({"tb": DECK}, _spec(optimize=None), state, agents)

    assert result["status"] == "SKIPPED"
    events = [json.loads(line) for line in open(state.history_path)]
    assert any(e["step"] == "optimize_skipped" for e in events)


@pytest.mark.asyncio
async def test_a_step_that_lowers_the_objective_is_accepted(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    # 기준선 235, 첫 단계 후 200 -> 개선이므로 수락, 그 다음은 정체.
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0, 200.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert result["objective_after"] < result["objective_before"]
    assert result["steps_accepted"] >= 1
    assert "m=3" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_step_that_raises_the_objective_is_reverted(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 260.0, 260.0, 260.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_step_that_breaks_the_guard_band_is_reverted(tmp_path):
    # 290은 iq<=300을 통과하지만 가드밴드 240을 넘는다. 목적값이 내려가도
    # 수락하면 안 된다 - 마진을 다 태워버린 상태가 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([295.0, 290.0, 290.0, 290.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"


@pytest.mark.asyncio
async def test_a_proposal_against_the_testbench_supply_never_reaches_simulation(tmp_path):
    # 전류를 줄이는 가장 쉬운 길은 공급을 낮추는 것이다. 게이트가 막아야 하고,
    # 시뮬레이션을 쓰기 전에 막아야 한다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, calls = _agents(
        [235.0],
        candidates=[{"refdes": "Vdd", "param": "value", "direction": "decrease",
                     "reasoning": "less supply, less current"}],
    )

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1  # 기준선 측정 한 번뿐
    events = [json.loads(line) for line in open(state.history_path)]
    assert any(e["step"] == "optimize_step" and e.get("gate") for e in events)


@pytest.mark.asyncio
async def test_an_integer_parameter_steps_by_one_and_stops_at_one(tmp_path):
    deck = DECK.replace("m=4", "m=1")
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": deck})
    agents, calls = _agents([235.0])

    result = await run_optimization({"tb": deck}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1  # 후보가 소진되어 시뮬레이션이 더 돌지 않는다


@pytest.mark.asyncio
async def test_the_area_budget_rejects_a_step_before_it_is_simulated(tmp_path):
    # `l` 증가는 면적을 1/0.9 = 1.111배로 만들어 1.10 예산을 넘는다. 면적은
    # 파생이라 공짜고 목적값은 재야 아는 값이므로, 예산 초과는 시뮬레이션
    # **앞에서** 걸려야 한다 - 그 비대칭이 이 루프를 감당 가능하게 만든다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, calls = _agents(
        [235.0, 100.0],
        candidates=[{"refdes": "AMP.M1", "param": "l", "direction": "increase",
                     "reasoning": "longer channel, less current"}],
    )

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1  # 기준선 측정 한 번뿐
    assert "l=1e-6" in state.current_netlist_texts()["tb"]
    steps = [json.loads(line) for line in open(state.history_path)]
    rejected = [e for e in steps if e["step"] == "optimize_step"]
    assert len(rejected) == 1
    assert "budget" in rejected[0]["reason"]
    assert rejected[0]["area"] is not None  # 면적은 측정 없이 알 수 있었다


@pytest.mark.asyncio
async def test_a_step_that_breaks_a_criterion_is_reverted_even_when_it_helps_the_objective(tmp_path):
    # 목적값은 내려갔지만 다른 기준이 깨졌다. 목적값만 보면 수락하게 되는데,
    # 최적화는 통과한 설계 위에서만 의미가 있다.
    tb = SimpleNamespace(
        name="tb",
        criteria=[
            Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0),
            Criterion(name="gain", measurement="gain_db", operator=">=", threshold=40.0),
        ],
        control_block="",
    )
    seq = [
        {"iq_ua": 235.0, "gain_db": 60.0},   # 기준선: 둘 다 통과
        {"iq_ua": 200.0, "gain_db": 30.0},   # 목적값은 좋아졌지만 gain이 깨졌다
    ]
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        value = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"measurements": dict(value), "status": "success", "warnings": []}

    async def propose(structure_view, margins, objective, netlist_view):
        return {
            "candidates": [{"refdes": "AMP.M1", "param": "m", "direction": "decrease",
                            "reasoning": "tail"}],
            "overall_reasoning": "x",
        }

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents = OptimizerAgents(propose=propose, simulate=simulate)

    result = await run_optimization({"tb": DECK}, _spec(testbenches=[tb]), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]
    steps = [json.loads(line) for line in open(state.history_path)]
    reasons = [e["reason"] for e in steps if e["step"] == "optimize_step"]
    assert any("criteria no longer pass" in r for r in reasons)


WRAPPER_DECK = (
    "* t\n"
    ".subckt AMP a b vss mult=1\n"
    "M1 a b vss vss NCH w=2e-6 l=1e-6 m='mult'\n"
    ".ends AMP\n"
    "Xa p q 0 AMP mult=4\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_an_instance_parameter_that_reaches_m_steps_as_a_whole_number(tmp_path):
    # 래퍼 셀 흐름: 이름은 `mult`지만 그 값이 도달하는 토큰은 `m`이라 개수다.
    # 이름만 보면 3.6을 만들고 area_limits의 정수성 검사가 그것을 되받아
    # 후보가 첫 단계에서 죽는다. 두 곳이 같은 판정을 써야 한다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": WRAPPER_DECK})
    agents, _ = _agents(
        [235.0, 200.0, 200.0, 200.0],
        candidates=[{"refdes": "Xa", "param": "mult", "direction": "decrease",
                     "reasoning": "tail multiplier"}],
    )

    result = await run_optimization({"tb": WRAPPER_DECK}, _spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert "mult=3" in state.current_netlist_texts()["tb"]
    steps = [json.loads(line) for line in open(state.history_path)]
    gates = [e["gate"] for e in steps if e["step"] == "optimize_step"]
    assert "area" not in gates  # 정수성 위반으로 되받히지 않았다


MIXED_CASE_DECK = (
    "* t\n"
    ".subckt AMP a b vss\n"
    "M1 a b vss vss NCH W=2e-6 l=1e-6 m=4\n"
    "M2 a b vss vss NCH w=1e-6 l=1e-6\n"
    ".ends AMP\n"
    "Xa p q 0 AMP\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_a_written_parameter_keeps_the_spelling_used_in_the_deck(tmp_path):
    # 덱은 `W=`로 쓰는데 제안이 `w`라고 한다. SPICE는 대소문자를 안 가리지만
    # apply_changes는 가리므로, 접은 이름으로 되쓰면 토큰이 하나 더 붙어 덱이
    # 폭을 두 번 든다. 그러면 resolved_token/total_area가 대소문자 무시 첫
    # 매치(낡은 값)를 읽어 면적 게이트와 예산이 변경 전 폭을 보게 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": MIXED_CASE_DECK})
    agents, _ = _agents(
        [235.0, 200.0, 200.0, 200.0],
        candidates=[{"refdes": "AMP.M1", "param": "w", "direction": "decrease",
                     "reasoning": "narrower"}],
    )

    result = await run_optimization({"tb": MIXED_CASE_DECK}, _spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    line = [
        line for line in state.current_netlist_texts()["tb"].splitlines()
        if line.startswith("M1 ")
    ][0]
    assert line.lower().count("w=") == 1  # 폭 토큰이 하나뿐이다
    assert "W=1.8e-06" in line


@pytest.mark.asyncio
async def test_a_simulation_that_raises_is_a_rejected_step_not_a_crash(tmp_path):
    # 줄어드는 기하에는 바닥이 없다 - 최소 치수는 PDK 지식이라 지어낼 수
    # 없다. sky130 소자 bin을 벗어나면 ngspice가 실행을 중단하므로, 그것이
    # 예외로 새어 나가면 이미 통과한 설계가 크래시로 끝난다.
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("could not find a valid modelname")
        return {"measurements": {"iq_ua": 235.0}, "status": "success", "warnings": []}

    async def propose(structure_view, margins, objective, netlist_view):
        return {
            "candidates": [{"refdes": "AMP.M1", "param": "w", "direction": "decrease",
                            "reasoning": "narrower"}],
            "overall_reasoning": "x",
        }

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents = OptimizerAgents(propose=propose, simulate=simulate)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"  # FAIL이라는 결말은 존재하지 않는다
    assert "w=2e-6" in state.current_netlist_texts()["tb"]  # 되돌려졌다
    steps = [json.loads(line) for line in open(state.history_path)]
    reasons = [e["reason"] for e in steps if e["step"] == "optimize_step"]
    assert any("could not find a valid modelname" in (r or "") for r in reasons)


@pytest.mark.asyncio
async def test_a_simulation_that_does_not_succeed_is_a_rejected_step(tmp_path):
    # 수렴 실패한 해의 측정값으로 마진을 태우는 결정을 내리면 안 된다.
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        calls["n"] += 1
        if calls["n"] > 1:
            return {"measurements": {"iq_ua": 1.0}, "status": "convergence_failure",
                    "warnings": ["no convergence"]}
        return {"measurements": {"iq_ua": 235.0}, "status": "success", "warnings": []}

    async def propose(structure_view, margins, objective, netlist_view):
        return {
            "candidates": [{"refdes": "AMP.M1", "param": "m", "direction": "decrease",
                            "reasoning": "tail"}],
            "overall_reasoning": "x",
        }

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents = OptimizerAgents(propose=propose, simulate=simulate)

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]
    steps = [json.loads(line) for line in open(state.history_path)]
    reasons = [e["reason"] for e in steps if e["step"] == "optimize_step"]
    assert any("convergence_failure" in (r or "") for r in reasons)


AMBIGUOUS_DECK = (
    "* t\n"
    ".subckt A a b vss\n"
    "M1 a b vss vss NCH w=2e-6 l=1e-6 m=4\n"
    ".ends A\n"
    ".subckt B a b vss\n"
    "M1 a b vss vss NCH w=9e-6 l=1e-6 m=9\n"
    ".ends B\n"
    "Xa p q 0 A\n"
    "Xb q r 0 B\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_an_ambiguous_refdes_is_rejected_rather_than_read_from_one_of_them(tmp_path):
    # 값을 읽는 색인과 편집 대상을 정하는 규칙이 갈라지면 소자 X를 읽고 소자
    # Y를 쓰게 된다. 모호한 refdes는 어느 쪽도 고르지 않고 거절되어야 한다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": AMBIGUOUS_DECK})
    agents, calls = _agents(
        [235.0],
        candidates=[{"refdes": "M1", "param": "m", "direction": "decrease", "reasoning": "?"}],
    )

    result = await run_optimization({"tb": AMBIGUOUS_DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1
    assert state.current_netlist_texts()["tb"] == AMBIGUOUS_DECK
    steps = [json.loads(line) for line in open(state.history_path)]
    gated = [e for e in steps if e["step"] == "optimize_step" and e.get("gate") == "refdes"]
    assert gated and "ambiguous" in gated[0]["reason"]


@pytest.mark.asyncio
async def test_every_step_is_recorded_with_its_outcome(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])

    await run_optimization({"tb": DECK}, _spec(), state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    steps = [e for e in events if e["step"] == "optimize_step"]
    assert steps
    for e in steps:
        assert "refdes" in e and "param" in e and "accepted" in e
        assert "objective" in e and "area" in e
