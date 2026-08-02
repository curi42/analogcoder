import math
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area import DEFAULT_AREA_MODEL, total_area
from analogcoder.area_limits import check_area_growth, index_baseline_components, is_count_param
from analogcoder.area_ranking import rank_by_area_gain
from analogcoder.judge_tools import (
    baseline_ratio_allowances,
    corner_allowances,
    evaluate_criteria,
    guard_band_violations,
    ratio_allowances,
    relative_slack,
)
from analogcoder.netlist import (
    Component,
    apply_changes,
    check_param_applicability,
    check_refdes_resolution,
    check_stimulus_untouched,
    parse_spice_value,
)
from analogcoder.patterns import find_patterns
from analogcoder.signal_path import build_signal_paths
from analogcoder.spec import Criterion
from analogcoder.structure import derive_structure
from analogcoder.structure_view import render_netlist, render_structure, select_focus

MAX_OPTIMIZE_STEPS = 20
STEP_RATIO = 0.9


class _AreaObjective:
    """목적이 **파생 면적**이라는 표식.

    문자열이 아닌 이유가 이 클래스의 전부다: 목적 이름은 측정값 딕셔너리의
    키이므로 문자열 표식은 언젠가 같은 이름의 진짜 measure와 부딪힌다."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "area"


AREA_OBJECTIVE = _AreaObjective()


def _objective_value(
    objective: "str | _AreaObjective", measurements: dict, derived_area: float | None
) -> float | None:
    """목적값 하나를 고른다. 면적 표식이면 파생값, 이름이면 측정값이다."""
    if objective is AREA_OBJECTIVE:
        return derived_area
    return measurements.get(objective)


@dataclass(frozen=True)
class MarginFloor:
    """코너를 잴 수 없을 때 쓸 여유분 하한 규칙 하나. `PhaseConfig`와 같은
    계약이다 - **분기가 아니라 데이터다.**

    `rule`은 `"f1"` / `"f2"` / `"f3"`. `value`의 단위는 rule에 따라 다르다 -
    f1(과 f1로 환원되는 f3)은 임계값에 곱할 비율 `g`, f2는 기준선 여유에
    곱할 배율 `r`이다. 두 규칙이 원래 다른 단위의 상수를 받으므로 이 자체가
    문제가 아니다 - 규칙을 갈라 읽는 곳은 `_margin_floor_allowances` 하나뿐이다."""

    rule: str
    value: float


@dataclass(frozen=True)
class PhaseConfig:
    """최적화 단계 하나의 설정. **분기가 아니라 데이터다.**

    단계가 둘이 되는 순간 `if 면적단계:`가 오라클·수락·이벤트 세 곳에
    흩어지고, 셋 중 하나를 고치지 않으면 조용히 갈라진다. 이 저장소가
    compose.py가 netlist.py의 규칙을 손으로 베껴 양방향으로 갈라진 것으로
    이미 겪은 모양이다."""

    # 문자열이면 측정값 이름, AREA_OBJECTIVE면 파생 면적.
    objective: "str | _AreaObjective"
    # None이면 예산 검사를 하지 않는다.
    area_budget: float | None
    # None이면 비율 폴백이 없다. 실측 여유분만 쓴다. 코너를 잴 수 있으면
    # 비율 위에 코너 실측을 덮어쓰는 바탕이 되고, **코너를 잴 수 없고
    # margin_floor도 없으면 그 실행의 여유분 전부다** - 오늘 `optimize:`를
    # 선언한 코너 없는 스펙이 모두 그 경로다. 그래도 이 필드를
    # margin_floor와 겸용하지 않는다: 한 필드가 두 뜻을 가지면 안 되고,
    # 하한이 있는 실행에서는 하한이 이 값을 **대체**한다(더하지 않는다).
    guard_band: float | None
    # 이벤트 이름 접두사. 기존 optimize_*를 읽는 쪽이 새 단계의 이벤트를
    # 오늘의 것으로 오독하면 안 된다.
    label: str
    # None이면 하한이 없다 - 코너를 잴 수 없을 때 모든 기준이 무방비로
    # 남는다(오늘의 면적 단계 출하 상태). 값은 Task 4의 측정이 정한다 - 지금
    # 고르면 사후 규칙 변경이다.
    margin_floor: MarginFloor | None = None


def phase_from_spec(optimize) -> PhaseConfig:
    """오늘의 전류 단계를 데이터로. 흐르는 값이 한 글자도 다르지 않다."""
    return PhaseConfig(
        objective=optimize.objective,
        area_budget=optimize.area_budget,
        guard_band=optimize.guard_band,
        label="optimize",
    )


AREA_PHASE = PhaseConfig(
    objective=AREA_OBJECTIVE, area_budget=None, guard_band=None, label="optimize_area",
    margin_floor=None,
)


@dataclass
class OptimizerAgents:
    propose: Callable
    simulate: Callable
    # 코너 스윕. `(netlist_texts) -> pvt.run_full_pvt_sweep의 결과 dict`인
    # **동기** 콜러블이다 - run_full_pvt_sweep 자체가 동기이고 LLM이 끼지
    # 않는다(코너 변동은 순수하게 기계적이다). None이면 코너를 잴 수단이
    # 없다는 뜻이고, 그때는 검증했다고 말하지 않는다.
    #
    # frozen이 아닌 것은 의도다: 테스트가 구성 후에 대입한다.
    verify_corners: Callable | None = None
    # 탐색 전략. None이면 coordinate_descent - 오늘까지의 동작 그대로다.
    # 이름으로 고르는 쪽은 SEARCH_STRATEGIES를 쓴다(scripts/search_ab.py).
    search_strategy: "SearchStrategy | None" = None
    # 노브 순위를 **고정**으로 주입한다. 주지 않으면(None) propose를 부른다 -
    # 기본 경로는 바뀌지 않는다.
    #
    # 이것이 있는 이유는 하나다: 탐색기를 비교할 때 실행에서 LLM을 통째로
    # 빼기 위해서다. SPICE는 결정론적이므로 LLM만 빠지면 실행 전체가
    # 결정론적이 되고, 두 전략의 차이를 표본 몇 개로 판정할 수 있다 -
    # 그것이 로드맵 단계 3·4의 판정 방식이다. LLM이 남아 있으면 같은 입력에
    # 다른 순위가 나오고(이 저장소는 한 넷리스트에서 93/26/1개의 역할을 받은
    # 전례가 있다), 그 분산이 탐색기 차이보다 커진다.
    knob_ranking: list[dict] | None = None


def _next_value(current: float, integer: bool, direction: str) -> float | None:
    """한 단계 이동한 값. 더 갈 수 없으면 None (후보 소진).

    개수 파라미터(m/nf에 도달하는 것)는 0.9배가 의미를 갖지 않는다. 다음
    정수로 가고 1 미만으로는 내려가지 않는다.

    **줄어드는 쪽에는 바닥이 없다.** 최소 기하 치수는 PDK 지식이고 이
    프로젝트는 그것을 추측하는 것을 금한다 (CLAUDE.md: sky130 소자 모델은
    binned이고 bin을 벗어나면 경고가 아니라 실행 중단이다). 숫자를 지어내는
    대신 시뮬레이션 실패를 단계 거절로 받는다 - _run_step_simulation 참고."""
    if integer:
        step = -1 if direction == "decrease" else 1
        nxt = int(current) + step
        return None if nxt < 1 else float(nxt)
    return current * STEP_RATIO if direction == "decrease" else current / STEP_RATIO


def _format_value(value: float, integer: bool) -> str:
    """넷리스트에 쓸 문자열. 개수 파라미터는 정수로 - `m=3.0`은
    area_limits의 정수성 검사가 거부한다."""
    if integer:
        return str(int(round(value)))
    return f"{value:.6g}"


def _deck_token(component: Component, param: str) -> str | None:
    """이 소자의 줄에 **실제로 적힌 철자**의 토큰 이름. 없으면 None.

    철자를 그대로 돌려주는 것이 핵심이다. SPICE는 대소문자를 안 가리지만
    apply_changes의 _replace_param은 `f"{param}="` 접두로 찾으므로 **가린다**.
    덱이 `W=2e-6`인데 제안이 `w`라고 쓴 것을 접어서 읽고 `w`로 되쓰면,
    apply_changes가 기존 토큰을 못 찾아 `w=...`를 하나 더 붙인다 - 덱이 폭을
    두 번 들고, resolved_token/total_area는 대소문자 무시 **첫** 매치(낡은
    `W=2e-6`)를 읽으므로 에어리어 게이트와 예산이 그때부터 변경 전 폭을 본다."""
    if param == "value":
        return "value"
    if param in component.params:
        return param
    for name in component.params:
        if name.lower() == param.lower():
            return name
    return None


def _current_value(component: Component, token: str) -> float | None:
    """넷리스트에 적힌 현재 값. 읽지 못하면 None - 추측하지 않는다.

    후보가 값을 실어 보낼 수 없으므로(OPTIMIZER_SCHEMA가
    additionalProperties: false) 출발점은 언제나 넷리스트 원문이다.
    component.resolved_params(해소된 수치)가 아니라 원문 문자열을 읽는다:
    `W='wn*2'`의 해소값 2e-6을 알더라도 그 소자의 크기가 다른 곳에서
    정해진다는 뜻이라, 리터럴로 덮어쓰면 설계자의 공유 파라미터를 끊는다.
    원문은 parse_spice_value에서 ValueError가 나므로 자연히 후보가 소진된다."""
    raw = component.value if token == "value" else component.params.get(token)
    if raw is None:
        return None
    try:
        return parse_spice_value(raw)
    except ValueError:
        return None


def _gate_addressing(netlist_text: str, change: dict) -> tuple[str | None, str | None]:
    """값을 읽기 **전에** 돌 수 있는 세 게이트. (막은 게이트 이름, 피드백).

    셋 다 new_value를 안 보므로 현재 값 읽기보다 앞에 둔다. 뒤에 두면 해석
    불가능한 refdes가 "현재 값을 못 읽었다"로 보고되어, 실제 원인(그런 소자가
    없다/모호하다)을 자기 게이트가 말하지 못한다.

    **전부 넷리스트 원문을 읽는다 - 접힌 뷰가 아니다.** 초점 뷰는 프롬프트
    전용이고, 게이트가 접힌 덱을 읽으면 그 안의 소자를 "존재하지 않음"으로
    판정해 설계를 뒤집는다.

    check_stimulus_untouched는 여기서 재사용이 아니라 전제다: 전류를 줄이는
    가장 싼 길은 공급을 낮추는 것이고, 목적이 전류일 때 그 퇴화 해는 튜닝
    때보다 훨씬 가깝다."""
    for name, (ok, feedback) in (
        ("refdes", check_refdes_resolution(netlist_text, [change])),
        ("param", check_param_applicability(netlist_text, [change])),
        ("stimulus", check_stimulus_untouched(netlist_text, [change])),
    ):
        if not ok:
            return name, feedback
    return None, None


async def _run_simulation(simulate, netlist_texts: dict[str, str], spec) -> tuple[dict | None, str | None]:
    """(측정 결과, 실패 이유). 실패하면 (None, 이유).

    예외를 삼키는 이유는 이 단계에 **FAIL 결과가 없다**는 계약 때문이다.
    최적화는 이미 통과한 설계 위에서 도는 것이라, 여기서 예외가 새어 나가면
    통과한 실행을 크래시로 바꾼다.

    실제로 도달 가능한 경로다: 이 루프의 기본 방향은 폭을 줄여 전류를 줄이는
    것이고 줄어드는 쪽에는 바닥이 없다. sky130 소자 모델은 binned이라 wmin
    아래로 내려가면 ngspice가 `could not find a valid modelname`으로 실행을
    **중단**한다 - 경고가 아니다. 최소 치수를 숫자로 박는 대신(그것은 PDK
    지식이고 이 프로젝트는 추측을 금한다) 실패를 단계 거절로 받는다.

    status가 명시적으로 success가 아닌 경우도 같이 막는다 - 수렴 실패한 해의
    측정값으로 마진을 태우는 결정을 내리면 안 된다. 그러나 **키가 없는 것은
    실패가 아니다.** 이 프로젝트의 유일한 실제 simulate 콜러블(cli.py의
    simulate_fn)은 테스트벤치별 결과를 합치면서 최상위 status를 만들지 않고,
    orchestrator도 최상위 status를 읽지 않는다 - 그것이 누락이 아니라 계약이다.
    없는 키를 실패로 읽으면 최적화가 영구히 UNCHANGED가 된다: 크래시도 없고
    이상해 보이는 로그도 없이 모든 단계가 거절된다. 이 저장소가 세 번 겪은
    조용한 무력화와 같은 모양이라, 기본값은 success다. (테스트벤치를 가로지르는
    진짜 status 신호는 Task 7이 cli.py 쪽에서 만든다.)

    docstring이 "아무것도 새어 나가지 않는다"고 약속하는 이상 출구는 하나여야
    한다 - 그래서 result의 모양 검사도 전부 이 안에서 한다."""
    try:
        result = await simulate(netlist_texts, spec)
    except Exception as exc:  # noqa: BLE001 - 계약상 어떤 실패도 결과를 바꾸면 안 된다
        return None, f"simulation raised {type(exc).__name__}: {exc}"

    try:
        status = result.get("status", "success")
        measurements = result["measurements"]
    except Exception as exc:  # noqa: BLE001 - dict가 아니거나 measurements가 없다
        return None, f"simulation returned an unusable result ({type(exc).__name__}: {exc})"

    if status != "success":
        return None, f"simulation did not succeed (status={status!r})"
    if not isinstance(measurements, dict):
        return None, (
            f"simulation returned 'measurements' of type {type(measurements).__name__}, "
            f"not a mapping"
        )
    return result, None


def _run_sweep(verify_corners, netlist_texts: dict[str, str]) -> tuple[dict | None, str | None]:
    """(스윕 결과, 실패 이유). 실패하면 (None, 이유).

    _run_simulation과 같은 계약이고 같은 이유다: 이 단계에는 **FAIL 결과가
    없다.** 코너 스윕은 코너마다 sim_backend.run을 부르고(pvt.run_full_pvt_sweep),
    ngspice는 소자 bin을 벗어나면 경고가 아니라 실행 중단으로 답한다. 그
    예외가 새어 나가면 이미 통과한 실행이 크래시로 끝나는데, 그것이 바닥
    규칙("최적화는 시작보다 나쁜 결과를 내지 않는다")의 가장 나쁜 위반이다.

    실패한 스윕은 "통과하지 않은 스윕"으로 접는다 - 진입에서는 최적화를 하지
    않는 쪽, 이분 탐색에서는 앵커 쪽으로 미는 쪽이라 어느 자리에서도 보수적인
    방향이다. 대신 사유를 반드시 이력에 남긴다: 조용히 무력화되는 것이 이
    저장소가 반복해서 당한 실패 모양이라, 스윕이 늘 터지는 환경에서 최적화가
    영구히 UNCHANGED가 되더라도 그 이유가 history에 보여야 한다."""
    try:
        sweep = verify_corners(netlist_texts)
    except Exception as exc:  # noqa: BLE001 - 계약상 어떤 실패도 결과를 크래시로 바꾸면 안 된다
        return None, f"corner sweep raised {type(exc).__name__}: {exc}"

    if not isinstance(sweep, dict) or "overall_pass" not in sweep:
        # 판정 키가 없는 것을 "통과"로 읽으면 아무것도 확인하지 않고
        # corner_confirmed=True를 내게 된다.
        return None, (
            f"corner sweep returned an unusable result "
            f"(type {type(sweep).__name__}, no 'overall_pass')"
        )

    return sweep, None


def _sweep_event(sweep: dict | None, failure: str | None, **extra) -> dict:
    """스윕 하나를 이력에 남길 모양. 실패한 스윕도 같은 모양으로 남는다.

    worst_case_corners까지 남긴다 - cli.py가 메인 루프의 최종 스윕을 통째로
    기록하는 것과 같은 이유다. "어디서 멈췄나"를 묻는 사람이 실제로 원하는
    것은 어느 코너가 그 기준을 밀어냈는가이고, 그것은 이 키에만 있다."""
    return {
        "overall_pass": bool(sweep and sweep.get("overall_pass")),
        "summary": sweep.get("summary") if sweep else None,
        "criteria": sweep.get("criteria") if sweep else None,
        "worst_case_corners": sweep.get("worst_case_corners") if sweep else None,
        "reason": failure,
        **extra,
    }


def _tightest_slack(criteria: list[Criterion], criteria_results: list[dict]) -> dict | None:
    """이 판정에서 가장 빠듯한 상대 여유와 그 기준 이름.

    코너 스윕에서 관측 가능한("코너에서 깨진다") 것을 기다리지 않고 코너
    없는 스펙에서도 한 실행만으로 읽을 수 있게 하는 것이 이 태스크의
    이유다 - 원래 트리거는 그 스펙에서 구조적으로 발화할 수 없었다.

    `criteria_results`는 `evaluate_criteria`가 내는 모양(list of
    `{"name","actual",...}`)이다 - `records[version]["criteria"]`는
    기준선 판정이든 탐색이 다시 잰 것이든 언제나 이 모양을 공유하므로
    호출부를 가리지 않는다.

    NaN은 `evaluate_criteria`가 측정 실패에 실제로 쓰는 표식이고
    (`records`를 거쳐 여기 도달하는 것이 재현 가능한 경로다), 여기서
    **최솟값 경쟁에서 빼는 것으로** 처리한다. `relative_slack`은 NaN을
    막지 않으므로(그 함수의 docstring 참고), 그대로 넘기면
    `max(|threshold|, |NaN|)`이 인자 순서에 따라 값이 갈리고 그 결과가
    `min()`에 들어가 "가장 나쁘다"도 "가장 좋다"도 아닌, 어느 쪽이 될지
    코드 순서로 결정되는 값이 된다. NaN은 여기서 이기지도 지지도
    않는다 - 그 기준은 이번 최솟값 계산에서 그냥 빠지고, 측정 실패라는
    사실 자체는 `criteria_results`의 그 항목(`actual=NaN`,
    `pass=False`)이 이미 들고 있다."""
    by_name = {c.name: c for c in criteria}
    tightest_name: str | None = None
    tightest_value: float | None = None
    for entry in criteria_results:
        criterion = by_name.get(entry.get("name"))
        if criterion is None:
            continue
        actual = entry.get("actual")
        if actual is None or actual != actual:  # actual != actual: NaN 판정, math 없이.
            continue
        slack = relative_slack(criterion, actual)
        if slack is None:
            continue
        if tightest_value is None or slack < tightest_value:
            tightest_value = slack
            tightest_name = criterion.name
    if tightest_name is None:
        return None
    return {"criterion": tightest_name, "value": tightest_value}


def _criteria_slack(
    criteria: list[Criterion], criteria_results: list[dict]
) -> list[dict]:
    """이 판정의 **모든** 기준에 대한 상대 여유. 사전 등록의 첫째 절
    ("각 수락 스텝 이후 모든 기준의 상대 여유를 기록하고")이 요구하는 것.

    `_tightest_slack`은 **착지한 덱 하나**의 최솟값이다 - 실행이 지나온
    중간 버전의 여유는 어디에도 남지 않는다. 실제로 갈라지는 구성이
    이 저장소에 이미 기록돼 있다: 밴드갭 목적 단계는 10스텝을 수락하고
    확인 스윕이 실패해 이분 탐색이 v4에 착지한다 - `tightest_slack`은
    v4를 설명하고, 더 빠듯했던 v10의 여유는 사라진다. 그래서 스텝마다
    남긴다.

    최솟값 경쟁이 아니므로 여기서는 기준을 **빼지 않는다** - 빠진
    이름과 잰 이름이 섞이면 "이 스텝에서 이 기준을 안 봤다"와 "여유를
    못 쟀다"가 같은 모양이 된다. 값을 낼 수 없는 경우만 `NaN`으로
    남긴다: `evaluate_criteria`가 측정 실패에 쓰는 표식 그대로다.
    `relative_slack`을 NaN에 부르지 않는 이유는 그 함수의 docstring에
    있다(`max()`가 인자 순서에 따라 다른 값을 돌려준다). 수락된 스텝은
    `overall_pass`를 요구하고 NaN 기준은 언제나 `pass=False`이므로 이
    분기는 오늘 도달하지 않는다 - 수락되지 않은 판정에도 이 함수를
    쓰게 되는 날을 위한 것이다."""
    by_name = {c.name: c for c in criteria}
    out: list[dict] = []
    for entry in criteria_results:
        criterion = by_name.get(entry.get("name"))
        if criterion is None:
            continue
        actual = entry.get("actual")
        if actual is None or actual != actual:  # actual != actual: NaN 판정.
            out.append({"criterion": entry.get("name"), "value": math.nan})
            continue
        out.append({"criterion": criterion.name, "value": relative_slack(criterion, actual)})
    return out


def _result(
    status: str,
    state,
    objective_before: float | None,
    objective_after: float | None,
    area_before: float,
    area_after: float,
    accepted: int = 0,
    rejected: int = 0,
    rejected_by_reason: dict[str, int] | None = None,
    pvt_sweep: dict | None = None,
    corner_failure: str | None = None,
    failure: str | None = None,
    guard_infeasible: list[str] | None = None,
    area_coverage: dict | None = None,
    final_criteria: list[dict] | None = None,
    unguarded_criteria: list[str] | None = None,
    tightest_slack: dict | None = None,
) -> dict:
    return {
        "status": status,
        "objective_before": objective_before,
        "objective_after": objective_after,
        "area_before": area_before,
        "area_after": area_after,
        "steps_accepted": accepted,
        "steps_rejected": rejected,
        # steps_rejected를 사유별로 쪼갠 것. 합은 언제나 steps_rejected와 같고,
        # 걸리지 않은 사유도 0으로 실린다 - 그러지 않으면 "이 사유로는 거절이
        # 없었다"와 "이 사유가 사라졌다"가 같은 부재가 된다. REJECTION_REASONS의
        # 주석이 어느 코드가 시뮬레이션을 쓰고 어느 코드가 안 쓰는지 적어 둔다.
        "rejected_by_reason": dict(
            rejected_by_reason
            if rejected_by_reason is not None
            else {name: 0 for name in REJECTION_REASONS}
        ),
        # 돌려주는 넷리스트에 **통과한 스윕이 붙어 있을 때만** True다.
        # 코너를 잴 수단이 없었거나(pvt_sweep is None) 스윕이 실패했으면
        # False - 확인하지 않은 것을 확인했다고 말하지 않는다. 두 필드가 한
        # 사실에서 파생되므로 서로 어긋날 수 없다.
        "corner_confirmed": bool(pvt_sweep and pvt_sweep.get("overall_pass")),
        "pvt_sweep": pvt_sweep,
        # 스윕이 **돌지 못한** 사유. pvt_sweep=None + corner_confirmed=False는
        # "코너를 잴 수단이 없었다"와 "재려다 터졌다"가 같은 모양이라 결과
        # dict만 보는 쪽(Task 7의 리포팅)이 둘을 구분하지 못한다 - 사유가
        # history.jsonl에만 있으면 조용한 무력화가 한 층 위에서 되살아난다.
        # 스윕이 정상적으로 돌고 "실패"라고 답한 경우는 여기가 None이다:
        # 그때는 pvt_sweep 자체가 무엇이 왜 실패했는지 들고 있다.
        "corner_failure": corner_failure,
        # 이 단계 자체가 **터져서** 접힌 사유. corner_failure는 스윕 하나가 못
        # 돈 것이고, 이쪽은 최적화가 통째로 중단된 것이다 - 둘은 다른 사실이라
        # 한 필드에 뭉치면 "코너를 못 쟀다"와 "LLM 호출이 실패했다"가 같은
        # 모양이 된다. 정상 경로에서는 언제나 None이다.
        "failure": failure,
        # 기준선 자체가 자기 가드밴드를 못 지키는 기준들. 비어 있으면 검사했고
        # 문제가 없었다는 뜻이다 - 키가 없는 것과 구별되어야 한다.
        "guard_infeasible": list(guard_infeasible or []),
        # 면적 예산이 실제로 걸렸는가와, 걸리지 않았다면 왜인가. area_before가
        # 0이면 area/area_before 비교가 통째로 꺼지는데, 그것이 결과에도
        # 이력에도 안 보이면 "예산이 있었지만 안 걸렸다"와 "예산이 아예 없다"가
        # 같은 모양이 된다.
        "area_coverage": area_coverage,
        # **돌려주는 넷리스트에서 잰** 기준들. 실행 결과의 final_criteria는
        # 메인 루프의 judge 결과라 최적화 전 덱을 설명하는데, cli.py는
        # final_netlist_paths만 착지 버전으로 갱신했다 - 실측 bandgap 실행에서
        # 212.25uA를 재는 넷리스트 옆에 212.99uA가 적혔다. 이분 탐색이 되돌아온
        # 실행에서는 마지막 수락 단계가 아니라 **착지한 버전**의 것이다.
        # 잴 수 없었던 경로(기준선 실패, 이 단계가 터짐)에서는 None이고,
        # 그때 cli.py는 메인 루프의 판정을 그대로 둔다.
        "final_criteria": final_criteria,
        "final_netlist_paths": state.current_netlist_paths(),
        # 여유분 없이(guard_band_violations가 이름 부재를 0.0으로 읽는 채로)
        # 판정된 기준의 이름. `{label}_baseline` 이벤트에만 있던 사실을 여기로
        # 끌어온다 - history.jsonl만 읽는 소비자는 있지만 result.json/report.md만
        # 읽는 소비자는 이 위험을 볼 방법이 없었다. 계획 문서("스펙에 없던 결정
        # 하나")가 "보고서는 무방비 기준의 개수를 적는다"고 값을 명시했는데
        # 그 값이 코드 어디에도 없었던 것이 최종 리뷰의 Critical이다.
        #
        # **`None`을 `[]`로 접지 않는다 - 여기서는 guard_infeasible과 같은
        # 관례를 쓰면 안 된다.** guard_infeasible은 report.py가 `if
        # area.get("guard_infeasible"):`로 진실성 검사만 하므로 `[]`와 `None`이
        # 렌더링에서 똑같이 "아무것도 안 그림"이 된다 - 무해한 붕괴다. 이 필드는
        # 다르다: `_unguarded_summary`는 **빈 리스트에도 긍정 문장**("모든 기준이
        # 방비됨")을 그리도록 설계됐다 - 그것이 Critical 수정의 핵심이었다.
        # `None`을 `[]`로 접으면 "allowances가 아예 없어서 잴 수 없었다"(이
        # 단계가 `_search`에 들어가기도 전에, 또는 도중에 터진 경우 -
        # `run_optimization`/`run_area_optimization`의 except 핸들러, REFUSED
        # 경로)와 "쟀고 전부 방비됐다"가 같은 긍정 문장으로 렌더된다. 그 문장은
        # 거짓이다: 어떤 기준도 어떤 allowance와도 비교되지 않았다. `0`과
        # `unknown`은 이 저장소에서 한 칸을 나눠 쓰지 않는다.
        "unguarded_criteria": list(unguarded_criteria) if unguarded_criteria is not None else None,
        # 사전 등록의 마지막 절: 각 수락 스텝 이후 모든 기준의 상대 여유를
        # 재고, 실행 종료 시 최솟값과 그 기준 이름을 남긴다. 원래 트리거
        # ("코너 스윕에서 깨지면")는 코너를 선언하지 않은 스펙에서
        # 관측 불가능했다 - 그 스펙이야말로 하한이 필요한 곳이었다. 이
        # 값은 한 실행만으로 읽을 수 있다.
        #
        # unguarded_criteria와 같은 이유로 **`None`을 `{}`나 빈 값으로
        # 접지 않는다.** `None`은 "이 판정에서 어떤 기준의 상대 여유도
        # 계산되지 않았다"는 사실이다(final_criteria가 None인 모든
        # 경로 - 기준선 시뮬레이션/목적값 확보 실패, 진입 스윕이 아예
        # 못 돌거나 실패해 탐색에 들어가지도 못한 경로, 이 단계 자체가
        # 준비 구간이나 예외 처리기에서 접힌 경로). `{}`로 접으면 "쟀고
        # 값이 없었다"로 읽혀 "재지 못했다"는 사실을 지운다.
        "tightest_slack": tightest_slack,
    }


def _version_index(state, canonical_name: str) -> int:
    """현재 버전의 인덱스. state.netlist_versions는 테스트벤치별로 lockstep이라
    canonical 하나만 봐도 된다."""
    return len(state.netlist_versions[canonical_name]) - 1


def _texts_at(state, index: int) -> dict[str, str]:
    """버전 index의 넷리스트 원문. 상태를 건드리지 않는다.

    rollback()은 pop뿐이라 앞으로 갈 수 없으므로, 이분 탐색이 중간 지점을
    확인하려면 **비파괴적** 조회가 필요하다. 새 상태 API를 만드는 대신 이미
    공개된 netlist_versions의 경로를 읽고, 착지한 뒤에야 rollback()을 필요한
    횟수만큼 부른다."""
    texts = {}
    for name, paths in state.netlist_versions.items():
        with open(paths[index]) as f:
            texts[name] = f.read()
    return texts


def _rollback_to(state, canonical_name: str, index: int) -> None:
    """rollback()을 한 단계씩 반복해 index까지 되돌린다."""
    while _version_index(state, canonical_name) > index:
        state.rollback()


def _bisect_last_passing(
    state, agents: OptimizerAgents, canonical_name: str, anchor_index: int, anchor_sweep: dict,
    label: str,
) -> tuple[int, dict]:
    """앵커와 현재 사이에서 코너를 통과하는 **마지막** 버전을 찾아 거기 착지한다.

    가드밴드를 올려 다시 탐색하는 안은 버렸다. 그것은 같은 추측을 더 크게
    다시 돌리는 것이라 비용 상한이 없고, 다시 실패하면 또 올릴지 말지를
    판단할 근거가 없다. 이분 탐색은 상한이 있고(구간 n에 대해 ceil(log2 n)
    회), 방향이 정해져 있고, 최악이어도 앵커 - 즉 시작점 - 에 착지한다.

    불변식: lo는 통과가 **확인된** 인덱스, hi는 실패가 **확인된** 인덱스.
    lo의 통과는 진입 스윕이 이미 확인해 두었다 - 진입 스윕이 추가 비용이
    아니라 앵커인 이유가 이것이다. 확인되지 않은 인덱스에 착지하는 경로는
    없으므로, 돌려주는 넷리스트에는 언제나 통과한 스윕이 붙는다."""
    lo = anchor_index
    hi = _version_index(state, canonical_name)
    passing = {lo: anchor_sweep}

    while hi - lo > 1:
        mid = (lo + hi) // 2
        sweep, failure = _run_sweep(agents.verify_corners, _texts_at(state, mid))
        state.log_event(f"{label}_bisect_probe", _sweep_event(sweep, failure, version=mid))
        if sweep is not None and sweep.get("overall_pass"):
            lo = mid
            passing[lo] = sweep
        else:
            # 실패한(또는 터진) 스윕은 통과가 아니다. 어느 쪽이든 앵커 쪽으로
            # 미는 방향이라 보수적이다.
            hi = mid

    _rollback_to(state, canonical_name, lo)
    return lo, passing[lo]


# ---------------------------------------------------------------------------
# 탐색 이음매 - 오라클 / 전략 / 수락 규칙
#
# 셋은 한 함수(_search) 안에 뭉쳐 있었다. 그 상태에서는 다른 탐색기를 꽂아
# 비교할 자리가 없다 - 로드맵 단계 3(신뢰영역 DFO)도 단계 4(제약 베이지안
# 최적화)도 "현행보다 나은가"를 물어야 하는데, 현행을 떼어낼 수 없으면 그
# 질문 자체가 성립하지 않는다. 그래서 나눈다. 나누는 선은 **비용과 권한**이다:
#
#   - **오라클**은 비싼 쪽이다. 후보를 덱에 적용하고, 시뮬레이션을 돌리고,
#     기준 판정·목적값·면적이라는 **사실**을 모아 온다. 아무것도 판정하지 않는다.
#   - **전략**은 싼 쪽이다. 어떤 후보를 어떤 순서로 시도할지만 정한다. 오늘의
#     전략은 좌표 하강(coordinate_descent)이다: 순위대로 한 번에 한 노브,
#     기하는 ×STEP_RATIO, 개수는 ±1.
#   - **수락 규칙**(accept_step)은 결정론적이고 전략 **밖**에 있다. 전략은
#     제안할 뿐 수락을 정하지 못한다.
#
# 마지막 줄이 이 분리의 전부다. 이 저장소의 전제는 "판정하는 층은 교체 가능하지
# 않다"이고(오케스트레이터가 자유 텍스트를 파싱하지 않는 것도, 튜너에게 구조
# 저작을 금하는 것도 같은 규칙이다), 전략은 언젠가 LLM이 될 수도 있는 자리다.
# 전략에게 "이 후보는 좋다"고 말할 권한을 주면 - 예컨대 전략이 스스로 목적값을
# 비교해 push/rollback을 부르게 하면 - 그 경계가 사라지고, 마진을 다 태운 덱이나
# 면적 예산을 넘긴 덱이 아무 게이트도 거치지 않고 실행의 결과가 될 수 있다.
# **다시 합치지 말 것.** 합칠 때 사라지는 것은 코드 몇 줄이 아니라 그 경계다.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Knob:
    """전략이 움직일 수 있는 노브 하나. **값이 없다** - OPTIMIZER_SCHEMA가
    후보에 값을 싣는 것을 구조적으로 금하고, 얼마나 움직일지는 전략이 덱의
    현재 값에서 정한다."""

    refdes: str
    param: str
    direction: str


@dataclass(frozen=True)
class KnobState:
    """덱에서 읽은 그 노브의 현재 상태. 오라클만 만든다 - 전략이 넷리스트를
    직접 파싱하기 시작하면 주소 지정 게이트를 우회하는 경로가 생긴다."""

    # 덱에 **실제로 적힌 철자**다(_deck_token 참고). 제안의 철자를 그대로 쓰면
    # 대소문자가 섞인 덱에서 apply_changes가 토큰을 하나 더 붙인다.
    token: str
    value: float
    # 개수 파라미터인가(m/nf에 도달하는가). 이름이 아니라 도달하는 토큰에서
    # 나온다 - area_limits와 같은 함수를 쓰므로 갈라질 수 없다.
    integer: bool


@dataclass(frozen=True)
class ProposedStep:
    """전략이 오라클에게 재 달라고 내미는 한 조각. 여러 개를 한 번에 낼 수
    있다 - 오늘의 전략은 언제나 하나지만, 노브 간 결합을 보는 탐색기(단계 3·4)는
    한 후보가 여러 노브를 동시에 옮긴다. check_area_growth와 apply_changes가
    이미 목록을 받으므로 이쪽만 목록이면 된다."""

    knob: Knob
    state: KnobState
    value: float


@dataclass
class Evaluation:
    """후보 하나를 재고 나온 **사실들**. 판정은 하나도 들어 있지 않다.

    blocked만 예외처럼 보이지만 그것도 사실이다 - "여기까지밖에 못 쟀고 이유는
    이것"이다. 수락 규칙이 그 사유를 거절 사유로 읽는다."""

    # 버전을 밀었는가. 거절되면 되돌려야 하므로 호출자가 알아야 한다.
    pushed: bool = False
    changes: list[dict] = field(default_factory=list)
    area: float | None = None
    objective: float | None = None
    measurements: dict | None = None
    verdict: dict | None = None
    violations: list[str] = field(default_factory=list)
    gate: str | None = None
    # 끝까지 재지 못한 사유: 에어리어 게이트, 면적 예산, 시뮬레이션 실패.
    blocked: str | None = None
    # 위 blocked를 **집계 가능한 코드**로 적은 것. 자유 문장은 사람이 읽는
    # 것이고, 이것은 결과가 세는 것이다 - 사유를 사후에 문자열에서 되파싱하면
    # 새 차단 지점 하나가 조용히 남의 칸에 들어간다(이 저장소가 area_check /
    # refdes_check가 같은 `feedback` 키를 쓴 것으로 이미 겪은 모양이다).
    blocked_by: str | None = None


