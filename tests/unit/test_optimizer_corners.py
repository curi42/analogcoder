import json
import math

import pytest

from analogcoder.optimizer import OptimizerAgents, run_optimization
from analogcoder.spec import Criterion, OptimizeSpec
from analogcoder.state import RunState
from types import SimpleNamespace

# Task 5의 헬퍼를 그대로 쓴다. 다시 정의하면 둘 중 하나가 반드시 드리프트하고,
# 그때 이 파일은 production이 아니라 자기 사본을 검증하게 된다.
from tests.unit.test_optimizer import DECK, _agents, _spec


def _corner_spec(**overrides):
    spec = _spec(**overrides)
    spec.pvt_corners = SimpleNamespace(process=["tt"], voltage=[1.8], temperature=[27.0])
    return spec


def _sweep(overall_pass, iq_actual):
    return {"overall_pass": overall_pass, "summary": "x",
            "criteria": [{"name": "iq", "actual": iq_actual}], "worst_case_corners": {}}


@pytest.mark.asyncio
async def test_without_corners_the_result_says_it_was_not_corner_confirmed(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["corner_confirmed"] is False


@pytest.mark.asyncio
async def test_a_starting_design_that_fails_corners_is_not_optimized(tmp_path):
    # 코너를 못 버티는 설계에서 마진을 더 깎을 이유가 없다.
    #
    # 측정 시퀀스가 [235.0] 하나뿐이면 첫 단계가 "목적값 미개선"으로 거절되어
    # 진입 게이트가 없어도 이 테스트가 통과한다(브리프 원본이 그랬고, 게이트를
    # 지운 변이가 실제로 통과하는 것을 확인했다). 200 을 붙여 **수락될 단계가
    # 존재하게** 만들어야 게이트가 유일한 원인이 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, calls = _agents([235.0, 200.0, 200.0, 200.0])
    agents.verify_corners = lambda texts: _sweep(False, 320.0)

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert result["steps_accepted"] == 0
    assert "m=4" in state.current_netlist_texts()["tb"]  # 한 단계도 밟지 않았다
    assert calls["n"] == 1  # 기준선 측정 한 번뿐 - 탐색 자체가 시작되지 않았다
    # 스윕은 돌았고 "실패"라고 답했다. 못 돈 것이 아니므로 corner_failure는 없다.
    assert result["corner_failure"] is None
    assert result["pvt_sweep"]["overall_pass"] is False


@pytest.mark.asyncio
async def test_the_allowance_comes_from_the_measured_corner_spread(tmp_path):
    # nominal 235, 최악 코너 268 -> 여유분 33. 그러면 허용선은 267 이고,
    # 목적값이 내려가도 267 을 넘는 단계는 수락되면 안 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 270.0, 270.0, 270.0])
    agents.verify_corners = lambda texts: _sweep(True, 268.0)

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    # 270 은 iq<=300 을 통과하지만 267 이라는 실측 허용선을 넘는다.
    assert result["status"] == "UNCHANGED"


# 위 테스트는 실측 여유분을 **고정하지 못한다**: guard_band 0.2, 임계값 300이면
# 비율 여유분이 60(허용선 240)이라 270 짜리 단계는 어느 쪽으로 계산해도
# 거절된다. corner_allowances 호출을 ratio_allowances로 바꿔 돌려 보고 확인했다 -
# 브리프의 여섯 테스트가 전부 그대로 통과한다. 아래 두 테스트가 두 계산이
# **서로 다른 답을 내는** 지점을 잡는다. 하나는 실측이 더 느슨한 쪽, 하나는 더
# 빡빡한 쪽이다 - 코너에 둔감한 기준은 여유를 더 쓸 수 있고 민감한 기준은
# 자동으로 보수적이 되는 것이 실측 여유분을 쓰는 이유 전부이기 때문이다.


@pytest.mark.asyncio
async def test_a_corner_insensitive_criterion_gets_more_room_than_the_ratio_guess(tmp_path):
    # 비율 여유분 0.8*300 = 240 -> 허용선 60. 실측 여유분 |240-235| = 5 ->
    # 허용선 295. 200 짜리 단계는 실측 기준으로는 수락, 비율 추측으로는 거절이다.
    spec = _corner_spec(
        optimize=OptimizeSpec(objective="iq_ua", area_budget=1.10, guard_band=0.8)
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])
    agents.verify_corners = lambda texts: _sweep(True, 240.0)

    result = await run_optimization({"tb": DECK}, spec, state, agents)

    assert result["status"] == "OPTIMIZED"
    assert "m=3" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_corner_sensitive_criterion_gets_less_room_than_the_ratio_guess(tmp_path):
    # 비율 여유분 0.02*300 = 6 -> 허용선 294. 실측 여유분 |340-280| = 60 ->
    # 허용선 240. 270 짜리 단계는 비율 추측으로는 수락되지만 코너가 60 만큼
    # 밀어내는 것이 측정되었으므로 수락하면 안 된다.
    spec = _corner_spec(
        optimize=OptimizeSpec(objective="iq_ua", area_budget=1.10, guard_band=0.02)
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([280.0, 270.0, 270.0, 270.0])
    agents.verify_corners = lambda texts: _sweep(True, 340.0)

    result = await run_optimization({"tb": DECK}, spec, state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


TWO_CRITERIA = [
    Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=300.0),
    Criterion(name="gain", measurement="gain_db", operator=">=", threshold=40.0),
]


def _two_criteria_spec():
    tb = SimpleNamespace(name="tb", criteria=list(TWO_CRITERIA), control_block="")
    return _corner_spec(testbenches=[tb])


def _two_measurement_agents(seq):
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

    return OptimizerAgents(propose=propose, simulate=simulate), calls


# 실측 여유분 표에는 **구멍이 생길 수 있다**: corner_allowances는 스윕이나
# nominal이 값을 주지 않은 기준을 의도적으로 뺀다. 그런데 소비자인
# guard_band_violations는 없는 이름을 여유분 0(=가드밴드 없음)으로 읽는다.
# 그대로 넘기면 코너 거동을 **모르는** 기준에서만 가드가 사라져, 대체하려던
# 비율 가드보다 느슨해진다. 아래 세 테스트는 구멍이 생기는 세 경로를 각각
# 잡는다 - 병합하지 않으면 셋 다 OPTIMIZED로 넘어간다.


@pytest.mark.asyncio
async def test_a_criterion_the_sweep_omits_keeps_the_ratio_guard(tmp_path):
    # gain 은 스윕에 없다. 비율 여유분 0.2*40 = 8 -> 허용선 48. 42 는 gain>=40 을
    # 통과하지만 48 을 못 지키므로 수락되면 안 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _two_measurement_agents([
        {"iq_ua": 235.0, "gain_db": 60.0},
        {"iq_ua": 200.0, "gain_db": 42.0},
    ])
    agents.verify_corners = lambda texts: _sweep(True, 268.0)  # criteria 에 iq 뿐

    result = await run_optimization({"tb": DECK}, _two_criteria_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_criterion_missing_from_the_nominal_baseline_keeps_the_ratio_guard(tmp_path):
    # 스윕에는 gain 이 있지만 **기준선 측정**에 gain_db 가 없다. 두 값의 차를
    # 낼 수 없으니 corner_allowances가 그 기준을 뺀다. 스윕 결함이 없어도
    # 도달하는 경로다: 기준선은 cli.py의 LLM 매개 simulate_fn에서, 스윕은
    # sim_backend.run 에서 나오는 서로 다른 추출 경로다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _two_measurement_agents([
        {"iq_ua": 235.0},                      # gain_db 없음
        {"iq_ua": 200.0, "gain_db": 42.0},
    ])
    agents.verify_corners = lambda texts: {
        "overall_pass": True, "summary": "x", "worst_case_corners": {},
        "criteria": [{"name": "iq", "actual": 268.0}, {"name": "gain", "actual": 55.0}],
    }

    result = await run_optimization({"tb": DECK}, _two_criteria_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_criterion_whose_corner_value_is_nan_keeps_the_ratio_guard(tmp_path):
    # 어느 코너가 gain 을 아예 못 냈다 - pvt.py는 그것을 nan 으로 돌려준다.
    # 코너 거동을 모른다는 뜻이므로 가드를 잃을 자리가 아니다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _two_measurement_agents([
        {"iq_ua": 235.0, "gain_db": 60.0},
        {"iq_ua": 200.0, "gain_db": 42.0},
    ])
    agents.verify_corners = lambda texts: {
        "overall_pass": True, "summary": "x", "worst_case_corners": {},
        "criteria": [{"name": "iq", "actual": 268.0}, {"name": "gain", "actual": math.nan}],
    }

    result = await run_optimization({"tb": DECK}, _two_criteria_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


@pytest.mark.asyncio
async def test_a_confirmed_optimization_reports_the_sweep_it_passed(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])
    agents.verify_corners = lambda texts: _sweep(True, 240.0)

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "OPTIMIZED"
    assert result["corner_confirmed"] is True
    assert result["pvt_sweep"]["overall_pass"] is True


