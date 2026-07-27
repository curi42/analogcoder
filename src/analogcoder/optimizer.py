from dataclasses import dataclass
from typing import Callable

from analogcoder.area import total_area
from analogcoder.area_limits import check_area_growth, index_baseline_components, is_count_param
from analogcoder.judge_tools import (
    corner_allowances,
    evaluate_criteria,
    guard_band_violations,
    ratio_allowances,
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
from analogcoder.structure import derive_structure
from analogcoder.structure_view import render_netlist, render_structure, select_focus

MAX_OPTIMIZE_STEPS = 20
STEP_RATIO = 0.9


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


def _result(
    status: str,
    state,
    objective_before: float | None,
    objective_after: float | None,
    area_before: float,
    area_after: float,
    accepted: int = 0,
    rejected: int = 0,
    pvt_sweep: dict | None = None,
    corner_failure: str | None = None,
) -> dict:
    return {
        "status": status,
        "objective_before": objective_before,
        "objective_after": objective_after,
        "area_before": area_before,
        "area_after": area_after,
        "steps_accepted": accepted,
        "steps_rejected": rejected,
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
        "final_netlist_paths": state.current_netlist_paths(),
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
    state, agents: OptimizerAgents, canonical_name: str, anchor_index: int, anchor_sweep: dict
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
        state.log_event("optimize_bisect_probe", _sweep_event(sweep, failure, version=mid))
        if sweep is not None and sweep.get("overall_pass"):
            lo = mid
            passing[lo] = sweep
        else:
            # 실패한(또는 터진) 스윕은 통과가 아니다. 어느 쪽이든 앵커 쪽으로
            # 미는 방향이라 보수적이다.
            hi = mid

    _rollback_to(state, canonical_name, lo)
    return lo, passing[lo]


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
) -> dict:
    """nominal 한 점에서 도는 탐색 루프. 코너는 모른다 - 여유분을 인자로 받는다.

    records는 **버전 인덱스 → 그 버전에서 잰 값**이다. 확인 스윕이 실패해서
    이분 탐색이 중간 버전에 착지했을 때, 마지막 버전이 아니라 착지한 버전의
    수치를 보고해야 하기 때문이다."""
    objective_name = spec.optimize.objective
    anchor_index = _version_index(state, canonical_name)
    records = {anchor_index: {"objective": objective_before, "area": area_before}}

    # **최적화 시작 시점**의 넷리스트로 만든다 - 에어리어 게이트가 여기서 막아야
    # 할 것은 최적화 자신이 만든 성장이다.
    baseline_components = index_baseline_components(start_text)

    structure = derive_structure(start_text, spec.circuit_name)
    paths = build_signal_paths(structure)
    # 실패한 기준이 없으므로 초점 씨앗도 없다. select_focus의 전 블록 폴백이
    # 여기서는 정상 동작이다 - 최적화는 특정 실패를 쫓는 것이 아니다.
    focus = select_focus(structure, paths, set(), set(), start_text)
    structure_view = render_structure(structure, paths, find_patterns(structure), focus)
    netlist_view = render_netlist(start_text, focus)

    margins = [
        {**entry, "allowance": allowances.get(entry["name"], 0.0)}
        for entry in evaluate_criteria(baseline_measurements, spec.all_criteria)["criteria"]
    ]
    proposal = await agents.propose(structure_view, margins, objective_name, netlist_view)
    state.log_event("optimize_proposal", {"objective": objective_name, **proposal})

    best_objective = objective_before
    best_area = area_before
    accepted_count = 0
    rejected_count = 0
    steps = 0

    for candidate in proposal.get("candidates", []):
        refdes = candidate["refdes"]
        param = candidate["param"]
        direction = candidate["direction"]

        while steps < MAX_OPTIMIZE_STEPS:
            steps += 1
            current_texts = state.current_netlist_texts()
            current_text = current_texts[canonical_name]

            event = {
                "refdes": refdes, "param": param, "direction": direction,
                "before": None, "after": None,
                "objective": None, "area": None, "accepted": False,
                "gate": None, "reason": None,
            }

            gate, feedback = _gate_addressing(current_text, {"refdes": refdes, "param": param})
            if gate is not None:
                event["gate"] = gate
                event["reason"] = feedback
                state.log_event("optimize_step", event)
                rejected_count += 1
                break

            # 값 읽기와 정수 판정 둘 다 index_baseline_components의 색인을 쓴다.
            # 같은 표를 쓰지 않으면 "어느 소자의 값을 읽어 한 단계 옮기는가"와
            # "어느 소자가 편집되는가"가 갈라질 수 있다 - 이 저장소가 이미 두 번
            # 닫은 넷리스트 해소 이중화 결함의 더 나쁜 판본이다.
            component = index_baseline_components(current_text).get(refdes)
            token = _deck_token(component, param) if component is not None else None
            before = _current_value(component, token) if token is not None else None
            if before is None:
                event["reason"] = (
                    f"cannot read a numeric current value for {refdes}.{param} in the netlist"
                )
                state.log_event("optimize_step", event)
                break
            # 실제로 편집한 철자를 기록한다. 제안의 철자를 남기면 대소문자가
            # 섞인 덱에서 이력은 `w`라고 하는데 넷리스트는 `W`를 든다.
            event["param"] = token
            event["before"] = before

            # 정수성은 이름이 아니라 param이 **도달하는 토큰**에서 나온다.
            # `Xa ... mult=4`가 본문 `m='mult'`에 도달하면 이름은 mult지만
            # 개수다 - 이름만 보면 3.6을 만들고 에어리어 게이트가 정수 위반으로
            # 되받는다. area_limits와 같은 함수를 쓰므로 갈라질 수 없다.
            integer = is_count_param(component, token)
            after = _next_value(before, integer, direction)
            if after is None:
                event["reason"] = f"{refdes}.{param} cannot move further in direction {direction!r}"
                state.log_event("optimize_step", event)
                break
            new_value = _format_value(after, integer)
            # 로그의 after는 넷리스트에 실제로 적힌 값이어야 한다. 원시 float를
            # 남기면 다음 단계의 before(덱에서 다시 읽은 값)와 미세하게
            # 어긋나 이력이 연결되지 않는다.
            event["after"] = parse_spice_value(new_value)

            # param이 아니라 **덱에 적힌 철자**(token)로 쓴다 - _deck_token 참고.
            change = {"refdes": refdes, "param": token, "new_value": new_value}
            # 에어리어 게이트만 new_value를 읽으므로 값이 정해진 뒤에 온다.
            area_ok, area_feedback = check_area_growth(baseline_components, [change])
            if not area_ok:
                event["gate"] = "area"
                event["reason"] = area_feedback
                state.log_event("optimize_step", event)
                rejected_count += 1
                break

            new_texts = {name: apply_changes(text, [change]) for name, text in current_texts.items()}
            state.push_netlist_version(new_texts)

            # 에어리어는 파생이라 공짜지만 목적값은 재야 안다. 그 비대칭이
            # 이 루프를 감당 가능하게 만드는 전부다 - 예산 초과는 시뮬레이션
            # 앞에서 걸러진다.
            area = total_area(new_texts[canonical_name]).area
            event["area"] = area
            if area_before > 0 and area / area_before > spec.optimize.area_budget:
                event["reason"] = (
                    f"area {area:g} is {area / area_before:.3f}x the starting area, "
                    f"over the {spec.optimize.area_budget:g}x budget"
                )
                state.log_event("optimize_step", event)
                state.rollback()
                rejected_count += 1
                break

            step_sim, sim_failure = await _run_simulation(agents.simulate, new_texts, spec)
            if step_sim is None:
                # 회로가 시뮬레이터를 통과하지 못하는 지점까지 갔다는 뜻이다
                # (예: sky130 소자 bin을 벗어난 폭). 되돌리고 후보를 소진한다 -
                # 여기서 예외가 새어 나가면 통과한 실행이 크래시가 된다.
                event["reason"] = sim_failure
                state.log_event("optimize_step", event)
                state.rollback()
                rejected_count += 1
                break

            measurements = step_sim["measurements"]
            verdict = evaluate_criteria(measurements, spec.all_criteria)
            violations = guard_band_violations(measurements, spec.all_criteria, allowances)
            objective = measurements.get(objective_name)
            event["objective"] = objective

            if not verdict["overall_pass"]:
                event["reason"] = f"criteria no longer pass: {verdict['summary']}"
            elif violations:
                # 통과했더라도 여유분을 다 태웠으면 수락하지 않는다. 임계값에
                # 바짝 붙은 채로 멈추면 코너와 모델 변동에서 무너진다.
                event["reason"] = "; ".join(violations)
            elif objective is None:
                event["reason"] = f"objective {objective_name!r} is not among the measurements"
            elif objective >= best_objective:
                event["reason"] = (
                    f"objective {objective:g} is not below the current best {best_objective:g}"
                )
            else:
                event["accepted"] = True

            state.log_event("optimize_step", event)

            if event["accepted"]:
                best_objective = objective
                best_area = area
                accepted_count += 1
                records[_version_index(state, canonical_name)] = {
                    "objective": objective, "area": area,
                }
                continue

            state.rollback()
            rejected_count += 1
            # 한 번 거절된 방향은 그 후보에서 더 밀지 않는다. 같은 노브를 같은
            # 방향으로 계속 미는 것은 방금 얻은 증거를 무시하는 것이고, 보수적인
            # 쪽(후보 소진)이 예산도 아낀다.
            break

    return {"accepted": accepted_count, "rejected": rejected_count, "records": records}


async def run_optimization(netlist_texts: dict[str, str], spec, state, agents: OptimizerAgents) -> dict:
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
    최적화를 돌렸다는 이유로 통과하던 설계가 실패로 끝난다."""
    canonical_name = spec.canonical.name
    start_text = netlist_texts[canonical_name]
    area_before = total_area(start_text).area

    if spec.optimize is None:
        state.log_event("optimize_skipped", {"reason": "spec declares no optimize block"})
        return _result("SKIPPED", state, None, None, area_before, area_before)

    objective_name = spec.optimize.objective

    # state가 인자와 같은 덱을 들고 있는지 맞춘다. 루프는 매 단계 state에서
    # 현재 텍스트를 다시 읽고 거절 시 state.rollback()으로 되돌리므로, 둘이
    # 갈라져 있으면 인자로 받은 덱이 아니라 state의 덱을 조용히 최적화하게
    # 되고, state가 비어 있으면 첫 롤백에서 터진다.
    if state.current_netlist_texts() != netlist_texts:
        state.push_netlist_version(netlist_texts)

    # 기준선 측정. 목적값도 에어리어도 여기서 고정된다.
    sim_result, sim_failure = await _run_simulation(agents.simulate, netlist_texts, spec)
    state.log_event(
        "optimize_baseline",
        {"objective": objective_name, **(sim_result or {"failure": sim_failure})},
    )
    baseline_measurements = sim_result["measurements"] if sim_result else {}
    objective_before = baseline_measurements.get(objective_name)

    if objective_before is None:
        # 목적값을 못 재면(또는 기준선 시뮬레이션 자체가 실패하면) 개선 여부를
        # 판정할 수 없다. 통과한 설계를 그대로 둔다.
        state.log_event(
            "optimize_step",
            {
                "refdes": None, "param": None, "before": None, "after": None,
                "objective": None, "area": area_before, "accepted": False,
                "gate": None,
                "reason": sim_failure
                or f"objective {objective_name!r} is not among the measurements",
            },
        )
        return _result("UNCHANGED", state, None, None, area_before, area_before)

    # 코너를 잴 수단이 있는가. 스펙에 코너가 없거나 스윕 콜러블이 없으면 코너
    # **인식이 없는** 탐색이다 - 비율 여유분을 쓰고, 결과는 확인이 없었다고
    # 말한다(corner_confirmed=False). 검증하지 않은 것을 검증된 것처럼 보고하지
    # 않는다.
    corner_capable = spec.pvt_corners is not None and agents.verify_corners is not None
    anchor_index = _version_index(state, canonical_name)
    entry_sweep = None

    if corner_capable:
        # 진입 스윕. 추가 비용이 아니라 **앵커**다: "실패하면 시작점으로
        # 되돌린다"는 회수 계획은 시작점이 코너를 통과할 때만 안전하다.
        entry_sweep, entry_failure = _run_sweep(
            agents.verify_corners, state.current_netlist_texts()
        )
        state.log_event(
            "optimize_entry_sweep", _sweep_event(entry_sweep, entry_failure, version=anchor_index)
        )
        if entry_sweep is None or not entry_sweep.get("overall_pass"):
            # 코너를 못 버티는 설계에서 마진을 더 깎을 이유가 없다. 되돌아갈
            # 안전한 지점이 아예 없으므로 한 단계도 밟지 않는다.
            return _result(
                "UNCHANGED", state, objective_before, objective_before,
                area_before, area_before, pvt_sweep=entry_sweep,
                corner_failure=entry_failure,
            )
        # 균일한 비율(추측) 대신 이미 값을 치른 스윕에서 기준별 실측 여유분을
        # 읽는다. nominal은 measurement로, 스윕은 기준 이름으로 색인되므로
        # 둘을 잇는 criteria 목록이 반드시 필요하다 - 인자가 셋인 이유다.
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
            **ratio_allowances(spec.all_criteria, spec.optimize.guard_band),
            **corner_allowances(baseline_measurements, entry_sweep, spec.all_criteria),
        }
    else:
        allowances = ratio_allowances(spec.all_criteria, spec.optimize.guard_band)

    outcome = await _search(
        spec, state, agents, canonical_name, start_text, baseline_measurements,
        objective_before, area_before, allowances,
    )
    accepted = outcome["accepted"]
    rejected = outcome["rejected"]
    records = outcome["records"]

    def _final(
        status: str, version: int, accepted_count: int, rejected_count: int, sweep,
        corner_failure: str | None = None,
    ) -> dict:
        record = records[version]
        return _result(
            status, state, objective_before, record["objective"], area_before, record["area"],
            accepted=accepted_count, rejected=rejected_count, pvt_sweep=sweep,
            corner_failure=corner_failure,
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
    state.log_event(
        "optimize_confirm_sweep", _sweep_event(confirm_sweep, confirm_failure, version=end_index)
    )
    if confirm_sweep is not None and confirm_sweep.get("overall_pass"):
        return _final("OPTIMIZED", end_index, accepted, rejected, confirm_sweep)

    # 확인이 실패했다. 다시 탐색하지 않는다 - 어느 단계가 코너를 깼는지는
    # 이미 구간 안에 있고, 통과하는 마지막 지점을 이분 탐색으로 찾는 편이
    # 상한이 있다. 최악이어도 앵커에 착지하므로 시작보다 나빠질 수 없다.
    landed, landed_sweep = _bisect_last_passing(
        state, agents, canonical_name, anchor_index, entry_sweep
    )
    survived = landed - anchor_index
    state.log_event(
        "optimize_bisect_result",
        {"version": landed, "anchor": anchor_index, "end": end_index,
         "steps_kept": survived, "steps_walked_back": accepted - survived},
    )
    # 착지가 앵커면 남은 것이 없다 - 시작 설계를 그대로 돌려준다. 보고하는
    # 수락 수는 **살아남은** 단계 수다: 코너에서 되돌린 단계를 수락으로 세면
    # 결과가 돌려주는 넷리스트를 설명하지 못한다.
    return _final(
        "UNCHANGED" if survived == 0 else "OPTIMIZED",
        landed, survived, rejected + (accepted - survived), landed_sweep,
        # 확인 스윕이 아예 돌지 못했다면(터졌다면) 그 사유도 결과에 실린다 -
        # "코너가 깨져서 되돌아왔다"와 "스윕을 못 돌려서 되돌아왔다"는
        # 다른 사실이고, 후자는 고칠 대상이 회로가 아니다.
        corner_failure=confirm_failure,
    )