@dataclass(frozen=True)
class StepOutcome:
    """전략이 한 단계를 시도하고 돌려받는 것. 수락 **여부**만 알려 준다 -
    전략이 그것을 정하지 않았다는 사실이 반환값의 방향에 드러난다."""

    accepted: bool
    reason: str | None
    objective: float | None


def area_within_budget(area: float, area_before: float, budget: float) -> tuple[bool, str | None]:
    """면적 예산 검사. 수락 규칙의 일부이지만 **오라클이 앞당겨** 부른다.

    앞당기는 이유는 비용이다: 면적은 파생이라 공짜고 목적값은 재야 아는데,
    예산을 넘긴 후보에 시뮬레이션을 쓸 이유가 없다. 그래도 규칙 자체는 이
    함수 하나에만 있으므로 오라클이 판정을 **하는** 것은 아니다 - 규칙을
    부르고 그 답을 사실로 들고 올 뿐이다.

    area_before가 0이면 비율이 정의되지 않아 예산이 통째로 꺼진다. 실제로
    도달하는 경우이고(래퍼 셀 덱), _optimize가 area_coverage로 그 사실을
    이력과 결과에 남긴다."""
    if area_before > 0 and area / area_before > budget:
        return False, (
            f"area {area:g} is {area / area_before:.3f}x the starting area, "
            f"over the {budget:g}x budget"
        )
    return True, None


