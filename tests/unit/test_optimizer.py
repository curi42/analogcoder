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
