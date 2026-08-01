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
    이벤트로 드러낸다."""
    from analogcoder.optimizer import AREA_PHASE

    assert AREA_PHASE.objective is AREA_OBJECTIVE
    assert AREA_PHASE.area_budget is None
    assert AREA_PHASE.guard_band is None
    assert AREA_PHASE.label == "optimize_area"


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
async def test_the_two_phases_do_not_share_event_names(tmp_path):
    """한 실행에 두 단계가 있으므로 이벤트 이름이 갈려야 한다.

    갈리지 않으면 history.jsonl 에서 어느 단계의 optimize_step 인지 알 수
    없고, 이 저장소의 측정 스크립트들이 두 단계를 하나로 읽는다. label 이
    존재하는 이유가 이것이며, 쓰이지 않으면 label 은 죽은 필드다."""
    from analogcoder.optimizer import AREA_PHASE, run_optimization
    from analogcoder.state import RunState
    from tests.unit.test_optimizer import DECK, _agents, _spec

    state = RunState(run_dir=str(tmp_path), testbench_names=["tb"])
    state.push_netlist_version({"tb": DECK})
    agents, _ = _agents([200.0, 190.0, 180.0])

    await run_optimization({"tb": DECK}, _spec(), state, agents)             # 전류 단계
    await run_optimization({"tb": DECK}, _spec(), state, agents, phase=AREA_PHASE)

    names = {
        json.loads(line)["step"]
        for line in open(state.history_path, encoding="utf-8")
    }
    # 전류 단계의 이름은 한 글자도 바뀌지 않았다.
    assert "optimize_baseline" in names
    # 면적 단계는 자기 이름을 쓴다.
    assert "optimize_area_baseline" in names


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
    "이 단계가 없다"가 구별된다."""
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
    assert set(ranked[0]) >= {"ranked", "zero_gain", "unknown", "unguarded_criteria"}


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
    events = [json.loads(line) for line in open(state.history_path, encoding="utf-8")]
    assert any(e["step"] == "optimize_area_refused" for e in events)
