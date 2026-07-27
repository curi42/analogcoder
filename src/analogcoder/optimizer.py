from dataclasses import dataclass
from typing import Callable

from analogcoder.area import total_area
from analogcoder.area_limits import check_area_growth, index_baseline_components, is_count_param
from analogcoder.judge_tools import evaluate_criteria, guard_band_violations, ratio_allowances
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
    # Task 6(코너 확인)이 채운다. 여기서는 쓰지 않지만 미리 자리를 잡아 둔다 -
    # Task 6이 이 dataclass의 시그니처를 바꾸지 않게 하려는 것이다.
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


def _result(
    status: str,
    state,
    objective_before: float | None,
    objective_after: float | None,
    area_before: float,
    area_after: float,
    accepted: int = 0,
    rejected: int = 0,
) -> dict:
    return {
        "status": status,
        "objective_before": objective_before,
        "objective_after": objective_after,
        "area_before": area_before,
        "area_after": area_after,
        "steps_accepted": accepted,
        "steps_rejected": rejected,
        # Task 5는 코너를 모른다. Task 6이 확인 스윕을 얹고 나서야 True가 될
        # 수 있다 - 여기서 True를 내면 확인하지 않은 것을 확인했다고 말하는 것이다.
        "corner_confirmed": False,
        "final_netlist_paths": state.current_netlist_paths(),
    }


async def run_optimization(netlist_texts: dict[str, str], spec, state, agents: OptimizerAgents) -> dict:
    """이미 모든 기준을 통과한 회로의 남은 마진을 목적값에 쓰는 결정론적 탐색.

    기존 루프의 verify_post를 쓰지 않는다. 그쪽 계약은 "나빠졌으면 롤백"인데
    좋은 최적화 단계는 **의도적으로** 마진을 소비하므로, 그 계약을 재사용하면
    성공한 축소마다 롤백이 난다. 수락 규칙은 결정론적이고 LLM이 필요 없다.

    실패(FAIL) 결과가 없다는 점도 의도적이다 - 개선하지 못하면 이미 통과한
    설계를 그대로 돌려준다."""
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

    # **최적화 시작 시점**의 넷리스트로 만든다 - 에어리어 게이트가 여기서 막아야
    # 할 것은 최적화 자신이 만든 성장이다.
    baseline_components = index_baseline_components(start_text)
    allowances = ratio_allowances(spec.all_criteria, spec.optimize.guard_band)

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
                continue

            state.rollback()
            rejected_count += 1
            # 한 번 거절된 방향은 그 후보에서 더 밀지 않는다. 같은 노브를 같은
            # 방향으로 계속 미는 것은 방금 얻은 증거를 무시하는 것이고, 보수적인
            # 쪽(후보 소진)이 예산도 아낀다.
            break

    status = "OPTIMIZED" if accepted_count else "UNCHANGED"
    return _result(
        status, state, objective_before, best_objective, area_before, best_area,
        accepted=accepted_count, rejected=rejected_count,
    )
