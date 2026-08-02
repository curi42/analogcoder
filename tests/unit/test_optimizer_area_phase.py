"""면적 최소화 단계 - 표식, 설정, 조립."""
import json

import pytest

from analogcoder.area import DEFAULT_AREA_MODEL, total_area
from analogcoder.optimizer import AREA_OBJECTIVE, _objective_value


def test_the_area_objective_marker_is_not_a_string():
    """측정값 이름 공간과 겹칠 수 없어야 한다.

    목적 이름은 측정값 딕셔너리를 색인하는 데 쓰인다. 문자열 표식은 언젠가
    같은 이름의 진짜 measure와 부딪히고, 그 충돌은 조용하다 - 예외도 로그도
    없이 다른 양이 목적값 자리에 들어가고 탐색이 그것을 성실하게 내린다."""
    assert not isinstance(AREA_OBJECTIVE, str)
    assert AREA_OBJECTIVE != "area"


def test_the_default_area_model_is_the_shipped_total_area():
    """경계는 새로 계산하지 않는다 - 오늘의 함수를 가리킬 뿐이다."""
    assert DEFAULT_AREA_MODEL is total_area


def test_the_marker_reads_derived_area_and_a_name_reads_measurements():
    """목적값 선택 규칙 자체를 핀한다.

    오라클 밖으로 뽑는 이유는, 규칙이 오라클 안에만 있으면 시뮬레이터를
    세워야만 잴 수 있고 그러면 이 분기가 사실상 검사되지 않기 때문이다.
    덱이 `area`라는 measure를 내놓아도 표식과 섞이지 않는 것을 함께 본다."""
    measurements = {"area": 999.0, "iq_ua": 212.99}
    assert _objective_value(AREA_OBJECTIVE, measurements, derived_area=41.0) == 41.0
    assert _objective_value("iq_ua", measurements, derived_area=41.0) == 212.99
    # 없는 이름은 None이다 - 0이 아니다. 0이면 수락 규칙이 "목적값이 최선보다
    # 낮다"를 참으로 읽어 재지 못한 후보를 수락한다.
    assert _objective_value("nope", measurements, derived_area=41.0) is None


def test_phase_config_from_spec_reproduces_todays_objective_phase():
    """오늘의 전류 단계가 데이터로 정확히 표현되는지."""
    from analogcoder.optimizer import PhaseConfig, phase_from_spec
    from analogcoder.spec import OptimizeSpec

    phase = phase_from_spec(OptimizeSpec(objective="iq_ua", area_budget=1.1, guard_band=0.2))
    assert phase == PhaseConfig(
        objective="iq_ua", area_budget=1.1, guard_band=0.2, label="optimize"
    )


def test_the_area_phase_config_has_no_budget_and_no_ratio_guard():
    """면적 단계의 두 None은 서로 다른 이유를 갖는다.

    area_budget=None: 목적이 면적이고 수락 규칙이 목적의 **하강**을 요구하므로
    면적은 단조 감소한다. 예산 검사는 구조적으로 발화할 수 없고, 발화할 수 없는
    검사를 켜 두면 "검사했다"와 "검사가 무력하다"가 구별되지 않는다.

    guard_band=None: 비율 폴백은 선언에서 오는데 이 단계는 선언 없이 돈다.
    없는 숫자를 지어내지 않는다. 대신 어느 기준이 무방비인지를 Task 4가
    이벤트로 드러낸다.

    margin_floor=None: 값은 Task 4의 측정이 정한다. 지금 고르면 사후 규칙
    변경이다 - 이 저장소가 이미 명시적으로 철회한 관행이다(D1)."""
    from analogcoder.optimizer import AREA_PHASE

    assert AREA_PHASE.objective is AREA_OBJECTIVE
    assert AREA_PHASE.area_budget is None
    assert AREA_PHASE.guard_band is None
    assert AREA_PHASE.label == "optimize_area"
    assert AREA_PHASE.margin_floor is None