def accept_step(
    evaluation: Evaluation, best_objective: float, objective_name: str
) -> tuple[bool, str | None]:
    """수락 규칙. (수락할까, 아니면 왜 안 되는가).

    **결정론적이고 전략 밖에 있다.** 전략을 바꿔도 이 함수는 바뀌지 않는다.

    기존 루프의 verify_post를 쓰지 않는 이유도 그대로다: 그쪽 계약은
    "나빠졌으면 롤백"인데 좋은 최적화 단계는 **의도적으로** 마진을 소비하므로,
    재사용하면 성공한 축소마다 롤백이 난다.

    순서가 규칙의 일부다. 오라클이 끝까지 재지 못한 사유가 가장 먼저다 -
    그때는 verdict도 objective도 없어서 뒤의 검사가 성립하지 않는다."""
    if evaluation.blocked is not None:
        return False, evaluation.blocked
    if not evaluation.verdict["overall_pass"]:
        return False, f"criteria no longer pass: {evaluation.verdict['summary']}"
    if evaluation.violations:
        # 통과했더라도 여유분을 다 태웠으면 수락하지 않는다. 임계값에 바짝
        # 붙은 채로 멈추면 코너와 모델 변동에서 무너진다.
        return False, "; ".join(evaluation.violations)
    if evaluation.objective is None:
        # "측정값에 없다"는 이름 있는 목적(전류 단계)에만 참이다 - 면적 단계의
        # 목적은 파생값이라 애초에 measurements를 보지 않는다
        # (`_objective_value`). 이 함수는 문자열이 된 objective_name만 받으므로
        # 어느 쪽인지 안에서 가를 수 없다 - 두 경우 모두에서 참인 문장으로
        # 남긴다.
        return False, (
            f"objective {objective_name!r} could not be evaluated for this "
            f"candidate (no value among the measurements, and no derivable value)"
        )
    if evaluation.objective >= best_objective:
        return False, (
            f"objective {evaluation.objective:g} is not below the current best {best_objective:g}"
        )
    return True, None


class SearchOracle:
    """후보를 **재는** 쪽. 비싼 부분이고, 아무것도 판정하지 않는다.

    전략이 넷리스트에 닿는 유일한 통로다. 네 개의 주소 지정 게이트가 전부
    여기서 도는 이유가 그것이다 - 전략이 직접 apply_changes를 부를 수 있으면
    게이트를 우회하는 경로가 생기고, 그 우회는 탐색기를 하나 새로 쓸 때마다
    다시 열린다."""

    def __init__(
        self,
        spec,
        state,
        agents: OptimizerAgents,
        canonical_name: str,
        baseline_components: dict,
        area_before: float,
        allowances: dict[str, float],
        phase: PhaseConfig,
    ) -> None:
        self._spec = spec
        self._state = state
        self._agents = agents
        self._canonical_name = canonical_name
        self._baseline_components = baseline_components
        self._area_before = area_before
        self._allowances = allowances
        self._phase = phase
        self._simulations = 0

    @property
    def simulations(self) -> int:
        """이 오라클이 실제로 쓴 시뮬레이션 수. 두 전략을 같은 예산에서
        비교하려면 "몇 번 쟀는가"가 결과에 들어와야 한다."""
        return self._simulations

    def knob_state(self, refdes: str, param: str) -> tuple[KnobState | None, str | None, str | None]:
        """(상태, 막은 게이트, 사유). 시뮬레이션을 쓰지 않는다.

        게이트가 값 읽기보다 **앞**에 온다. 뒤에 두면 해석 불가능한 refdes가
        "현재 값을 못 읽었다"로 보고되어, 실제 원인(그런 소자가 없다/모호하다)을
        자기 게이트가 말하지 못한다."""
        current_text = self._state.current_netlist_texts()[self._canonical_name]
        gate, feedback = _gate_addressing(current_text, {"refdes": refdes, "param": param})
        if gate is not None:
            return None, gate, feedback

        # 값 읽기와 정수 판정 둘 다 index_baseline_components의 색인을 쓴다.
        # 같은 표를 쓰지 않으면 "어느 소자의 값을 읽어 한 단계 옮기는가"와
        # "어느 소자가 편집되는가"가 갈라질 수 있다 - 이 저장소가 이미 두 번
        # 닫은 넷리스트 해소 이중화 결함의 더 나쁜 판본이다.
        component = index_baseline_components(current_text).get(refdes)
        token = _deck_token(component, param) if component is not None else None
        if token is None:
            # 게이트는 통과했는데 그 소자 줄에는 이 이름이 없다. 실재하는
            # 경로다: check_param_applicability의 **동료 규칙**이 bandgap의
            # `Xq1.m`을 admit 한다(Xq1은 m=을 안 쓰지만 같은 모델의 Xq8이
            # m=8을 쓰고, m이 이미터 면적비를 정하는 유일한 노브다). 적용
            # 가능한 것은 맞지만 **출발 값이 없다** - 여기서 기본값을 지어내는
            # 것이 이 프로젝트가 금하는 추측이다. "값을 못 읽었다"와 한 문장으로
            # 뭉치면, 해소 불가능한 표현식(`W='wn*2'`)과 같은 사유로 보여
            # 진단이 갈라진다.
            return None, None, (
                f"{refdes} does not write {param!r} on its own line, so there is no "
                f"current value to step from (a same-model peer writes it, which is why "
                f"the applicability gate admits it - but the starting value is not "
                f"something to invent)"
            )
        value = _current_value(component, token)
        if value is None:
            return None, None, (
                f"cannot read a numeric current value for {refdes}.{param} in the netlist"
            )
        return KnobState(token=token, value=value, integer=is_count_param(component, token)), None, None

    async def evaluate(self, steps: list[ProposedStep]) -> Evaluation:
        """후보 하나를 끝까지 재고 사실만 돌려준다. 수락도 롤백도 하지 않는다.

        롤백을 여기서 하지 않는 것은 의도다 - 수락 규칙이 아직 안 돌았으므로
        되돌릴지 말지를 이 자리에서는 알 수 없다. Evaluation.pushed가 호출자에게
        그 책임을 넘긴다."""
        evaluation = Evaluation()
        current_texts = self._state.current_netlist_texts()
        # param이 아니라 **덱에 적힌 철자**(token)로 쓴다 - _deck_token 참고.
        evaluation.changes = [
            {
                "refdes": step.knob.refdes,
                "param": step.state.token,
                "new_value": _format_value(step.value, step.state.integer),
            }
            for step in steps
        ]

        # 에어리어 게이트만 new_value를 읽으므로 값이 정해진 뒤에 온다.
        area_ok, area_feedback = check_area_growth(self._baseline_components, evaluation.changes)
        if not area_ok:
            evaluation.gate = "area"
            evaluation.blocked = area_feedback
            evaluation.blocked_by = "area_growth"
            return evaluation

        new_texts = {
            name: apply_changes(text, evaluation.changes) for name, text in current_texts.items()
        }
        self._state.push_netlist_version(new_texts)
        evaluation.pushed = True

        # 에어리어는 파생이라 공짜지만 목적값은 재야 안다. 그 비대칭이 이
        # 루프를 감당 가능하게 만드는 전부다 - 예산 초과는 시뮬레이션 앞에서
        # 걸러진다.
        evaluation.area = total_area(new_texts[self._canonical_name]).area
        if self._phase.area_budget is not None:
            within, budget_reason = area_within_budget(
                evaluation.area, self._area_before, self._phase.area_budget
            )
            if not within:
                evaluation.blocked = budget_reason
                evaluation.blocked_by = "area_budget"
                return evaluation

        self._simulations += 1
        step_sim, sim_failure = await _run_simulation(self._agents.simulate, new_texts, self._spec)
        if step_sim is None:
            # 회로가 시뮬레이터를 통과하지 못하는 지점까지 갔다는 뜻이다
            # (예: sky130 소자 bin을 벗어난 폭). 여기서 예외가 새어 나가면
            # 통과한 실행이 크래시가 된다.
            evaluation.blocked = sim_failure
            # 위의 둘과 달리 이 갈래는 시뮬레이션을 **한 번 썼다**. 같은 칸에
            # 세면 "몇 번 쟀는가"를 결과에서 읽을 수 없다.
            evaluation.blocked_by = "simulation_failed"
            return evaluation

        evaluation.measurements = step_sim["measurements"]
        evaluation.verdict = evaluate_criteria(evaluation.measurements, self._spec.all_criteria)
        evaluation.violations = guard_band_violations(
            evaluation.measurements, self._spec.all_criteria, self._allowances
        )
        evaluation.objective = _objective_value(
            self._phase.objective, evaluation.measurements, evaluation.area
        )
        return evaluation


