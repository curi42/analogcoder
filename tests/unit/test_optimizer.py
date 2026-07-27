import json
from types import SimpleNamespace

import pytest

from analogcoder.agents.backend import AgentExecutionError
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
    steps = [json.loads(entry) for entry in open(state.history_path)]
    written = [e["param"] for e in steps if e["step"] == "optimize_step" and e["before"]]
    assert written and set(written) == {"W"}  # 이력이 실제로 편집한 철자를 말한다


@pytest.mark.asyncio
async def test_a_simulate_without_a_status_key_still_lets_a_step_be_accepted(tmp_path):
    # production 모양을 고정한다. cli.py의 simulate_fn은 테스트벤치별 결과를
    # 합치면서 **최상위 status를 만들지 않는다** - orchestrator도 그것을 읽지
    # 않으므로 누락이 아니라 계약이다. 없는 키를 실패로 읽으면 최적화가
    # 영구히 UNCHANGED가 된다: 크래시도 없고 이상해 보이는 로그도 없다.
    seq = [235.0, 200.0, 200.0, 200.0]
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        value = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"measurements": {"iq_ua": value}, "by_testbench": {"tb": {"status": "success"}}}

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

    assert result["status"] == "OPTIMIZED"
    assert "m=3" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_simulate_returning_a_non_mapping_is_a_rejected_step(tmp_path):
    # _run_simulation은 "아무것도 새어 나가지 않는다"고 약속한다. 출구가
    # 하나여야 그 약속이 참이다.
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        calls["n"] += 1
        if calls["n"] > 1:
            return None
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
    assert any("unusable result" in (r or "") for r in reasons)


@pytest.mark.asyncio
async def test_a_result_without_measurements_is_a_rejected_step(tmp_path):
    calls = {"n": 0}

    async def simulate(netlist_texts, spec_arg):
        calls["n"] += 1
        if calls["n"] > 1:
            return {"status": "success", "warnings": []}
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
    assert any("unusable result" in (r or "") for r in reasons)


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


# --- 최종 리뷰 Finding 1: 이 모듈의 유일한 LLM 호출이 무방비였다 ------------
# _run_simulation과 _run_sweep은 각각 "이 단계에는 FAIL 결말이 없다"는 계약
# 때문에 bare Exception을 삼킨다. agents.propose에는 그 보호가 없었다.
# ClaudeSDKBackend.run은 오류 ResultMessage 어디에서나 AgentExecutionError를
# 던지고(레이트 리밋, 전송 오류, structured_output이 None, 약한 로컬 모델의
# 스키마 실패 - 마지막 것은 CLAUDE.md가 예상된 경우로 적어 둔 것이다),
# 그것이 새어 나가면 cli.main의 asyncio.run까지 올라가 write_result_json /
# write_report_md가 아예 돌지 않는다. **이미 PASS한 실행이** result.json도
# report.md도 없이 트레이스백으로 끝난다.


def _raising_propose(exc):
    async def propose(structure_view, margins, objective, netlist_view):
        raise exc

    return propose