@pytest.mark.asyncio
async def test_an_explicit_phase_is_not_skipped_when_the_spec_declares_no_optimize(tmp_path):
    """`optimize:` 선언이 없어도 명시적 phase가 있으면 돌아야 한다.

    이것이 이 태스크의 요점이다. 오늘의 조기 반환은 "선언이 없으면 할 일이
    없다"였고, 면적 단계가 생기면 그 전제가 거짓이 된다 - 선언 없이 도는
    것이 면적 단계의 정의다. 이 한 줄을 놓치면 면적 단계가 대상 스펙 대부분에서
    조용히 SKIPPED로 끝난다."""
    from analogcoder.state import RunState
    from analogcoder.optimizer import AREA_PHASE, run_optimization
    from tests.unit.test_optimizer import DECK, _agents, _spec

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([200.0])

    result = await run_optimization(
        {"tb": DECK}, _spec(optimize=None), state, agents, phase=AREA_PHASE
    )

    assert result["status"] != "SKIPPED"


@pytest.mark.asyncio
async def test_the_two_phases_emit_disjoint_event_name_sets(tmp_path):
    """이벤트 이름 접두사가 **열 개 전부**에 실제로 걸려 있는지.

    이전 버전은 `optimize_baseline`/`optimize_area_baseline` 딱 두 이름만
    봤다 - 10개 중 1개다. `scripts/search_ab.py:238`이 실제로 거르는 이름은
    `optimize_step`인데, `SearchRun.attempt`/`_reject`의
    `f"{self._phase.label}_step"`이 리터럴 `"optimize_step"`으로 회귀해도
    (`optimize_baseline`은 여전히 옳으므로) 이 가드는 계속 초록으로 남는다 -
    조용히 무력한 게이트의 정확히 이 저장소가 겪은 모양이다.

    그래서 이름 하나씩 고정하는 대신 두 단계가 낸 이벤트 **이름의 집합이
    서로소인지**를 직접 본다 - 어느 한 이름이든 겹치면 그 이름의 이벤트는
    history.jsonl에서 두 단계 사이에 구별 불가능해지고, 그것이 정확히
    `search_ab.py`가 두 단계를 하나로 읽는 방식이다.

    `optimize_skipped`/`optimize_area_refused`/`optimize_area_failed`는
    라벨을 안 쓰는 **의도된** 예외다: 전자는 `spec.optimize is None`일 때만
    나오고(이 스펙은 선언이 있으므로 어느 쪽 실행에도 나오지 않는다), 후자
    둘은 `run_area_optimization`의 준비 구간(구조 유도·순위 계산)이 `phase`
    객체를 만들기 **전**에 죽는 자리라서 애초에 라벨을 참조할 수 없다 -
    이 테스트는 `run_optimization`을 직접 부르므로(원래 테스트와 같은 경로)
    그 준비 구간 자체를 지나지 않는다."""
    from analogcoder.optimizer import AREA_PHASE, run_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    objective_state = RunState(run_dir=str(tmp_path / "objective"), testbench_names=["tb"])
    objective_state.push_netlist_version({"tb": DECK})
    objective_agents, _ = _agents([200.0, 190.0, 180.0])
    await run_optimization({"tb": DECK}, _spec(), objective_state, objective_agents)

    area_state = RunState(run_dir=str(tmp_path / "area"), testbench_names=["tb"])
    area_state.push_netlist_version({"tb": DECK})
    area_agents, _ = _agents([200.0, 190.0, 180.0])
    await run_optimization(
        {"tb": DECK}, _spec(), area_state, area_agents, phase=AREA_PHASE
    )

    objective_names = {
        json.loads(line)["step"]
        for line in open(objective_state.history_path, encoding="utf-8")
    }
    area_names = {
        json.loads(line)["step"]
        for line in open(area_state.history_path, encoding="utf-8")
    }

    # 둘 다 실제로 여러 종류의 이벤트를 냈는지 먼저 본다 - 빈 집합끼리는
    # 언제나 서로소이므로, 아무것도 로그하지 않는 회귀에서도 통과하는
    # 단언이 되면 안 된다. 오늘은 baseline/proposal/guard_infeasible/step
    # 네 종류가 최소로 나온다.
    assert len(objective_names) >= 3
    assert len(area_names) >= 3
    assert objective_names & area_names == set()
    # 각자 자기 라벨의 이름을 쓰는지도 직접 본다.
    assert "optimize_baseline" in objective_names
    assert "optimize_area_baseline" in area_names