REJECTION_REASONS = (
    # 시뮬레이션을 **한 번도 쓰지 않는** 셋.
    "knob_gate",  # refdes/param/stimulus 게이트가 그 주소를 막았다
    "knob_no_value",  # 주소는 합법인데 덱의 그 줄에 출발 값이 없다
    "exhausted",  # 전략이 이 노브에 더 갈 곳이 없다고 판정했다
    "area_growth",  # 소자별 성장 티어가 후보를 버렸다(적용 전)
    "area_budget",  # 총 면적 예산이 후보를 버렸다(적용 후, 측정 전)
    # 시뮬레이션을 쓴 둘.
    "simulation_failed",  # 회로가 시뮬레이터를 통과하지 못했다
    "not_accepted",  # 끝까지 재고 수락 규칙이 떨어뜨렸다
    # 탐색 밖.
    "corner_walked_back",  # nominal에서 수락됐다가 코너 확인에서 되돌려졌다
)


class SearchRun:
    """전략이 보는 세계 전부. 노브 목록, 남은 예산, 그리고 후보를 시도하는 문.

    전략은 여기서 수락 **결과**를 읽을 수 있지만 수락을 **정할** 수는 없다 -
    attempt가 accept_step을 부르고, 롤백과 버전 기록과 이력 이벤트까지 전부
    이 클래스가 한다. 전략이 할 수 있는 일은 "다음에 무엇을 시도할까"뿐이다."""

    def __init__(
        self,
        spec,
        state,
        oracle: SearchOracle,
        knobs: list[Knob],
        canonical_name: str,
        objective_before: float,
        records: dict,
        max_steps: int,
        phase: PhaseConfig,
    ) -> None:
        self.knobs = list(knobs)
        self._records = records
        self._accepted = 0
        self._rejected = 0
        # 사유별 집계. **모든** 코드를 0으로 미리 깔아 둔다 - 걸리지 않은 사유가
        # 키째 없으면 "이 사유로는 거절이 없었다"와 "이 사유가 코드에서
        # 사라졌다"가 읽는 쪽에서 같은 모양이 된다.
        self._rejected_by = {name: 0 for name in REJECTION_REASONS}
        self._best_objective = objective_before
        self._spec = spec
        self._state = state
        self._oracle = oracle
        self._canonical_name = canonical_name
        self._max_steps = max_steps
        self._phase = phase
        self._steps = 0

    # 아래 넷은 **읽기 전용**이다. 전략이 대입할 수 있으면 "제안만 한다"는
    # 계약이 문서상의 약속으로 내려앉는다 - best_objective를 올려 두면 더
    # 나쁜 후보가 수락되고, accepted를 조작하면 실행이 보고하는 수락 수가
    # 돌려주는 넷리스트를 설명하지 못한다(이 저장소가 final_criteria에서
    # 이미 한 번 겪은 모양이다). 파이썬에서 완전한 봉인은 불가능하지만,
    # 대입이 AttributeError로 즉시 터지는 것과 조용히 먹히는 것은 다르다.
    @property
    def accepted(self) -> int:
        return self._accepted

    @property
    def rejected(self) -> int:
        return self._rejected

    @property
    def rejected_by_reason(self) -> dict[str, int]:
        """사유별 거절 수. 합은 언제나 `rejected`와 같다.

        하나의 숫자로 접으면 "탐색이 열심히 했지만 여지가 없었다"와 "탐색이
        노브의 절반에 주소 단계에서 접근조차 못 했다"가 같은 값이 된다 - 그리고
        앞 다섯 코드는 시뮬레이션을 한 번도 쓰지 않으므로, 접힌 숫자만 보면
        이 단계가 실제로 몇 번 쟀는지도 알 수 없다."""
        return dict(self._rejected_by)

    @property
    def best_objective(self) -> float:
        """지금까지 수락된 가장 낮은 목적값. 수락 규칙의 기준점이다."""
        return self._best_objective

    @property
    def records(self) -> dict:
        """버전 인덱스 → 그 버전에서 잰 값. 이분 탐색이 착지한 버전을 보고할
        때 읽는다."""
        return self._records

    @property
    def steps_taken(self) -> int:
        return self._steps

    @property
    def remaining_steps(self) -> int:
        """남은 시도 횟수. 예산은 **전역**이다 - 노브마다가 아니다."""
        return self._max_steps - self._steps

    @property
    def simulations(self) -> int:
        return self._oracle.simulations

    def spend_step(self, knob: Knob) -> bool:
        """한 단계를 쓸 수 있으면 True. 없으면 사유를 남기고 False.

        전략은 False를 받으면 **통째로** 멈춰야 한다. 예산이 전역이므로 다음
        노브로 넘어가도 달라지는 것이 없다.

        "예산이 떨어졌다"와 "후보를 전부 소진했다"는 다른 사실인데 이력에서는
        둘 다 그냥 optimize_step이 멈추는 모양이라 구별되지 않았다 - 그래서
        자기 이벤트를 가진다."""
        if self._steps >= self._max_steps:
            self._state.log_event(f"{self._phase.label}_budget_exhausted", {
                "steps": self._steps,
                "limit": self._max_steps,
                "refdes": knob.refdes,
                "param": knob.param,
            })
            return False
        self._steps += 1
        return True

    def knob_state(self, knob: Knob) -> KnobState | None:
        """게이트를 통과한 현재 상태. 막히면 사유를 남기고 None.

        전략은 None을 받으면 그 노브를 버려야 한다 - 게이트가 막은 것은 값이
        아니라 주소이므로, 다른 값으로 다시 시도해도 같은 자리에서 막힌다."""
        state, gate, reason = self._oracle.knob_state(knob.refdes, knob.param)
        if state is None:
            # 게이트가 막은 것과 "주소는 합법인데 출발 값이 없다"는 다른
            # 사실이다 - 오라클이 이미 둘을 갈라 놓았으므로(gate가 None인지),
            # 집계도 갈라야 한다.
            code = "knob_gate" if gate is not None else "knob_no_value"
            self._reject(self._event(knob, gate=gate, reason=reason, reason_code=code), code)
        return state

    def log_event(self, suffix: str, payload: dict) -> None:
        """전략이 **자기 행동에 대해** 남기는 기록. 이력에 닿는 유일한 문이다.

        필요한 이유는 이 저장소가 게이트에 열 번 물었던 질문과 같다: "이것이
        아무것도 안 할 때 로그가 어떻게 보이는가." 탐색기도 조용히 무력해질 수
        있다 - 폴이 한 번도 완결되지 않은 실행과 매번 첫 방향에서 성공한 실행은
        `optimize_step`만 보면 구별되지 않는데, 둘 다 "적응 스텝이 발화하지
        않았다"이고 그 실행으로는 아무것도 판정할 수 없다.

        **집계는 여전히 전략이 쓸 수 없다.** accepted/rejected/best_objective는
        읽기 전용이고, 이 문으로 나가는 것은 이력의 이벤트뿐이다 - 실행이
        돌려주는 넷리스트를 설명하는 숫자는 하나도 전략을 거치지 않는다.

        **이 문이 스스로 단계 라벨을 앞에 붙인다** (`f"{self._phase.label}_{suffix}"`).
        `spend_step`/`attempt`가 이미 그렇게 하는 것과 같은 이유다: `SearchRun`은
        `_phase.label`을 내주는 공개 프로퍼티가 없고, 파라미터 이름을 `suffix`로
        둔 것은 호출부가 "이건 접미사일 뿐, 완전한 이벤트 이름이 아니다"를
        보게 하려는 것이다. `run_area_optimization`이 `agents.search_strategy`를
        그대로 area 단계에도 넘기므로(`optimizer.py:2102` 부근), 한 실행에서
        면적 단계와 목적 단계가 **같은 전략**을 돌릴 수 있다 - 전략이 라벨을
        스스로 붙이게 두면 그 실행이 낸 사건이 `history.jsonl`에서 두 단계
        사이에 섞여 버린다. 라벨을 이 문이 강제로 붙이면 전략은 그것을 잊을
        방법이 없다."""
        self._state.log_event(f"{self._phase.label}_{suffix}", payload)

    def exhausted(self, knob: Knob, state: KnobState, reason: str) -> None:
        """전략이 "이 노브는 더 갈 곳이 없다"고 판단한 경우. 사실이 사유와
        함께 이력에 남아야 하므로 전략이 조용히 넘어가지 못하게 문을 준다."""
        self._reject(
            self._event(knob, state=state, reason=reason, reason_code="exhausted"), "exhausted"
        )

    async def attempt(self, steps: list[ProposedStep]) -> StepOutcome:
        """후보 하나를 재고, **수락 규칙에 물어보고**, 그 결과를 실행에 반영한다.

        전략이 부를 수 있는 유일한 비싼 문이다. 수락되면 버전이 남고 목적값
        기준선이 내려가며, 거절되면 되돌린다.

        **거절된 후보의 목적값도 돌려준다.** 응답면을 모델링하는 탐색기(단계
        3의 신뢰영역, 단계 4의 BO)는 실패한 점에서도 배워야 하는데, 거절
        사유만 주면 그 점의 좌표와 값이 사라진다. 덱은 되돌아가 있으므로 알려
        준다고 해서 전략이 무언가를 유지하게 되는 것은 아니다.

        반대로 "수락 규칙이 받아들였는데 전략이 사양하는" 문은 **일부러 없다.**
        모든 게이트와 가드밴드를 통과하고 목적값까지 내린 후보를 버릴 권한은
        수락을 뒤집는 권한과 같고, 그것을 주면 이 분리가 무의미해진다."""
        evaluation = await self._oracle.evaluate(steps)
        accepted, reason = accept_step(
            evaluation, self._best_objective, str(self._phase.objective)
        )

        head = steps[0]
        # 재기 전에 막혔으면 그 차단 지점이 사유고, 끝까지 쟀으면 수락 규칙이다.
        code = None if accepted else (evaluation.blocked_by or "not_accepted")
        event = self._event(
            head.knob,
            state=head.state,
            gate=evaluation.gate,
            reason=reason,
            reason_code=code,
            accepted=accepted,
            after=parse_spice_value(evaluation.changes[0]["new_value"]),
            objective=evaluation.objective,
            area=evaluation.area,
            # 사전 등록의 첫째 절: **각 수락 스텝 이후** 모든 기준의 상대
            # 여유를 기록한다. 거절된 스텝은 덱이 되돌아가므로 그 판정이
            # 설명하는 덱이 남지 않는다 - 그래서 값이 아니라 `None`이고,
            # 키 자체는 **`_event`가 만드는 모든 스텝 이벤트**가 든다(그
            # 기본값). 없는 키와 비어 있는 값이 같아 보이면 계측이 사라진
            # 것을 못 본다.
            #
            # **"모든 스텝 이벤트"가 아니다.** `_optimize`의 목적값 미확보
            # 경로가 `{label}_step` 하나를 손으로 지어 남기는데
            # (`objective_before is None` 분기), 그 딕셔너리는 `_event`를
            # 거치지 않으므로 `criteria_slack`도 `reason_code`도 `direction`도
            # 없다. 목적 단계에서 목적 측정값이 안 나오면 실제로 도달한다.
            # 그 이벤트는 탐색이 한 스텝도 밟지 않았다는 사실만 나르므로
            # 채우지 않았다 - 그러나 `history.jsonl`을 스텝 이벤트 단위로
            # 훑는 소비자는 키가 없는 한 건을 만날 수 있다.
            criteria_slack=(
                _criteria_slack(self._spec.all_criteria, evaluation.verdict["criteria"])
                if accepted
                else None
            ),
        )
        if len(steps) > 1:
            # `coordinate_descent`는 언제나 steps 길이 1이라 여기 오지 않지만,
            # `compound_fallback_*`는 조합 스텝을 시도할 때마다(거절 여부와
            # 무관하게) 이 분기를 지난다 - 여러 노브를 한 후보로 미는 전략이
            # 붙었을 때, 이벤트의 스칼라 필드(head 하나분)만 보면 나머지 변경이
            # 통째로 안 보이므로 목록을 함께 남긴다.
            event["changes"] = evaluation.changes
        self._state.log_event(f"{self._phase.label}_step", event)

        if accepted:
            self._accepted += 1
            self._best_objective = evaluation.objective
            self._records[_version_index(self._state, self._canonical_name)] = {
                "objective": evaluation.objective,
                "area": evaluation.area,
                # 이 버전에서 실제로 잰 기준 판정. 이분 탐색이 여기 착지하면
                # 리포트가 쓰는 것이 이것이다.
                "criteria": evaluation.verdict["criteria"],
            }
        else:
            if evaluation.pushed:
                self._state.rollback()
            self._count_rejection(code)

        return StepOutcome(accepted=accepted, reason=reason, objective=evaluation.objective)

    def _event(
        self,
        knob: Knob,
        state: KnobState | None = None,
        gate: str | None = None,
        reason: str | None = None,
        reason_code: str | None = None,
        accepted: bool = False,
        after: float | None = None,
        objective: float | None = None,
        area: float | None = None,
        criteria_slack: list[dict] | None = None,
    ) -> dict:
        return {
            "refdes": knob.refdes,
            # 실제로 편집한 철자를 기록한다. 제안의 철자를 남기면 대소문자가
            # 섞인 덱에서 이력은 `w`라고 하는데 넷리스트는 `W`를 든다.
            "param": state.token if state is not None else knob.param,
            "direction": knob.direction,
            "before": state.value if state is not None else None,
            # 넷리스트에 **실제로 적힌** 값이어야 한다. 원시 float를 남기면
            # 다음 단계의 before(덱에서 다시 읽은 값)와 미세하게 어긋나 이력이
            # 연결되지 않는다.
            "after": after,
            "objective": objective,
            "area": area,
            "accepted": accepted,
            "gate": gate,
            "reason": reason,
            # 사유 **코드**. reason은 사람이 읽는 문장이고 이것은 세는 것이라,
            # 결과의 rejected_by_reason을 이력에서 그대로 되셀 수 있다.
            # 수락된 단계에서는 None이다.
            "reason_code": reason_code,
            # 수락된 단계에서만 값이 있다 - `_criteria_slack` 참고. 여기서
            # 재는 것은 이 스텝이 **남긴 덱**의 여유이므로, 실행이 지나온
            # 모든 중간 버전의 여유가 이력에 남는다. `result`의
            # `tightest_slack`은 **착지한 덱 하나**의 최솟값이라 그 둘은
            # 다른 사실이다.
            "criteria_slack": criteria_slack,
        }

    def _count_rejection(self, code: str) -> None:
        if code not in self._rejected_by:
            raise ValueError(f"unknown rejection reason {code!r}")
        self._rejected_by[code] += 1
        self._rejected += 1

    def _reject(self, event: dict, code: str) -> None:
        self._state.log_event(f"{self._phase.label}_step", event)
        self._count_rejection(code)