@pytest.mark.asyncio
async def test_a_failed_confirmation_bisects_back_to_the_last_passing_version(tmp_path):
    # 진입은 통과, 확인은 실패. 이분 탐색이 통과하는 마지막 지점에 착지해야
    # 하고, 시작점보다 나빠지면 안 된다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 220.0, 210.0, 200.0, 200.0, 200.0])
    sweeps = {"n": 0}

    def verify(texts):
        sweeps["n"] += 1
        # 진입 통과, 이후 m=2 이하로 내려간 것만 실패한다고 본다.
        failing = "m=2" in texts["tb"] or "m=1" in texts["tb"]
        return _sweep(not failing, 268.0)

    agents.verify_corners = verify

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["pvt_sweep"]["overall_pass"] is True
    assert "m=3" in state.current_netlist_texts()["tb"]
    # 브리프의 `<= 6`은 선형 역주행(5회)도 통과시킨다. 실제 상한을 건다:
    # 수락 3단계 -> 진입 1 + 확인 1 + 프로브 ceil(log2 3) = 4회, 정확히.
    assert sweeps["n"] == math.ceil(math.log2(3)) + 2 == 4


@pytest.mark.asyncio
async def test_when_no_step_survives_corners_the_start_is_returned_unchanged(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])
    entry = {"n": 0}

    def verify(texts):
        entry["n"] += 1
        return _sweep(entry["n"] == 1, 268.0)  # 진입만 통과

    agents.verify_corners = verify

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]