UNRESOLVABLE_DECK = (
    "* t\n"
    "Rload p 0 1k\n"       # w/l 이 없어 면적 모델이 아무것도 못 읽는다
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_the_area_phase_calls_no_agent_at_all(tmp_path):
    """이 단계에 LLM이 붙지 않는다는 사실을 핀한다.

    propose를 즉시 실패하는 것으로 둔다 - 나중에 누군가 "면적에도 LLM
    조언이 있으면 좋겠다"고 배선하면 이 테스트가 깨져야 한다. 안 깨지면
    LLM 없음이라는 설계의 근거가 조용히 사라진다."""
    from analogcoder.optimizer import OptimizerAgents, run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    async def boom(*args, **kwargs):
        raise AssertionError("면적 단계는 에이전트를 부르면 안 된다")

    base, _ = _agents([200.0])
    agents = OptimizerAgents(propose=boom, simulate=base.simulate)
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})

    result = await run_area_optimization({"tb": DECK}, _spec(optimize=None), state, agents)

    assert result["status"] in {"OPTIMIZED", "UNCHANGED"}


@pytest.mark.asyncio
async def test_the_area_phase_records_what_it_could_not_rank(tmp_path):
    """0 이득과 unknown이 이벤트에 서로 다른 칸으로 남는지.

    무조건 남긴다 - 순위가 비어도 이벤트가 있어야 "아무것도 못 줄였다"와
    "이 단계가 없다"가 구별된다.

    unguarded_criteria는 더 이상 이 이벤트(ranking)에 없다 - 스펙만으로
    정해지는 상수([c.name for c in spec.all_criteria])였다면 실행마다
    똑같은 값을 내 "이 로그가 아무것도 안 할 때 어떻게 보이는가"에 답할 수
    없었고, 코너 대응 실행에서는 진입 스윕이 대부분을 실측 여유분으로
    덮으므로 그 상수는 과대 보고가 아니라 틀린 이름표였다. 진짜 사실은
    allowances가 실제로 확정되는 `_optimize`의 baseline 이벤트에서 잰다."""
    from analogcoder.optimizer import run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    agents, _ = _agents([200.0])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})

    await run_area_optimization({"tb": DECK}, _spec(optimize=None), state, agents)

    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    ranked = [e for e in events if e["step"] == "optimize_area_ranking"]
    assert len(ranked) == 1
    assert set(ranked[0]) >= {"ranked", "zero_gain", "unknown"}
    assert "unguarded_criteria" not in ranked[0]

    baseline = [e for e in events if e["step"] == "optimize_area_baseline"]
    assert len(baseline) == 1
    # 면적 단계는 guard_band도(AREA_PHASE) 코너 스윕도(_spec(optimize=None)의
    # 기본 pvt_corners=None) 없으므로 allowances가 통째로 비고, 정직한 답은
    # "전 기준이 무방비"다 - _spec()의 유일한 기준 "iq".
    assert baseline[0]["unguarded_criteria"] == ["iq"]