# 전략의 계약: SearchRun 하나를 받아 돌고, 아무것도 돌려주지 않는다. 결과는
# run에 쌓인다(accepted/rejected/records) - 전략이 집계를 직접 만들면 그
# 숫자가 실행이 돌려주는 넷리스트와 갈라질 수 있다.
SearchStrategy = Callable[["SearchRun"], Awaitable[None]]


async def coordinate_descent(run: SearchRun) -> None:
    """오늘까지의 탐색 그대로. 이름이 곧 정체다 - 좌표 하강이다.

    순위대로 한 번에 한 노브, 기하는 ×STEP_RATIO, 개수는 ±1. 노브 간 결합을
    보지 못하고, 스텝이 고정 비율이며, 코너에 눈이 멀어 있다 - 실측으로
    nominal에서 10단계를 수락하고 코너 확인에서 6개 기준이 깨져 4단계만
    살아남았다. 그것이 로드맵 단계 3이 겨냥하는 약점이고, **여기서 고치지
    않는다**: 지금 필요한 것은 비교 대상이 될 기준선이다."""
    for knob in run.knobs:
        while True:
            if not run.spend_step(knob):
                # 예산은 전역이다 - 다음 노브로 넘어가도 달라지지 않는다.
                return
            state = run.knob_state(knob)
            if state is None:
                break
            value = _next_value(state.value, state.integer, knob.direction)
            if value is None:
                run.exhausted(
                    knob,
                    state,
                    f"{knob.refdes}.{knob.param} cannot move further in "
                    f"direction {knob.direction!r}",
                )
                break
            outcome = await run.attempt([ProposedStep(knob, state, value)])
            if not outcome.accepted:
                # 한 번 거절된 방향은 그 후보에서 더 밀지 않는다. 같은 노브를
                # 같은 방향으로 계속 미는 것은 방금 얻은 증거를 무시하는
                # 것이고, 보수적인 쪽(후보 소진)이 예산도 아낀다.
                break


def _compound_fallback(partners: int) -> SearchStrategy:
    """좌표별 하강 + 거절 시 **부호가 섞인** 2노브 스텝 되시도.

    왜 반대 방향인가: 면적 단계는 목적이 면적이므로 순위의 모든 방향이
    "decrease"다. 어떤 노브를 축소해서 기준이 깨졌다면 둘을 같이 축소하면
    **더** 깨진다 - 같은 방향 조합은 실행되고 로그도 남지만 거절을 구제할 수
    없다. 좌표별 하강이 결합 문제에서 막히는 이유는 개선 방향이 부호가 섞인
    대각선이기 때문이고(밀러 캡을 줄이되 출력단을 키운다), 축만 따라가는
    탐색은 그 방향을 원리적으로 보지 못한다.

    확대 폭은 새 상수가 아니다 - `_next_value`가 이미 `direction="increase"`를
    `current / STEP_RATIO`로 처리한다. 순 면적이 떨어져야 한다는 것은
    `accept_step`이 이미 요구하므로 여기서 검사하지 않는다.

    `partners=0`은 `coordinate_descent`와 같아야 하며, 그것은 주장이 아니라
    `test_partners_zero_is_byte_for_byte_coordinate_descent`가 못박는다."""

    async def strategy(run: SearchRun) -> None:
        for index, knob in enumerate(run.knobs):
            while True:
                if not run.spend_step(knob):
                    return
                state = run.knob_state(knob)
                if state is None:
                    break
                value = _next_value(state.value, state.integer, knob.direction)
                if value is None:
                    run.exhausted(
                        knob,
                        state,
                        f"{knob.refdes}.{knob.param} cannot move further in "
                        f"direction {knob.direction!r}",
                    )
                    break
                outcome = await run.attempt([ProposedStep(knob, state, value)])
                if outcome.accepted:
                    continue
                if not await _try_partners(run, index, knob, state, value, partners):
                    break

    return strategy


async def _try_partners(
    run: SearchRun,
    index: int,
    knob: Knob,
    state: KnobState,
    value: float,
    partners: int,
) -> bool:
    """순위상 다음 `partners` 개를 **반대 방향**으로 짝지어 시도한다.

    상대를 결합 스캔에서 고르지 않는 이유: 스캔은 덱 하나·테스트벤치 하나에만
    있고, 스캔을 전제하는 전략은 스캔이 없는 덱에서 돌 수 없다. 순위는 모든
    실행이 이미 만든다."""
    for partner in run.knobs[index + 1 : index + 1 + partners]:
        if not run.spend_step(partner):
            return False
        partner_state = run.knob_state(partner)
        if partner_state is None:
            continue
        opposite = "increase" if knob.direction == "decrease" else "decrease"
        partner_value = _next_value(partner_state.value, partner_state.integer, opposite)
        if partner_value is None:
            # 이 파트너는 자기 방향(원래 순위 방향)으로는 멀쩡하다 - 반대
            # 방향으로만 갈 곳이 없다. `run.exhausted`는 쓰지 않는다: 그건
            # `_reject`를 타 거절 카운터를 올리는데, 이 파트너는 평가된 적조차
            # 없다. 그래도 예산 1스텝은 `spend_step`이 이미 써 버렸으므로,
            # 아무 기록도 안 남기면 "이 파트너를 아예 고려조차 안 했다"와
            # 구별되지 않는다.
            run.log_event(
                suffix="compound_partner_direction_unavailable",
                payload={
                    "lead_refdes": knob.refdes,
                    "lead_param": knob.param,
                    "partner_refdes": partner.refdes,
                    "partner_param": partner.param,
                    "attempted_direction": opposite,
                    "partner_value": partner_state.value,
                    "budget_spent": True,
                },
            )
            continue
        outcome = await run.attempt(
            [
                ProposedStep(knob, state, value),
                ProposedStep(replace(partner, direction=opposite), partner_state, partner_value),
            ]
        )
        if outcome.accepted:
            return True
    return False


# 이름으로 전략을 고르는 표. scripts/search_ab.py가 이것을 읽는다 - 하니스가
# 모듈 내부를 뒤지게 하면 전략을 하나 더 붙일 때마다 하니스도 고쳐야 한다.
SEARCH_STRATEGIES: dict[str, SearchStrategy] = {
    "coordinate_descent": coordinate_descent,
    # 사전 등록 격자 partners ∈ {0,1,3}. 0은 coordinate_descent와 같으므로
    # 표에 넣지 않는다 - 같은 것을 두 이름으로 넣으면 A/B 표에 대조군이 둘로
    # 보인다. 동일성은 단위 테스트가 못박는다.
    "compound_fallback_1": _compound_fallback(1),
    "compound_fallback_3": _compound_fallback(3),
}
DEFAULT_STRATEGY = "coordinate_descent"


async def _knob_ranking(
    spec,
    state,
    agents: OptimizerAgents,
    start_text: str,
    baseline_verdict: dict,
    allowances: dict[str, float],
    objective_name: str,
    label: str,
) -> list[dict]:
    """노브 순위. 주입된 것이 있으면 그것, 없으면 에이전트를 부른다.

    **주입된 경우 에이전트는 부르지 않는다** - 프롬프트에 쓰이는 구조 뷰와
    넷리스트 뷰도 만들지 않는다. 그것이 이 갈래의 목적이다: 탐색기 비교
    실행에는 LLM이 하나도 없어야 한다(OptimizerAgents.knob_ranking 참고).
    이 함수가 반환한 뒤에는 두 갈래가 구별되지 않으므로, 출처는 이력에
    남긴다 - 남기지 않으면 "고정 순위로 돈 실행"과 "에이전트가 마침 같은
    순위를 낸 실행"이 history.jsonl에서 같은 모양이 된다."""
    if agents.knob_ranking is not None:
        state.log_event(f"{label}_proposal", {
            "objective": objective_name,
            "source": "fixed",
            "candidates": list(agents.knob_ranking),
            "overall_reasoning": "fixed knob ranking supplied by the caller (no agent call)",
        })
        return list(agents.knob_ranking)

    structure = derive_structure(start_text, spec.circuit_name)
    paths = build_signal_paths(structure)
    # 실패한 기준이 없으므로 초점 씨앗도 없다. select_focus의 전 블록 폴백이
    # 여기서는 정상 동작이다 - 최적화는 특정 실패를 쫓는 것이 아니다.
    focus = select_focus(structure, paths, set(), set(), start_text)
    structure_view = render_structure(structure, paths, find_patterns(structure), focus)
    netlist_view = render_netlist(start_text, focus)
    margins = [
        {**entry, "allowance": allowances.get(entry["name"], 0.0)}
        for entry in baseline_verdict["criteria"]
    ]
    proposal = await agents.propose(structure_view, margins, objective_name, netlist_view)
    state.log_event(f"{label}_proposal", {"objective": objective_name, "source": "agent", **proposal})
    return list(proposal.get("candidates", []))


async def _search(
    spec,
    state,
    agents: OptimizerAgents,
    canonical_name: str,
    start_text: str,
    baseline_measurements: dict,
    objective_before: float,
    area_before: float,
    allowances: dict[str, float],
    phase: PhaseConfig,
) -> dict:
    """탐색을 **조립**한다. 어떤 후보를 시도할지는 여기서 정하지 않는다.

    순위(에이전트 또는 주입) → 오라클 → SearchRun → 전략, 이 네 조각을 잇는
    것이 이 함수의 전부다. 실제 탐색은 전략(기본값 coordinate_descent)이 하고,
    수락은 accept_step이 한다 - 왜 셋이 나뉘어 있는지는 위의 "탐색 이음매"
    주석에 있다.

    코너 **선택**은 하지 않는다 - 여유분을 인자로 받는다.

    한때 "nominal 한 점에서 돈다"고 적혀 있었고 지금은 틀린 말이다. 코너 축소가
    켜진 실행에서 `agents.simulate`는 선택 집합의 **최악값**을 돌려준다. 이
    루프가 여전히 모르는 것은 그 집합이 무엇인지이고, 아는 것은 매 단계가 같은
    기준점 위에서 재진다는 사실이다 - `CornerState.probe_frozen`이 이 단계
    동안 회전과 승격을 멈추므로, `records`의 목적값들과 `best_objective`를
    비교하는 것이 서로 다른 코너 집합에서 잰 값을 비교하는 일이 되지 않는다.

    records는 **버전 인덱스 → 그 버전에서 잰 값**이다. 확인 스윕이 실패해서
    이분 탐색이 중간 버전에 착지했을 때, 마지막 버전이 아니라 착지한 버전의
    수치를 보고해야 하기 때문이다. 기준 판정(criteria)도 같은 이유로 여기
    들어간다 - 리포트가 설명해야 하는 것은 돌려주는 덱이다."""
    objective_name = str(phase.objective)
    anchor_index = _version_index(state, canonical_name)
    baseline_verdict = evaluate_criteria(baseline_measurements, spec.all_criteria)
    records = {
        anchor_index: {
            "objective": objective_before,
            "area": area_before,
            "criteria": baseline_verdict["criteria"],
        }
    }

    # **최적화 시작 시점**의 넷리스트로 만든다 - 에어리어 게이트가 여기서 막아야
    # 할 것은 최적화 자신이 만든 성장이다.
    baseline_components = index_baseline_components(start_text)

    ranking = await _knob_ranking(
        spec, state, agents, start_text, baseline_verdict, allowances, objective_name, phase.label
    )
    oracle = SearchOracle(
        spec, state, agents, canonical_name, baseline_components, area_before, allowances, phase
    )
    run = SearchRun(
        spec,
        state,
        oracle,
        [Knob(c["refdes"], c["param"], c["direction"]) for c in ranking],
        canonical_name,
        objective_before,
        records,
        # 모듈 전역을 **여기서** 읽는다 - 테스트가 monkeypatch로 낮춰 예산
        # 소진 경로를 고정한다. 클래스 기본값으로 굳히면 그 경로가 사라진다.
        MAX_OPTIMIZE_STEPS,
        phase,
    )
    strategy = agents.search_strategy or coordinate_descent
    await strategy(run)

    return {
        "accepted": run.accepted,
        "rejected": run.rejected,
        "rejected_by_reason": run.rejected_by_reason,
        "records": run.records,
    }