# --- 브리프 밖: 스윕 자체가 터지는 경우 -------------------------------------
# run_full_pvt_sweep은 코너마다 sim_backend.run을 부른다. 그것이 예외를 던지는
# 것은 이 저장소에서 이미 실재하는 경로다(_run_simulation이 존재하는 이유가
# 그것이다 - sky130 소자 bin을 벗어나면 ngspice가 실행을 중단한다). 최적화에는
# FAIL 결말이 없다는 계약이 있으므로, 스윕의 예외가 새어 나가면 이미 통과한
# 실행이 크래시로 끝난다 - 바닥 규칙("시작보다 나쁜 결과를 내지 않는다")의
# 가장 나쁜 위반이다. 실패한 스윕은 "통과하지 않은 스윕"으로 접고 사유를
# 이력에 남긴다(조용히 무력화되지 않게).


@pytest.mark.asyncio
async def test_an_entry_sweep_that_raises_stops_optimization_without_crashing(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 200.0, 200.0, 200.0])

    def verify(texts):
        raise RuntimeError("ngspice aborted")

    agents.verify_corners = verify

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert result["corner_confirmed"] is False
    assert "m=4" in state.current_netlist_texts()["tb"]
    events = [json.loads(line) for line in open(state.history_path)]
    reasons = [e.get("reason") for e in events if e["step"] == "optimize_entry_sweep"]
    assert any("ngspice aborted" in (r or "") for r in reasons)
    # 사유가 history.jsonl에만 있으면 결과 dict만 보는 쪽(Task 7)에서는
    # "코너를 잴 수단이 없었다"와 구분되지 않는다 - 조용한 무력화가 한 층
    # 위에서 되살아난다. 결과가 스스로 말해야 한다.
    assert "ngspice aborted" in result["corner_failure"]


@pytest.mark.asyncio
async def test_a_crashed_sweep_is_not_the_same_result_as_no_corners_configured(tmp_path):
    async def run(subdir, spec, verify):
        state = RunState(run_dir=str(tmp_path / subdir), testbench_names=["tb"])
        state.push_netlist_version({"tb": DECK})
        agents, _ = _agents([235.0, 235.0, 235.0])
        agents.verify_corners = verify
        return await run_optimization({"tb": DECK}, spec, state, agents)

    def boom(texts):
        raise RuntimeError("ngspice aborted")

    crashed = await run("crashed", _corner_spec(), boom)
    no_corners = await run("no_corners", _spec(), None)

    # 둘 다 UNCHANGED / corner_confirmed=False / pvt_sweep=None 이지만,
    # 하나는 "잴 수단이 없었다"이고 하나는 "재려다 터졌다"이다.
    assert crashed["pvt_sweep"] is None and no_corners["pvt_sweep"] is None
    assert crashed["corner_failure"] != no_corners["corner_failure"]
    assert no_corners["corner_failure"] is None