@pytest.mark.asyncio
async def test_a_deck_whose_devices_cannot_be_resolved_is_refused_not_unchanged(tmp_path):
    """`counted == 0`은 "쟀는데 못 줄임"이 아니라 "잴 수 없음"이다.

    UNCHANGED로 합치면 면적 모델이 이 덱에서 아무것도 못 읽고 있다는 사실을
    아무도 알아채지 못한다."""
    from analogcoder.optimizer import run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import _agents, _spec

    agents, _ = _agents([200.0])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": UNRESOLVABLE_DECK})

    result = await run_area_optimization(
        {"tb": UNRESOLVABLE_DECK}, _spec(optimize=None), state, agents
    )

    assert result["status"] == "REFUSED"
    assert "counted" in result["reason"]
    # REFUSED도 나머지 결과와 같은 모양이어야 한다 - _result()를 거쳐야
    # steps_accepted/steps_rejected/final_netlist_paths가 함께 실린다.
    # 그러지 않으면 .get("steps_accepted", 0)으로 읽는 소비자가 이 결과를
    # "0단계짜리 정상 실행"으로 오독한다.
    assert "steps_accepted" in result
    assert "steps_rejected" in result
    assert "final_netlist_paths" in result
    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    assert any(e["step"] == "optimize_area_refused" for e in events)


@pytest.mark.asyncio
async def test_a_crash_in_the_area_phase_preamble_does_not_escape(tmp_path, monkeypatch):
    """면적 단계의 준비 구간(구조 유도, 노브 값 읽기, 순위 계산)에서 터진
    예외도 run_optimization과 같은 계약을 지켜야 한다 - 이 단계에도 FAIL이
    없다.

    derive_structure를 강제로 터뜨린다: 이 준비 구간은 run_optimization의
    호출 앞에 있어서 그 함수의 예외 가드 안에 있지 않다. 안 잡으면 이미
    PASS한 실행이 result.json도 report.md도 없이 트레이스백으로 끝난다 -
    이 저장소가 이미 기록한 실패 모양이다."""
    import analogcoder.optimizer as optimizer_mod
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    def boom(*args, **kwargs):
        raise ValueError("boom in derive_structure")

    monkeypatch.setattr(optimizer_mod, "derive_structure", boom)

    agents, _ = _agents([200.0])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})

    result = await optimizer_mod.run_area_optimization(
        {"tb": DECK}, _spec(optimize=None), state, agents
    )

    assert result["status"] == "UNCHANGED"
    assert result["failure"] is not None
    assert "steps_accepted" in result
    assert "final_netlist_paths" in result
    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    assert any(e["step"] == "optimize_area_failed" for e in events)


@pytest.mark.asyncio
async def test_the_area_phase_ranks_knobs_from_the_passed_deck_not_states_deck(tmp_path):
    """netlist_texts 인자와 state의 현재 덱이 다를 수 있다 - `_optimize`가
    탐색 앞에서 `state.current_netlist_texts() != netlist_texts`를 확인해
    둘을 맞추는 이유가 정확히 이것이다. 면적 순위는 그 맞춤보다 먼저 돌기
    때문에, 노브의 현재 값은 **인자로 받은 텍스트**에서 읽어야 한다 -
    state의 텍스트에서 읽으면 순위가 잘못된 기준값에서 계산된 스텝을
    이득으로 보고한다."""
    from analogcoder.optimizer import run_area_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    state_deck = DECK  # w=2e-6, l=1e-6, m=4
    passed_deck = DECK.replace("w=2e-6", "w=4e-6")  # w=4e-6, l=1e-6, m=4

    agents, _ = _agents([200.0])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": state_deck})

    await run_area_optimization({"tb": passed_deck}, _spec(optimize=None), state, agents)

    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    ranking_event = next(e for e in events if e["step"] == "optimize_area_ranking")
    ranked_by_key = {(e["refdes"], e["param"]): e["gain"] for e in ranking_event["ranked"]}

    # 0.9배 한 단계는 4e-6 -> 3.6e-6이고, 이득은 (4e-6 - 3.6e-6) * 1e-6 * 4
    # = 1.6e-12이다. state 덱(w=2e-6)에서 현재값을 읽었다면 2e-6 -> 1.8e-6이
    # 되어, base(4e-6 기준)와의 차이가 8.8e-12로 완전히 다르게 나온다.
    assert ranked_by_key[("AMP.M1", "w")] == pytest.approx(1.6e-12, rel=1e-6)