async def run_optimization(
    netlist_texts: dict[str, str],
    spec,
    state,
    agents: OptimizerAgents,
    phase: "PhaseConfig | None" = None,
) -> dict:
    """최적화 단계의 공개 진입점. 어떤 실패도 결과를 크래시로 바꾸지 않는다.

    _run_simulation과 _run_sweep은 각자 예외를 삼키지만, 이 모듈의 **유일한
    LLM 호출**(agents.propose)에는 그 보호가 없었다. ClaudeSDKBackend.run은
    오류 ResultMessage 어디에서나 AgentExecutionError를 던진다 - 레이트 리밋,
    전송 오류, structured_output이 None, 그리고 약한 로컬 모델이 스키마를
    못 맞추는 경우(CLAUDE.md가 **예상된** 경우로 적어 둔 것이다). 그것이
    새어 나가면 cli.main의 asyncio.run까지 올라가 write_result_json /
    write_report_md가 아예 돌지 않는다 - **이미 PASS한 실행이** result.json도
    report.md도 없이 트레이스백으로 끝난다.

    ValueError를 함께 잡는 것은 orchestrator.run_orchestration과 같은
    belt-and-braces다: 주소 지정 게이트는 canonical 원문만 보므로, 다른
    테스트벤치 덱에서만 모호한 refdes는 apply_changes의 ValueError로 나온다.

    OSError까지 잡는 것은 이 단계가 **파일을 되읽기** 때문이다. _texts_at은
    버전 덱을 디스크에서 다시 읽고, 그 open이 실패하면 위 두 예외 중 어느
    것도 아니다. run_orchestration은 되읽기를 하지 않아 이 짝이 없다 -
    여기서만 필요한 세 번째다.

    되돌리기까지 해야 계약이 완성된다. 예외가 터진 시점에 이미 밀어 넣은
    버전이 있으면 그것은 **확인되지 않은** 덱이므로, 시작 버전까지 롤백한
    뒤에야 결과를 돌려준다 - "최적화는 시작보다 나쁜 결과를 내지 않는다"."""
    progress: dict = {}
    try:
        return await _optimize(netlist_texts, spec, state, agents, progress, phase)
    except (AgentExecutionError, ValueError, OSError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        # _optimize가 phase를 내부에서 resolve했을 수도 있는 지점(spec.optimize
        # 로부터)에서 터졌을 수 있으므로, 여기서도 같은 규칙으로 label을 다시
        # 구한다 - 그러지 않으면 면적 단계의 실패가 "optimize_failed"라는 전류
        # 단계 이름으로 잘못 남는다.
        label = (
            phase.label if phase is not None
            else (phase_from_spec(spec.optimize).label if spec.optimize is not None else "optimize")
        )
        state.log_event(f"{label}_failed", {"reason": reason})
        if progress.get("safe_index") is not None:
            _rollback_to(state, spec.canonical.name, progress["safe_index"])
        area_before = progress.get("area_before", 0.0)
        return _result(
            "UNCHANGED", state, None, None, area_before, area_before, failure=reason,
            area_coverage=progress.get("area_coverage"),
        )


def _margin_floor_allowances(
    baseline_measurements: dict, criteria: list[Criterion], floor: "MarginFloor | None"
) -> tuple[dict[str, float], str | None]:
    """코너를 잴 수 없을 때의 대체 여유분과 **그것을 만든 규칙 이름**.
    **규칙이 갈리는 유일한 곳** - 호출부는 이 함수를 부를 뿐, `rule`을
    다시 묻지 않는다. 둘째 반환값이 있는 이유가 그것이다: 이벤트에
    "어떤 규칙이 요청됐나"를 남기려고 호출부가 `floor.rule`을 다시 읽으면
    규칙을 읽는 지점이 둘이 되고, 그 둘이 갈리는 것이 이 저장소가
    compose.py에서 이미 치른 대가다.

    `floor`가 `None`이면 대체 여유분이 없다 - 모든 기준이 무방비로 남는다
    (오늘의 면적 단계 출하 상태이고, 2026-08-02 측정이 코너에서 깨지는
    것을 확인한 그 상태다). 그때 둘째 값도 `None`이다.

    f1은 `ratio_allowances`(임계값 비율) 그 자체이므로 모든 기준 이름이
    채워진다. f2는 `baseline_ratio_allowances`(기준선 여유의 배율)를 쓰고,
    적용할 수 없는 기준(측정 없음/이미 실패/임계값에 정확히 붙음)은
    반환하는 딕셔너리에 **넣지 않는다** - `guard_band_violations`가 없는
    이름을 여유분 0.0으로 읽으므로, 이름이 빠진 채로 남는 것 자체가 "이
    기준은 무방비"라는 사실이다. `_optimize`의 `_unguarded`가 바로 이
    규칙(allowances에 이름이 있는가)으로 무방비 목록을 다시 만들므로,
    `baseline_ratio_allowances`가 함께 돌려주는 제외 이름 목록을 여기서
    따로 들고 다니지 않아도 같은 사실이 두 번 다른 방법으로 재지지 않는다.

    **f3은 거절한다 - 조용히 f1으로 환원하지 않는다.** 사전 등록
    (`2026-08-02-area-phase-margin-floor-design.md`)은 f3을 "코너가 있으면
    코너 실측, 없으면 F1·F2의 우승자"로 적었고, 코너 실측 쪽은 이미 세
    규칙 전부가 하는 일이며(`_optimize`가 비율 위에 `corner_allowances`를
    덮어쓴다), 코너 없는 절반의 우승자는 **판정 규칙 3이 발화해 정해지지
    않았다**. 그래서 `rule="f3"`은 정의가 미완인 이름이지 f1의 동의어가
    아니다. 예전 코드는 여기서 f1을 골랐고 그 선택은 임의였다: 다음 사전
    등록이 F2 `r=0.75`를 우승자로 삼아 `MarginFloor("f3", 0.75)`를 배선하면
    그 0.75가 f1의 `g`로 읽혀 `vbgout >= 2.1` **및** `<= 0.32`라는 빈 구간을
    요구하고, 모든 기준이 기준선에서 infeasible이 되어 0스텝 수락으로 깨끗한
    `UNCHANGED`가 나온다 - 조용히 아무것도 안 하는, 이 저장소가 반복해서
    당한 그 모양이다. `run_optimization`이 `ValueError`를 잡으므로 잘못된
    호출부는 `optimize_failed`로 기록되고 끝난다."""
    if floor is None:
        return {}, None

    rule = floor.rule
    if rule == "f3":
        raise ValueError(
            "margin_floor rule 'f3' is not implemented: its corner half is what "
            "corner_allowances already does for every rule, and its corner-less half "
            "is whichever of f1/f2 wins - a choice verdict rule 3 of "
            "2026-08-02-area-phase-margin-floor-design.md never got to make. "
            "Name 'f1' or 'f2' explicitly, with the value in that rule's own unit "
            "(f1: ratio of |threshold|; f2: ratio of the baseline slack)."
        )

    if rule == "f1":
        return ratio_allowances(criteria, floor.value), rule
    if rule == "f2":
        allowances, _excluded = baseline_ratio_allowances(
            baseline_measurements, criteria, floor.value
        )
        return allowances, rule

    raise ValueError(f"unknown margin_floor rule {floor.rule!r}")


async def _optimize(
    netlist_texts: dict[str, str], spec, state, agents: OptimizerAgents, progress: dict,
    phase: "PhaseConfig | None",
) -> dict:
    """이미 모든 기준을 통과한 회로의 남은 마진을 목적값에 쓰는 결정론적 탐색.

    기존 루프의 verify_post를 쓰지 않는다. 그쪽 계약은 "나빠졌으면 롤백"인데
    좋은 최적화 단계는 **의도적으로** 마진을 소비하므로, 그 계약을 재사용하면
    성공한 축소마다 롤백이 난다. 수락 규칙은 결정론적이고 LLM이 필요 없다.

    실패(FAIL) 결과가 없다는 점도 의도적이다 - 개선하지 못하면 이미 통과한
    설계를 그대로 돌려준다.

    코너를 잴 수 있으면(spec.pvt_corners와 agents.verify_corners가 둘 다 있으면)
    nominal 탐색을 진입 스윕/확인 스윕으로 감싼다. nominal 마진과 코너 마진은
    다른 양이기 때문이다 - 한 점에서 잰 여유를 다 써도 코너에서 무너질 수 있다.
    진입 스윕은 앵커(되돌아갈 안전한 지점)이자 실측 여유분의 출처이고, 확인
    스윕이 실패하면 다시 탐색하지 않고 통과하는 마지막 버전으로 이분 탐색해
    내려간다. **최적화는 시작보다 나쁜 결과를 내지 않는다** - 이것이 없으면
    최적화를 돌렸다는 이유로 통과하던 설계가 실패로 끝난다.

    progress는 run_optimization의 예외 처리기가 읽는 쓰기 전용 메모다 - 예외가
    터진 시점에 "어느 버전까지 되돌려야 하는가"와 "시작 면적이 얼마였는가"는
    여기서만 알 수 있다."""
    canonical_name = spec.canonical.name
    start_text = netlist_texts[canonical_name]
    start_area = total_area(start_text)
    area_before = start_area.area

    if phase is None:
        # 명시적 phase가 없으면 spec.optimize에서 오늘의 전류 단계를 만든다 -
        # spec.optimize도 없으면 정말 할 일이 없다는 뜻이라 그때만 SKIPPED다.
        # 면적 단계는 언제나 phase를 명시적으로 들고 오므로 여기를 타지 않는다.
        #
        # **area_coverage를 계산하기 전에 phase를 정한다.** budget_enforced가
        # phase.area_budget을 읽어야 "예산이 있었지만 안 걸렸다"와 "예산이
        # 아예 없다"를 가를 수 있다 - phase 없이 area_before > 0 만으로 정하면
        # AREA_PHASE(area_budget=None)에서도 area_before가 양수라는 이유만으로
        # "예산이 걸렸다"고 말하게 된다. 그 구분이 이 필드가 존재하는 이유라고
        # 아래 area_coverage 주석이 말한다.
        if spec.optimize is None:
            state.log_event("optimize_skipped", {"reason": "spec declares no optimize block"})
            return _result(
                "SKIPPED", state, None, None, area_before, area_before,
                area_coverage={
                    "counted": start_area.counted,
                    "skipped": start_area.skipped,
                    "budget_enforced": False,
                    "reason": "no optimize phase ran (spec declares no optimize block)",
                },
            )
        phase = phase_from_spec(spec.optimize)

    # 면적 예산이 실제로 걸리는지를 여기서 한 번 정하고, 그 사실을 이력과
    # 결과 양쪽에 싣는다. AreaTotal이 counted/skipped를 드러내는 이유가 정확히
    # 이것인데(docstring), 지금까지 이 두 값을 읽는 곳이 자기 테스트 말고는
    # 없었다. area_before가 0이면 예산 비교가 통째로 꺼지는데, 그것이 실제로
    # 도달하는 경우다: 래퍼 셀 덱에서는 인스턴스마다 wn이 달라
    # build_param_envs가 그 이름을 버리고(tests/unit/test_area_total.py가
    # `counted == 0, skipped == 2`로 고정), 그러면 해소되는 소자가 하나도
    # 없다. **area_before > 0이어도 phase.area_budget이 None이면 마찬가지로
    # 꺼진다** - `area_within_budget`은 `self._phase.area_budget is not None`일
    # 때만 불린다(SearchOracle.evaluate). AREA_PHASE가 정확히 이 경우다: 목적
    # 자체가 면적이라 accept_step의 "목적이 내려가야 한다"는 요구가 이미 면적을
    # 단조 감소시키므로 별도 비율 상한을 켜 둬도 구조적으로 발화할 수 없다.
    #
    # 이 저장소에서 게이트가 조용히 무력화된 것이 세 번이고 세 번 다 실행
    # 로그에 보이지 않았다. 네 번째가 되지 않게 사실을 적는다.
    if area_before <= 0:
        budget_enforced = False
        area_reason = (
            f"the area budget is not enforced: no device's w/l/m could be resolved in "
            f"{canonical_name} ({start_area.counted} counted, {start_area.skipped} skipped), "
            f"so the starting area is 0 and every candidate's area ratio is undefined"
        )
    elif phase.area_budget is None:
        budget_enforced = False
        area_reason = (
            f"the area budget is not enforced: phase {phase.label!r} declares no area "
            f"budget (phase.area_budget is None)"
        )
    else:
        budget_enforced = True
        area_reason = None

    area_coverage = {
        "counted": start_area.counted,
        "skipped": start_area.skipped,
        "budget_enforced": budget_enforced,
        "reason": area_reason,
    }
    progress["area_before"] = area_before
    progress["area_coverage"] = area_coverage

    objective_name = str(phase.objective)

    # state가 인자와 같은 덱을 들고 있는지 맞춘다. 루프는 매 단계 state에서
    # 현재 텍스트를 다시 읽고 거절 시 state.rollback()으로 되돌리므로, 둘이
    # 갈라져 있으면 인자로 받은 덱이 아니라 state의 덱을 조용히 최적화하게
    # 되고, state가 비어 있으면 첫 롤백에서 터진다.
    if state.current_netlist_texts() != netlist_texts:
        state.push_netlist_version(netlist_texts)
    # 여기가 "돌아갈 수 있는 가장 이른 지점"이다 - 예외 경로의 착지점.
    progress["safe_index"] = _version_index(state, canonical_name)

    # 기준선 측정. 목적값도 에어리어도 여기서 고정된다.
    sim_result, sim_failure = await _run_simulation(agents.simulate, netlist_texts, spec)

    def _unguarded(allowances: dict[str, float]) -> list[str]:
        """이 allowances로 판정될 때 여유분이 아예 없는 기준의 이름.

        guard_band_violations가 없는 이름을 `allowances.get(name, 0.0)`으로
        읽는 것과 정확히 같은 기준이어야 한다 - 여기서 다른 기준을 쓰면
        "무방비"라는 이름표가 실제로 판정에 쓰인 여유분과 어긋난다.
        `_baseline_event`와 `_result`가 이 하나의 함수를 공유하는 이유가
        그것이다."""
        return [c.name for c in spec.all_criteria if c.name not in allowances]

    def _baseline_event(
        allowances: dict[str, float], floor_applied: str | None = None
    ) -> dict:
        """`{label}_baseline` 페이로드. unguarded_criteria는 그 호출 시점까지
        **실제로 확정된** allowances에서 계산한다 - 상수가 아니다.

        이 함수를 세 지점(목적값 미확보/진입 스윕 실패/정상 경로)에서 각각
        다른 allowances로 부른다: 각각 {}, ratio, ratio+corner_allowances다.
        상수 하나(예: 스펙의 전체 기준 이름)를 박아 넣으면 실행마다 같은 값이
        나와 "이 로그가 아무것도 안 할 때 어떻게 보이는가"에 답할 수 없고,
        코너 대응 실행에서는 진입 스윕이 대부분의 기준을 실측 여유분으로
        덮으므로 그 상수는 과대 보고가 아니라 **틀린** 이름표가 된다 -
        "무방비"라고 말하면서 실은 방비돼 있다. 이름이 없으면
        guard_band_violations가 그 이름을 여유분 0.0으로 읽는다 - 그것이
        "무방비"의 정의다.

        **여유분 하한 두 필드는 언제나 쓴다 - 하한이 없어도 `None`으로
        쓴다.** 안 쓰면 "하한이 없었다"와 "이 계측이 사라졌다"가
        history.jsonl에서 같은 모양이 된다(`tuning_retries`·`corner_seed`가
        무조건 쓰이는 것과 같은 이유다). 두 필드가 갈라지는 것이 신호다:
        f1이 만드는 allowances는 `guard_band=g`가 만드는 것과 **바이트까지
        같아서**, 이 필드가 없으면 하한 없는 실행과 `MarginFloor("f1", g)`
        실행이 로그만으로는 구별되지 않는다. `_applied`가 `None`인데
        `_requested`가 이름을 들고 있으면 그것은 "하한을 줬는데 이 경로가
        쓰지 않았다"는 사실이다.

        **그 비대칭에서 "코너를 잴 수 있는 실행이었다"를 읽지 말 것.** 같은
        모양을 내는 경로가 셋이고, 그 중 둘은 코너와 무관하다:

          1. 코너 대응 정상 경로 - `corner_allowances`가 실측을 냈고 그것이
             이겼으므로 하한을 쓰지 않았다. (여기만이 "코너가 덮었다"이다.)
          2. 목적값 미확보 반환 - `_baseline_event({})`, `floor_applied`는
             기본값 `None`. `corner_capable`을 계산하기도 **전에** 반환하므로
             **코너 없는 스펙에서도** 이 모양이 나온다.
          3. 진입 스윕 실패 반환 - `_baseline_event(ratio)`, 역시 기본값
             `None`. 코너 대응 실행이긴 하지만 코너 여유분은 **한 번도
             확정되지 않았다** - allowances는 ratio뿐이다.

        2·3을 1로 읽으면 "코너에서는 하한이 무해하더라"는 결론이 나오는데,
        그 오독은 이 필드가 막으려던 바로 그것이다. 셋을 가르는 것은 이
        이벤트 **안**이 아니라 짝지어 남는 다른 이벤트다: 1은 그 앞에
        `{label}_entry_sweep`이 통과로 남고, 3은 같은 이벤트가 실패로 남으며,
        2는 `objective: None`인 손수 지은 `{label}_step` 한 건이 뒤따른다.
        (이벤트 하나만 보고 가르고 싶으면 사유 코드를 하나 더 실어야 한다 -
        오늘은 넣지 않았다.)"""
        return {
            "objective": objective_name,
            "area_before": area_before,
            "area_counted": area_coverage["counted"],
            "area_skipped": area_coverage["skipped"],
            "area_budget_enforced": area_coverage["budget_enforced"],
            "area_reason": area_coverage["reason"],
            "unguarded_criteria": _unguarded(allowances),
            "margin_floor_rule_requested": floor_rule,
            "margin_floor_rule_applied": floor_applied,
            **(sim_result or {"failure": sim_failure}),
        }

    baseline_measurements = sim_result["measurements"] if sim_result else {}
    # 하한을 여기서 한 번 푼다. 코너를 잴 수 있는 실행은 결과를 쓰지 않지만
    # (`corner_allowances`가 이깁니다 - 아래 분기 참고) **요청된 규칙 이름은
    # 그 실행도 기록해야 한다**: 하한을 줬는데 무시됐다는 사실이 어디에도
    # 안 남으면 "코너에서는 하한이 무해하다"는 틀린 결론이 나온다.
    # 정의가 미완인 규칙(f3)은 여기서 터진다 - 코너 유무와 무관하게.
    floor_allowances, floor_rule = _margin_floor_allowances(
        baseline_measurements, spec.all_criteria, phase.margin_floor
    )
    objective_before = _objective_value(phase.objective, baseline_measurements, area_before)

    if objective_before is None:
        # 목적값을 못 재면(또는 기준선 시뮬레이션 자체가 실패하면) 개선 여부를
        # 판정할 수 없다. 통과한 설계를 그대로 둔다. allowances는 아직 하나도
        # (비율도 코너도) 계산되지 않았으므로, 이 시점에 정직하게 말할 수 있는
        # 것은 "전 기준이 무방비"뿐이다.
        state.log_event(f"{phase.label}_baseline", _baseline_event({}))
        state.log_event(f"{phase.label}_step", {
            "refdes": None, "param": None, "before": None, "after": None,
            "objective": None, "area": area_before, "accepted": False,
            "gate": None,
            "reason": sim_failure
            or (
                f"objective {objective_name!r} could not be evaluated at the "
                f"baseline (no value among the measurements, and no derivable value)"
            ),
        })
        return _result(
            "UNCHANGED", state, None, None, area_before, area_before,
            area_coverage=area_coverage, unguarded_criteria=_unguarded({}),
        )

    # 코너를 잴 수단이 있는가. 스펙에 코너가 없거나 스윕 콜러블이 없으면 코너
    # **인식이 없는** 탐색이다 - 비율 여유분을 쓰고, 결과는 확인이 없었다고
    # 말한다(corner_confirmed=False). 검증하지 않은 것을 검증된 것처럼 보고하지
    # 않는다.
    corner_capable = spec.pvt_corners is not None and agents.verify_corners is not None
    anchor_index = _version_index(state, canonical_name)
    entry_sweep = None
    # None이면 비율 폴백이 없다 - 면적 단계처럼 선언 없이 도는 phase는 없는
    # 숫자를 지어내지 않는다. 전류 단계는 언제나 guard_band를 들고 있으므로
    # 이 삼항이 흐르는 값을 바꾸지 않는다.
    ratio = (
        ratio_allowances(spec.all_criteria, phase.guard_band)
        if phase.guard_band is not None
        else {}
    )

    if corner_capable:
        # 진입 스윕. 추가 비용이 아니라 **앵커**다: "실패하면 시작점으로
        # 되돌린다"는 회수 계획은 시작점이 코너를 통과할 때만 안전하다.
        entry_sweep, entry_failure = _run_sweep(
            agents.verify_corners, state.current_netlist_texts()
        )
        state.log_event(f"{phase.label}_entry_sweep",
                         _sweep_event(entry_sweep, entry_failure, version=anchor_index))
        if entry_sweep is None or not entry_sweep.get("overall_pass"):
            # 코너를 못 버티는 설계에서 마진을 더 깎을 이유가 없다. 되돌아갈
            # 안전한 지점이 아예 없으므로 한 단계도 밟지 않는다. corner_allowances는
            # 돌지 않았으므로 이 시점에 정직하게 아는 여유분은 ratio뿐이다.
            state.log_event(f"{phase.label}_baseline", _baseline_event(ratio))
            # tightest_slack은 여기서 None으로 남는다(final_criteria와 같은
            # 자리) - baseline_measurements는 이미 채워져 있으므로 값을 낼
            # 수는 있었다는 뜻이다(evaluate_criteria 한 번이면 된다). 이
            # 경로에서는 corner_failure/entry_sweep이 이미 "코너를 못
            # 버틴다"는 사실을 실어 나르므로 버려도 정보 손실이 크지
            # 않다고 판단했지만, 여전히 잴 수 있었던 값을 안 재는
            # 선택이다 - 필요해지면 여기서 계산해 채운다.
            return _result(
                "UNCHANGED", state, objective_before, objective_before,
                area_before, area_before, pvt_sweep=entry_sweep,
                corner_failure=entry_failure, area_coverage=area_coverage,
                unguarded_criteria=_unguarded(ratio),
            )
        # 균일한 비율(추측) 대신 이미 값을 치른 스윕에서 기준별 실측 여유분을
        # 읽는다. reference는 measurement로, 스윕은 기준 이름으로 색인되므로
        # 둘을 잇는 criteria 목록이 반드시 필요하다 - 인자가 셋인 이유다.
        #
        # **기준점은 baseline_measurements, 즉 탐색(_search)이 실제로 보는
        # 값이다 - 별도로 다시 잰 nominal이 아니다.** 오늘은 baseline이 곧
        # nominal 한 점이라 이 구분이 안 보이지만, 탐색이 축소 코너 집합의
        # 최악값을 보도록 바뀌는 순간(코너-인식 simulate가 배선되는 이후
        # 작업) baseline_measurements 자체가 그 최악값이 되고, 이 자리는
        # 코드를 안 고쳐도 저절로 옳아진다. 여기서 nominal을 따로 다시 재서
        # 넘기면 탐색이 이미 최악에 가까운 점을 보는데 가드가 같은 간격을
        # 두 번 세게 된다 - 이 파일이 고치는 실패 모양이 정확히 그것이다.
        #
        # **비율 여유분 위에 덮어쓴다.** corner_allowances는 스윕이나 nominal이
        # 값을 주지 않은 기준을 의도적으로 **뺀다**(0을 넣으면 "코너가 이 기준을
        # 전혀 안 움직인다"는 거짓 사실이 되므로 그 규칙 자체는 옳다). 그런데
        # 소비자인 guard_band_violations는 없는 이름을 `allowances.get(name, 0.0)`
        # 으로 읽어 **여유분 0**, 즉 가드밴드 없음으로 처리한다. 그래서 구멍이
        # 생길 수 있는 표를 그대로 넘기면, 하필 코너 거동을 모르는 기준에서만
        # 가드가 사라져 대체하려던 비율 가드보다 엄격하게 느슨해진다. 실제로
        # 재현된다: nominal 측정은 cli.py의 LLM 매개 simulate_fn에서, 스윕은
        # sim_backend.run에서 나오는 서로 다른 추출 경로라, 한쪽에만 없는
        # measurement가 생기면 그 기준은 가드를 통째로 잃는다.
        #
        # 병합하면 잰 것은 실측이 이기고(코너에 둔감한 기준은 여전히 더 작은
        # 실측 여유분을 받는다 - 측정하는 이유가 보존된다) 못 잰 것은 비율
        # 추측이 구멍을 막는다. 고칠 자리는 여기(이음매)이지 Task 3의 생략
        # 규칙이 아니다.
        allowances = {
            **ratio,
            **corner_allowances(baseline_measurements, entry_sweep, spec.all_criteria),
        }
        # **여유분 하한은 이 분기에서 쓰이지 않는다** - 코너를 실제로 잰
        # 값이 있으므로 대체값이 필요 없다. 하한을 받고도 안 썼다는 사실은
        # `margin_floor_rule_applied=None`으로 남는다(요청 쪽은 이름을 든
        # 채로). 그래야 "하한을 켰는데 코너에서 아무 차이가 없더라"가
        # "하한이 코너에서 무해하다"로 오독되지 않는다.
        floor_applied = None
    else:
        # 코너를 잴 수 없는 절반 - margin_floor가 있으면 그 규칙, 없으면
        # 오늘 그대로 ratio(guard_band 대체, 없으면 {}). margin_floor가
        # None인 한 이 분기의 값은 어제와 한 글자도 다르지 않다.
        allowances = floor_allowances if phase.margin_floor is not None else ratio
        floor_applied = floor_rule

    # allowances가 이 실행이 실제로 쓸 최종값으로 확정되는 지점이 여기다 -
    # 코너 대응이면 실측이 섞이고, 아니면 비율뿐이다. baseline 이벤트를 더
    # 일찍 남기면 아직 모르는 것을 안다고 말하게 되므로 여기서 남긴다.
    state.log_event(f"{phase.label}_baseline", _baseline_event(allowances, floor_applied))

    # **기준선이 자기 가드밴드를 지키는가.** 지키지 못하면 어떤 후보도 수락될
    # 수 없다: 수락 규칙이 매 단계 이 같은 검사를 돌리기 때문이다.
    #
    # 조건은 "pvt_corners가 없다"가 아니다 - benchmarks/bandgap의 spec.yaml이
    # 그 사례일 뿐이고(비율 대체가 vbgout_v >= 1.44 AND <= 1.024라는 빈 구간을
    # 요구하는데 1.2389V 기준선이 이미 위반한다), 실측 경로에서도 도달한다:
    # 어떤 기준에서 nominal이 모든 코너보다 나쁘면 |worst - nominal|이
    # nominal보다 엄격한 허용선을 만든다.
    #
    # **조기 반환하지 않는다 - 의도적인 선택이다.** (1) 위반한 기준을 도로
    # 안쪽으로 미는 단계가 원리적으로 존재한다(예: 가드를 깬 것이 목적값과
    # 같은 방향으로 움직이는 기준일 때). 여기서 끊으면 그 경우를 영구히
    # 못 찾는다. (2) 비용 상한이 이미 작다: 후보 하나가 거절되면 그 후보는
    # 소진되므로(루프 끝의 break) 최악이 후보 수만큼의 시뮬레이션이다.
    # (3) 이 저장소가 반복해서 당한 실패는 "조용히 아무것도 안 함"이지
    # "너무 많이 함"이 아니다 - 그래서 고치는 자리는 가시성이다.
    # 사유는 이벤트로도 결과로도 나간다.
    guard_infeasible = guard_band_violations(
        baseline_measurements, spec.all_criteria, allowances
    )
    # 조건 없이 남긴다. 위반이 있을 때만 남기면 "검사했고 문제없었다"와
    # "검사 자체가 사라졌다"가 history.jsonl에서 같은 모양이 된다.
    state.log_event(f"{phase.label}_guard_infeasible", {
        "infeasible": bool(guard_infeasible),
        "violations": guard_infeasible,
        "allowances": allowances,
        "measured_allowances": corner_capable,
    })

    outcome = await _search(
        spec, state, agents, canonical_name, start_text, baseline_measurements,
        objective_before, area_before, allowances, phase,
    )
    accepted = outcome["accepted"]
    rejected = outcome["rejected"]
    rejected_by_reason = outcome["rejected_by_reason"]
    records = outcome["records"]

    def _final(
        status: str, version: int, accepted_count: int, rejected_count: int, sweep,
        corner_failure: str | None = None, walked_back: int = 0,
    ) -> dict:
        record = records[version]
        # 코너 확인에서 되돌려진 단계는 탐색이 거절한 것이 아니다 - nominal에서
        # 수락됐고 코너가 뒤집은 것이다. 총계에는 이미 들어가 있었으므로
        # (rejected + accepted - survived), 사유 표에도 자기 칸으로 들어간다.
        by_reason = dict(rejected_by_reason)
        by_reason["corner_walked_back"] += walked_back
        return _result(
            status, state, objective_before, record["objective"], area_before, record["area"],
            accepted=accepted_count, rejected=rejected_count,
            rejected_by_reason=by_reason, pvt_sweep=sweep,
            corner_failure=corner_failure, guard_infeasible=guard_infeasible,
            area_coverage=area_coverage, final_criteria=record.get("criteria"),
            # 이 실행이 실제로 판정에 쓴 allowances에서 잰다 - 진입 스윕 이후로
            # 고정된 값이므로 탐색 결과가 어떤 버전에 착지하든 같다.
            unguarded_criteria=_unguarded(allowances),
            # record["criteria"]는 착지한 버전의 것이다(기준선일 수도,
            # 이분 탐색이 되돌아온 중간 버전일 수도) - final_criteria와
            # 같은 덱을 설명해야 하므로 같은 record에서 잰다. accepted가
            # 0이면 record는 baseline_verdict 그대로이므로, 그때의
            # 최솟값은 "기준선의 것"이 된다 - 태우지 않은 것과 재지
            # 않은 것을 구분하는 지점이다.
            tightest_slack=_tightest_slack(spec.all_criteria, record.get("criteria") or []),
        )

    if not corner_capable:
        return _final(
            "OPTIMIZED" if accepted else "UNCHANGED",
            _version_index(state, canonical_name), accepted, rejected, None,
        )

    if not accepted:
        # 밟은 단계가 없으므로 확인할 것도 없다. 진입 스윕이 곧 이 넷리스트의
        # 스윕이다 - 다시 도는 것은 같은 덱에 같은 값을 두 번 치르는 것이다.
        return _final("UNCHANGED", anchor_index, 0, rejected, entry_sweep)

    end_index = _version_index(state, canonical_name)
    confirm_sweep, confirm_failure = _run_sweep(
        agents.verify_corners, state.current_netlist_texts()
    )
    state.log_event(f"{phase.label}_confirm_sweep",
                     _sweep_event(confirm_sweep, confirm_failure, version=end_index))
    if confirm_sweep is not None and confirm_sweep.get("overall_pass"):
        return _final("OPTIMIZED", end_index, accepted, rejected, confirm_sweep)

    # 확인이 실패했다. 다시 탐색하지 않는다 - 어느 단계가 코너를 깼는지는
    # 이미 구간 안에 있고, 통과하는 마지막 지점을 이분 탐색으로 찾는 편이
    # 상한이 있다. 최악이어도 앵커에 착지하므로 시작보다 나빠질 수 없다.
    landed, landed_sweep = _bisect_last_passing(
        state, agents, canonical_name, anchor_index, entry_sweep, phase.label
    )
    survived = landed - anchor_index
    state.log_event(f"{phase.label}_bisect_result",
                     {"version": landed, "anchor": anchor_index, "end": end_index,
                      "steps_kept": survived, "steps_walked_back": accepted - survived})
    # 착지가 앵커면 남은 것이 없다 - 시작 설계를 그대로 돌려준다. 보고하는
    # 수락 수는 **살아남은** 단계 수다: 코너에서 되돌린 단계를 수락으로 세면
    # 결과가 돌려주는 넷리스트를 설명하지 못한다.
    return _final(
        "UNCHANGED" if survived == 0 else "OPTIMIZED",
        landed, survived, rejected + (accepted - survived), landed_sweep,
        walked_back=accepted - survived,
        # 확인 스윕이 아예 돌지 못했다면(터졌다면) 그 사유도 결과에 실린다 -
        # "코너가 깨져서 되돌아왔다"와 "스윕을 못 돌려서 되돌아왔다"는
        # 다른 사실이고, 후자는 고칠 대상이 회로가 아니다.
        corner_failure=confirm_failure,
    )


# 전략 등록. `mads`는 이 모듈의 SearchRun/ProposedStep을 쓰므로 반대 방향으로만
# 의존할 수 없다 - 그래서 표를 만든 **뒤** 여기서 부른다. 어느 쪽을 먼저
# import해도 순환이 풀린다: 이 줄은 속성이 아니라 모듈만 묶고(`import`),
# analogcoder.mads는 이 줄 위에서 이미 정의된 이름만 가져간다.
#
# 이 줄이 없으면 SEARCH_STRATEGIES에 `mads`가 없고, scripts/search_ab.py가
# 이 표만 읽으므로 A/B에서 전략 이름이 조용히 사라진다.
import analogcoder.mads  # noqa: E402,F401


def _area_change(refdes: str, param: str, current: float, integer: bool) -> dict | None:
    """한 스텝 줄인 변경 dict. 순위 계산에 주입되는 유일한 통로다.

    `_next_value`/`_format_value`를 그대로 쓴다 - 순위가 가정하는 스텝과
    탐색이 실제로 밟는 스텝이 같아야 한다."""
    target = _next_value(current, integer, "decrease")
    if target is None:
        return None
    return {
        "refdes": refdes,
        "param": param,
        "old_value": _format_value(current, integer),
        "new_value": _format_value(target, integer),
    }


def _area_knob_state(
    netlist_text: str, baseline_components: dict, refdes: str, param: str
) -> KnobState | None:
    """면적 순위가 노브 하나를 읽는 법. `SearchOracle.knob_state`와 같은
    게이트 순서를 쓰지만, `state.current_netlist_texts()`가 아니라 **이
    함수가 받은 텍스트 하나만** 읽는다.

    면적 단계가 순위를 매길 때 재는 덱과 값을 읽는 덱이 갈라지면 안 된다 -
    `_optimize`가 탐색을 시작하기 전에 `state.current_netlist_texts() !=
    netlist_texts`를 확인해 맞추는 이유가 정확히 이것이다. 면적 순위는 그
    맞춤보다 먼저 도므로, 노브의 현재 값은 인자로 받은 텍스트에서 직접
    읽어야 한다. state의 텍스트에서 읽으면 순위와 값이 서로 다른 덱을
    설명하게 되고, 그 어긋남은 조용하다: rank_by_area_gain은
    apply_changes/refdes 해석의 실패만 unknown으로 삼킬 뿐, 값이 그른 채로
    성공한 계산은 걸러내지 않는다."""
    gate, _ = _gate_addressing(netlist_text, {"refdes": refdes, "param": param})
    if gate is not None:
        return None
    component = baseline_components.get(refdes)
    token = _deck_token(component, param) if component is not None else None
    if token is None:
        return None
    value = _current_value(component, token)
    if value is None:
        return None
    return KnobState(token=token, value=value, integer=is_count_param(component, token))


async def run_area_optimization(
    netlist_texts: dict[str, str], spec, state, agents, margin_floor: "MarginFloor | None" = None
) -> dict:
    """면적 최소화 단계. **선언이 필요 없고 LLM을 부르지 않는다.**

    `run_optimization`을 그대로 쓰되 둘을 바꾼다: 단계 설정을 `AREA_PHASE`로
    주고, 계산한 노브 순위를 `OptimizerAgents.knob_ranking`에 **주입**한다.
    주입된 순위가 있으면 `_knob_ranking`이 에이전트를 부르지 않으므로 LLM을
    빼기 위한 새 배선이 필요 없다.

    `margin_floor`는 기본값 `None`이면 `AREA_PHASE`를 그대로 쓴다 - 오늘의
    호출부와 한 글자도 다르지 않다. 값을 주면 `dataclasses.replace`로 그
    필드 하나만 다른 새 `PhaseConfig`를 만든다 - **`AREA_PHASE` 자체는
    바뀌지 않는다**(Task 4의 측정 스크립트가 여럿을 순서대로 도는 동안 같은
    모듈 상수를 공유 상태로 바꾸면 조합끼리 오염된다).

    **하한은 코너를 잴 수 없는 실행에서만 쓰인다.** 스펙이 `pvt_corners`를
    선언하고 `OptimizerAgents.verify_corners`가 배선돼 있으면 `_optimize`는
    진입 스윕에서 읽은 **실측** 여유분을 쓰고 하한은 아예 참조하지 않는다 -
    여기에 하한을 넘겨도 결과는 하한 없는 실행과 같다. 그것은 결함이 아니라
    설계다(실측이 추측을 이긴다). 다만 **그 실행에서도 하한을 준 사실은
    기록된다**: `{label}_baseline` 이벤트의
    `margin_floor_rule_requested`가 규칙 이름을 들고
    `margin_floor_rule_applied`가 `None`이다. 그 비대칭이 신호 전부다 -
    "코너에서는 하한이 아무 차이를 안 만든다"는 결론을 이 두 필드를 보지
    않고 내리면 안 된다.

    `counted == 0`에서 REFUSED를 내는 것은 그것이 UNCHANGED와 다른 사실이기
    때문이다 - "쟀는데 못 줄였다"와 "잴 수 없었다"를 합치면 면적 모델이 이
    덱에서 아무것도 못 읽는다는 것을 아무도 모른다.

    준비 구간(구조 유도, 노브 값 읽기, 순위 계산) 전체를 `run_optimization`의
    호출 **앞에** 두면서도 같은 예외 계약 아래 둔다 - "최적화에는 FAIL이
    없다"는 이 모듈 전체의 약속이고, `run_optimization`은 자기 안에서 터진
    실패만 잡는다. 이 준비 구간에서 예외가 새어 나가면 이미 PASS한 실행이
    result.json도 report.md도 없이 트레이스백으로 끝난다 - 이 저장소가 이미
    기록한 실패 모양이다. REFUSED도 나머지 결과와 같은 모양(`_result`)으로
    돌려준다 - 그러지 않으면 `steps_accepted`/`final_netlist_paths`를 읽는
    소비자가 조용히 기본값이나 빈 값을 받는다."""
    canonical_name = spec.canonical.name
    start_text = netlist_texts[canonical_name]
    area_before = 0.0

    try:
        base = DEFAULT_AREA_MODEL(start_text)
        area_before = base.area
        if base.counted == 0:
            reason = (
                f"area model resolved no device on this deck "
                f"(counted={base.counted}, skipped={base.skipped})"
            )
            state.log_event(
                "optimize_area_refused",
                {"reason": reason, "counted": base.counted, "skipped": base.skipped},
            )
            result = _result("REFUSED", state, None, None, base.area, base.area, failure=reason)
            # 브리프가 정한 이름을 대체가 아니라 추가로 남긴다 - "reason"을
            # 읽는 기존 소비자와 "failure"를 읽는 나머지 결과 소비자 둘 다
            # 맞아야 한다.
            result["reason"] = reason
            return result

        structure = derive_structure(start_text, spec.circuit_name)
        baseline_components = index_baseline_components(start_text)
        candidates = []
        for entry in structure.tunable:
            knob_state = _area_knob_state(
                start_text, baseline_components, entry.refdes, entry.param
            )
            if knob_state is None:
                continue
            # entry.param이 아니라 knob_state.token을 쓴다 - 덱에 실제로 적힌
            # 철자다(_deck_token 참고). 오늘은 둘이 같지만, 대소문자가 섞인
            # 덱에서는 token 쪽이 apply_changes가 실제로 찾을 수 있는 이름이다.
            candidates.append(
                (entry.refdes, knob_state.token, knob_state.value, knob_state.integer)
            )

        ranking = rank_by_area_gain(start_text, candidates, _area_change)
        state.log_event(
            "optimize_area_ranking",
            {
                "ranked": [
                    {"refdes": e.refdes, "param": e.param, "gain": e.gain} for e in ranking.entries
                ],
                "zero_gain": ranking.zero_gain,
                "unknown": ranking.unknown,
                "counted": base.counted,
                "skipped": base.skipped,
                "area_before": base.area,
                # unguarded_criteria는 여기 없다 - 스펙만의 상수라 실행마다
                # 똑같은 값을 냈고("이 로그가 아무것도 안 할 때 어떻게 보이는가"에
                # 답할 수 없다), 코너 대응 실행에서는 진입 스윕이 실측 여유분으로
                # 대부분의 기준을 덮으므로 "이 기준은 무방비"라는 주장 자체가
                # 틀린 이름표였다. 진짜 사실은 allowances가 실제로 확정되는
                # `_optimize`의 `{label}_baseline` 이벤트에만 있다 - 두 단계
                # 모두에서 남는다.
            },
        )

        area_agents = OptimizerAgents(
            propose=agents.propose,
            simulate=agents.simulate,
            verify_corners=agents.verify_corners,
            search_strategy=agents.search_strategy,
            knob_ranking=[
                {"refdes": e.refdes, "param": e.param, "direction": "decrease"}
                for e in ranking.entries
            ],
        )
    except (AgentExecutionError, ValueError, OSError) as exc:
        # run_optimization의 예외 처리기(:1171-1188 부근)와 같은 계약이다:
        # 이 단계에도 FAIL이 없다. 여기서 잡는 것은 run_optimization에 들어가기
        # **전**의 구간이므로, 안 잡으면 그 계약이 여기서 뚫린다.
        reason = f"{type(exc).__name__}: {exc}"
        state.log_event("optimize_area_failed", {"reason": reason})
        return _result("UNCHANGED", state, None, None, area_before, area_before, failure=reason)

    phase = AREA_PHASE if margin_floor is None else replace(AREA_PHASE, margin_floor=margin_floor)
    return await run_optimization(netlist_texts, spec, state, area_agents, phase=phase)