@pytest.mark.asyncio
async def test_a_confirmation_sweep_that_raises_walks_back_to_the_anchor(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 220.0, 210.0, 200.0, 200.0, 200.0])
    calls = {"n": 0}

    def verify(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            return _sweep(True, 268.0)
        raise RuntimeError("ngspice aborted")

    agents.verify_corners = verify

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    # 통과가 확인된 마지막 지점은 앵커뿐이다.
    assert result["status"] == "UNCHANGED"
    assert "m=4" in state.current_netlist_texts()["tb"]
    assert result["pvt_sweep"]["overall_pass"] is True  # 앵커의 진입 스윕
    # "코너가 깨져서 되돌아왔다"와 "스윕을 못 돌려서 되돌아왔다"는 다른
    # 사실이고, 후자는 고칠 대상이 회로가 아니다.
    assert "ngspice aborted" in result["corner_failure"]


@pytest.mark.asyncio
async def test_the_entry_and_confirmation_sweeps_are_both_recorded(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 220.0, 210.0, 200.0, 200.0, 200.0])

    def verify(texts):
        sweep = _sweep("m=2" not in texts["tb"] and "m=1" not in texts["tb"], 268.0)
        sweep["worst_case_corners"] = {"iq": {"process": "ss", "voltage": 1.62,
                                              "temperature": 125.0, "value": 268.0}}
        return sweep

    agents.verify_corners = verify

    await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    steps = [e["step"] for e in events]
    assert "optimize_entry_sweep" in steps
    assert "optimize_confirm_sweep" in steps
    probes = [e for e in events if e["step"] == "optimize_bisect_probe"]
    assert probes  # 어느 버전을 확인했는지가 이력에 남는다
    assert all("version" in e and "overall_pass" in e for e in probes)
    # 어느 코너가 그 기준을 밀어냈는가 - "왜 여기서 멈췄나"를 묻는 사람이
    # 실제로 원하는 것이고, 스윕의 worst_case_corners에만 있다.
    sweep_events = [e for e in events if e["step"].startswith("optimize_") and "overall_pass" in e]
    assert sweep_events
    assert all(e["worst_case_corners"] == {
        "iq": {"process": "ss", "voltage": 1.62, "temperature": 125.0, "value": 268.0}
    } for e in sweep_events)


# --- 최종 리뷰 Finding 2: 리포트가 최적화 **전** 회로를 설명하고 있었다 ------
# result["final_criteria"]는 run_orchestration의 judge 결과 - 최적화 전 덱이다.
# cli.py는 final_netlist_paths만 착지 버전으로 갱신하고 final_criteria는 그대로
# 두므로, 실측 bandgap 실행의 리포트는 212.25uA를 재는 넷리스트 옆에 212.99uA를
# 적었다. 결과는 돌려주는 덱을 설명해야 한다.


@pytest.mark.asyncio
async def test_the_reported_criteria_belong_to_the_version_bisection_landed_on(tmp_path):
    # 진입 통과, 확인 실패 -> 이분 탐색이 m=3(220)에 착지한다. 마지막으로
    # 수락된 단계는 m=1(200)이므로, 마지막 단계의 수치를 싣는 구현은 여기서
    # 깨진다 - 그것이 이 테스트의 전부다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 220.0, 210.0, 200.0, 200.0, 200.0])
    agents.verify_corners = lambda texts: _sweep(
        not ("m=2" in texts["tb"] or "m=1" in texts["tb"]), 268.0
    )

    result = await run_optimization({"tb": DECK}, _corner_spec(), state, agents)

    assert "m=3" in state.current_netlist_texts()["tb"]
    assert result["objective_after"] == 220.0
    criteria = result["final_criteria"]
    assert [c["name"] for c in criteria] == ["iq"]
    assert criteria[0]["actual"] == 220.0   # 200.0(마지막 수락 단계)이 아니다
    assert criteria[0]["pass"] is True


@pytest.mark.asyncio
async def test_an_unchanged_run_reports_the_baseline_criteria(tmp_path):
    # 한 단계도 살아남지 않으면 돌려주는 덱은 시작 덱이고, 기준도 기준선의
    # 것이어야 한다 - 비어 있으면 리포트가 아무것도 못 쓴다.
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([235.0, 260.0, 260.0, 260.0])

    result = await run_optimization({"tb": DECK}, _spec(), state, agents)

    assert result["status"] == "UNCHANGED"
    assert [c["actual"] for c in result["final_criteria"]] == [235.0]