# --- 여유분 하한(MarginFloor) - 결정 지점은 _margin_floor_allowances 하나 ---


def test_the_area_phase_with_no_floor_leaves_every_criterion_unguarded():
    """하한이 없으면 코너 없는 스펙에서 모든 기준이 무방비다 - 이것이
    2026-08-02 측정이 코너에서 깨지는 것을 확인한 출하 상태다."""
    from analogcoder.optimizer import _margin_floor_allowances
    from analogcoder.spec import Criterion

    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
        Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0),
    ]
    baseline = {"gain_db": 65.0, "psrr_db": -30.0}

    # floor가 None이면 baseline이 무엇이든, 임계값이 무엇이든 결과는 늘 {}다 -
    # "잴 수 없다"가 아니라 "규칙이 없다"이므로 입력에 좌우되지 않는다.
    assert _margin_floor_allowances(baseline, criteria, None) == {}
    assert _margin_floor_allowances({}, criteria, None) == {}


def test_f1_fills_every_criterion_from_the_threshold():
    """F1 은 ratio_allowances 그 자체이므로 모든 이름이 채워진다."""
    from analogcoder.judge_tools import ratio_allowances
    from analogcoder.optimizer import MarginFloor, _margin_floor_allowances
    from analogcoder.spec import Criterion

    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
        Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0),
    ]
    floor = MarginFloor(rule="f1", value=0.1)

    # F1은 기준선을 보지 않는다 - 빈 딕셔너리를 줘도 값이 같아야 한다.
    allowances = _margin_floor_allowances({}, criteria, floor)

    assert set(allowances) == {"gain", "psrr"}
    assert allowances == ratio_allowances(criteria, 0.1)


def test_f2_fills_from_the_baseline_distance_and_names_what_it_could_not():
    """F2 는 적용 못 한 기준을 **이름으로** 남긴다 - 조용히 빠지면
    '하한이 걸렸다'와 '이 기준에는 하한이 없다'가 같아 보인다."""
    from analogcoder.optimizer import MarginFloor, _margin_floor_allowances
    from analogcoder.spec import Criterion

    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
        Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0),
    ]
    # gain은 기준선이 5의 여유를 가진다(65 - 60). psrr은 임계값에 정확히
    # 붙어 있어(-25 == -25) baseline_ratio_allowances가 제외하는 세 경우 중
    # 하나(slack == 0)를 친다.
    baseline = {"gain_db": 65.0, "psrr_db": -25.0}
    floor = MarginFloor(rule="f2", value=0.5)

    allowances = _margin_floor_allowances(baseline, criteria, floor)

    assert allowances == {"gain": 2.5}
    # psrr은 조용히 빠진 게 아니라 "이 이름이 allowances에 없다"로 확인할 수
    # 있게 빠져 있다 - guard_band_violations는 없는 이름을 여유분 0.0으로
    # 읽으므로, 여기서 "이름이 없다"는 곧 "무방비"의 정의다.
    assert "psrr" not in allowances


def test_f3_reduces_to_f1_explicitly():
    """F3 은 구별되는 규칙이 아니다 - 사전 등록이 "코너 없는 절반에서
    F1·F2의 우승자를 그대로 쓰므로 별도 격자가 없다"고 적었고, 코너 없는
    이 절반에서 f3의 정의(코너가 있으면 그것, 없으면 F1/F2) 자체가 f1과
    같은 딕셔너리를 만드는 것과 같다. 그 환원을 여기서 고정한다."""
    from analogcoder.optimizer import MarginFloor, _margin_floor_allowances
    from analogcoder.spec import Criterion

    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0)]

    f1 = _margin_floor_allowances({}, criteria, MarginFloor(rule="f1", value=0.1))
    f3 = _margin_floor_allowances({}, criteria, MarginFloor(rule="f3", value=0.1))

    assert f3 == f1