@pytest.mark.asyncio
async def test_a_propose_that_raises_is_not_a_crash(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, calls = _agents([235.0])
    agents.propose = _raising_propose(
        AgentExecutionError("backend returned output that does not match the schema")
    )

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"  # FAIL이라는 결말은 존재하지 않는다
    assert "does not match the schema" in result["failure"]
    assert state.current_netlist_texts()["tb"] == DECK
    events = [json.loads(line) for line in open(state.history_path)]
    failed = [e for e in events if e["step"] == "optimize_failed"]
    assert failed and "AgentExecutionError" in failed[0]["reason"]


@pytest.mark.asyncio
async def test_a_value_error_from_applying_a_change_is_not_a_crash(tmp_path, monkeypatch):
    # 주소 지정 게이트는 canonical 원문만 본다. 비-canonical 테스트벤치 덱에서
    # refdes가 모호하면 apply_changes가 ValueError를 던지고, 그 경로는 게이트가
    # 막지 못한다 - orchestrator가 같은 이유로 ValueError를 함께 잡는 것과
    # 정확히 같은 belt-and-braces다.
    import analogcoder.optimizer as optimizer_module

    def boom(text, changes):
        raise ValueError("refdes 'M1' is ambiguous")

    monkeypatch.setattr(optimizer_module, "apply_changes", boom)

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "ambiguous" in result["failure"]
    assert state.current_netlist_texts()["tb"] == DECK


@pytest.mark.asyncio
async def test_a_failed_optimization_result_is_still_shaped_like_a_normal_one(tmp_path):
    # cli.py:167-188은 이 dict를 그대로 result["optimization"]에 넣고
    # pvt_sweep / corner_failure / final_criteria를 읽는다. 실패 경로가 키를
    # 빼먹으면 크래시를 한 칸 옆으로 옮긴 것뿐이다.
    async def run(subdir, propose_exc):
        state = RunState(run_dir=str(tmp_path / subdir), testbench_names=["tb"])
        state.push_netlist_version({"tb": DECK})
        agents, _ = _agents([235.0, 200.0, 200.0, 200.0])
        if propose_exc is not None:
            agents.propose = _raising_propose(propose_exc)
        return await run_optimization({"tb": DECK}, _spec(), state, agents)

    healthy = await run("healthy", None)
    failed = await run("failed", AgentExecutionError("rate limited"))

    assert set(healthy) <= set(failed)
    assert failed["pvt_sweep"] is None
    assert failed["corner_confirmed"] is False
    assert failed["final_netlist_paths"]
    # 정상 경로는 실패 사유가 없다 - 두 경로가 구분되어야 한다.
    assert healthy["failure"] is None


# --- 최종 리뷰의 값싼 Minor들 -----------------------------------------------


@pytest.mark.asyncio
async def test_a_candidate_that_cannot_move_further_is_counted_as_a_rejection(tmp_path):
    # 이력에는 거절이 하나 남는데 보고하는 steps_rejected는 0이었다. 다른 모든
    # 거절 경로는 세므로, 결과 dict만 보는 쪽에서는 "후보가 하나도 시도되지
    # 않았다"와 구별되지 않는다.
    deck = DECK.replace("m=4", "m=1")
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": deck})
    agents, _ = _agents([235.0])

    result = await run_optimization({"tb": deck}, _spec(), state, agents)

    steps = [
        json.loads(line) for line in open(state.history_path)
        if json.loads(line)["step"] == "optimize_step"
    ]
    assert len(steps) == 1 and steps[0]["accepted"] is False
    assert result["steps_rejected"] == len([s for s in steps if not s["accepted"]]) == 1


PEER_DECK = (
    "* t\n"
    ".subckt CORE a b vss\n"
    "Xq1 a b vss pnp_05v5\n"
    "Xq8 a b vss pnp_05v5 m=8\n"
    ".ends CORE\n"
    "Xc p q 0 CORE\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_a_param_admitted_by_the_peer_rule_but_absent_from_the_line_says_so(tmp_path):
    # bandgap의 Xq1.m 그대로다: check_param_applicability는 동료 규칙으로
    # 이것을 **admit** 하는데(m이 이미터 면적비를 정하는 유일한 노브라 그
    # 규칙이 존재한다), 정작 Xq1 줄에는 m=이 없어 출발 값이 없다. 값을
    # 지어내지 않는 것이 옳지만, 그 사유가 "값을 못 읽었다"로 뭉개지면
    # 해소 불가능한 표현식과 구별되지 않는다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": PEER_DECK})
    agents, calls = _agents(
        [235.0],
        candidates=[{"refdes": "CORE.Xq1", "param": "m", "direction": "decrease",
                     "reasoning": "emitter area ratio"}],
    )

    result = await run_optimization({"tb": PEER_DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert calls["n"] == 1
    steps = [
        json.loads(line) for line in open(state.history_path)
        if json.loads(line)["step"] == "optimize_step"
    ]
    assert len(steps) == 1
    assert steps[0]["gate"] is None  # 게이트가 막은 것이 아니다 - 동료 규칙이 통과시켰다
    assert "does not write" in steps[0]["reason"]
    assert "cannot read a numeric" not in steps[0]["reason"]
    assert result["steps_rejected"] == 1


@pytest.mark.asyncio
async def test_running_out_of_the_step_budget_is_its_own_event(tmp_path, monkeypatch):
    # "예산이 떨어졌다"와 "후보를 전부 소진했다"는 다른 사실이다. 이력에서
    # 구별되지 않으면 탐색이 왜 멈췄는지 아무도 답할 수 없다.
    import analogcoder.optimizer as optimizer_module

    monkeypatch.setattr(optimizer_module, "MAX_OPTIMIZE_STEPS", 2)
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    # 매 단계 개선되므로 후보는 절대 소진되지 않는다 - 멈추는 이유는 예산뿐이다.
    agents, _ = _agents([235.0, 220.0, 210.0, 200.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["steps_accepted"] == 2
    events = [json.loads(line) for line in open(state.history_path)]
    exhausted = [e for e in events if e["step"] == "optimize_budget_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["steps"] == 2


@pytest.mark.asyncio
async def test_exhausting_the_candidates_is_not_reported_as_a_spent_budget(tmp_path):
    # 반대 방향도 고정한다 - 두 사실이 구별되지 않으면 새 이벤트를 붙인 의미가
    # 없다. m=1은 더 내려갈 곳이 없어 후보가 소진되고, 예산은 남아 있다.
    deck = DECK.replace("m=4", "m=1")
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": deck})
    agents, _ = _agents([235.0])

    await run_optimization({"tb": deck}, _spec(), state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    assert not [e for e in events if e["step"] == "optimize_budget_exhausted"]