def test_the_three_rules_are_decided_in_exactly_one_place():
    """규칙이 셋으로 늘어도 결정 지점은 하나여야 한다.

    소스를 읽어 `MarginFloor.rule`을 분기하는(`rule ==`/`rule in`) 줄이
    `_margin_floor_allowances` 안에만 있는지 확인한다. compose.py가
    netlist.py의 규칙을 손으로 베껴 양방향으로 갈라진 것이 이 저장소가
    이미 치른 대가다.

    (브리프는 이 형태의 소스 스캔에 tests/unit/test_area_ranking.py의
    선례가 있다고 적었지만, 확인 결과 그 파일에는 inspect.getsource를
    쓰는 테스트가 전혀 없다 - 선례 없이 새로 쓴다.)"""
    import inspect
    import re

    from analogcoder import optimizer as optimizer_module
    from analogcoder.optimizer import _margin_floor_allowances

    module_lines = inspect.getsource(optimizer_module).splitlines()
    func_lines, func_start = inspect.getsourcelines(_margin_floor_allowances)
    func_line_numbers = set(range(func_start, func_start + len(func_lines)))

    pattern = re.compile(r"\brule\s*(==|in)\s")
    branching_lines = [i + 1 for i, line in enumerate(module_lines) if pattern.search(line)]

    # 패턴이 하나도 안 걸리면 정규식이 코드와 어긋난 것이지, "분기가 없다"는
    # 뜻이 아니다 - 침묵을 통과로 읽지 않는다.
    assert branching_lines, "no 'rule ==' / 'rule in' line found at all"
    outside = [ln for ln in branching_lines if ln not in func_line_numbers]
    assert outside == [], f"rule is branched outside _margin_floor_allowances at lines {outside}"


@pytest.mark.asyncio
async def test_margin_floor_is_actually_wired_into_the_optimize_baseline_event(tmp_path):
    """단위 함수가 아니라 `_optimize`의 allowances 조립 지점에 실제로
    배선됐는지를 본다. `_margin_floor_allowances`가 존재해도 `_optimize`가
    여전히 옛 `ratio`만 읽으면 psrr이 무방비로 남는데, 그 차이는 함수
    단위 테스트로는 안 잡힌다."""
    from types import SimpleNamespace

    from analogcoder.optimizer import MarginFloor, OptimizerAgents, PhaseConfig, run_optimization
    from analogcoder.spec import Criterion
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _spec

    tb = SimpleNamespace(
        name="tb",
        criteria=[
            Criterion(name="gain", measurement="gain_db", operator=">=", threshold=60.0),
            Criterion(name="psrr", measurement="psrr_db", operator="<=", threshold=-25.0),
        ],
        control_block=".control\nmeas dc iq_ua FIND i(Vdd) AT=27\n.endc\n",
        fragments=None,
    )
    spec = _spec(testbenches=[tb], optimize=None)

    async def simulate(netlist_texts, spec_arg):
        return {
            "measurements": {"iq_ua": 200.0, "gain_db": 65.0, "psrr_db": -25.0},
            "status": "success",
            "warnings": [],
        }

    # knob_ranking=[] (None이 아니다) - 이 테스트는 조립 지점만 보므로 LLM은
    # 부르지 않는다(OptimizerAgents.knob_ranking 참고).
    agents = OptimizerAgents(propose=None, simulate=simulate, knob_ranking=[])
    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})

    phase = PhaseConfig(
        objective="iq_ua", area_budget=None, guard_band=None, label="optimize_f2test",
        margin_floor=MarginFloor(rule="f2", value=0.5),
    )

    await run_optimization({"tb": DECK}, spec, state, agents, phase=phase)

    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    baseline = next(e for e in events if e["step"] == "optimize_f2test_baseline")

    # gain은 F2가 여유 2.5(=0.5*(65-60))를 채운다. psrr은 임계값에 정확히
    # 붙어 있어 F2가 이름으로 제외하고, _unguarded는 그 이름을 무방비로 본다.
    assert baseline["unguarded_criteria"] == ["psrr"]
