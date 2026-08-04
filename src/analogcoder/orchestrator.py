from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.alternatives import Measured, normalize, select
from analogcoder.area_limits import evaluate_area_growth, index_baseline_components
from analogcoder.attempt_log import ATTEMPT_RENDER_LIMIT, Attempt, deltas_between, regressed_between, render_attempts
from analogcoder.checkpoint import LoopProgress, snapshot_progress
from analogcoder.judge_tools import violation_sum
from analogcoder.area import total_area
from analogcoder.control_block import measurement_nets
from analogcoder.netlist import (
    apply_changes,
    apply_topology_swap,
    check_param_applicability,
    check_refdes_resolution,
    check_stimulus_untouched,
    parse_netlist,
    resolve_change_scopes,
)
from analogcoder.patterns import find_patterns
from analogcoder.signal_path import build_signal_paths
from analogcoder.state import RunState
from analogcoder.structure import derive_structure
from analogcoder.structure_view import focus_misses, render_netlist, render_structure, select_focus
from analogcoder.topologies import TOPOLOGY_LIBRARY
from analogcoder.topology_match import SwapCandidate, compatible_swaps, unavailable_reason

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3
TOPOLOGY_SWITCH_THRESHOLD = 3


@dataclass
class OrchestratorAgents:
    simulate: Callable
    judge: Callable
    tune: Callable
    verify_pre: Callable
    verify_post: Callable
    propose_topology: Callable
    # 대안 **선별**용 시뮬레이션. `simulate`와 계약(`(netlist_texts, spec)`)은
    # 같지만 시뮬레이터 **에이전트를 거치지 않는** 것이 존재 이유다: 에이전트의
    # 일은 컨트롤 블록을 수렴·복구하는 것이고, 컨트롤 블록은 테스트벤치의
    # 성질이지 파라미터 값의 성질이 아니다(그래서 `corner_sim`이 한 번 수렴한
    # 것을 45 코너에 재사용한다). 선별에 에이전트를 쓰면 bandgap 기준 iteration
    # 당 시뮬레이터 LLM 호출이 5 -> 15가 되고, 이 저장소는 LLM 지연이 벽시계를
    # 지배한다는 것을 이미 실측했다.
    #
    # `None`이면 `simulate`로 떨어진다 - 그것이 설계 스펙 본문의 동작이고,
    # 배선하지 않은 호출부에서도 정확히 오늘처럼 돈다.
    #
    # **알려진 비대칭:** 코너 축소가 활성일 때 `simulate`는 축소 집합의 최악값을
    # 내지만, 배선된 `screen_simulate`가 그 래퍼를 거치지 않으면 선별은 명목
    # 한 점으로 판정한다. 선별은 **후보를 고르는 일**이지 판정이 아니고, 고른
    # 승자는 아래에서 `simulate`로 다시 재어 판정된다 - 그래서 어긋남의 대가는
    # "덜 좋은 후보를 골랐다"이지 "틀린 판정"이 아니다.
    screen_simulate: Callable | None = None


# `Attempt.outcome`이 가질 수 있는 값 전부. 집계를 **0으로 채워** 내보내기
# 위해 이름이 필요하다 - 일어난 것만 담으면 "0건"과 "집계가 사라졌다"가
# 같은 부재가 된다.
ATTEMPT_OUTCOMES = ("kept", "rolled_back", "rejected")

# `_record_rejected`가 쓰는 사유 코드 전부. 이 다섯은 결정되는 자리에서
# 기록되며 `history.jsonl`에서 되찾을 수 없다 - `area_check`와 `refdes_check`가
# 둘 다 `feedback` 키에 텍스트를 쓰기 때문이다.
REJECTION_REASONS = ("area", "refdes", "param", "stimulus", "verify_pre")


def _attempt_summary(attempts: list[Attempt]) -> dict:
    """이 실행의 제안이 **어떻게 끝났는지**. 항상 실리고, 항상 0으로 채운다.

    이 집계는 `history.jsonl`에만 있었다. 그래서 모든 제안이 면적 게이트에
    막혀 덱이 한 번도 안 바뀐 실행(kept 0 / rejected 30)과 제안이 대부분
    채택된 실행(kept 12 / rolled_back 8 / rejected 6)의 `result.json`이
    구조적으로 **동일**했다. 거짓을 말한 것이 아니라 생략한 것이다.

    **D1의 교훈이 이 자리다.** 그 측정이 무효였던 이유는 기준선 실행에 실패
    이벤트가 0건이라 반복 제안률이 `0.000` 외의 값을 낼 수 없었다는 것이고,
    그 사실은 `history.jsonl`을 따로 파야만 나왔다. 지표를 읽는 사람이 물어야
    하는 질문("이 지표가 다른 답을 낼 조건이 이 실행에 있었는가")에 **실행
    자신이** 답할 수 있어야 한다.

    `rejected_by_reason`의 합은 `by_outcome["rejected"]`와 같다 - 게이트는
    제안 **전체**를 거부하므로 변경 하나마다 항목이 하나다.
    """
    by_outcome = {name: 0 for name in ATTEMPT_OUTCOMES}
    by_reason = {name: 0 for name in REJECTION_REASONS}
    for attempt in attempts:
        by_outcome[attempt.outcome] = by_outcome.get(attempt.outcome, 0) + 1
        if attempt.outcome == "rejected" and attempt.reason:
            by_reason[attempt.reason] = by_reason.get(attempt.reason, 0) + 1
    return {"changes": len(attempts), "by_outcome": by_outcome, "rejected_by_reason": by_reason}


def _retry_reason_counts(history: list[Attempt], outer_iter: int) -> dict[str, int]:
    """이 outer iteration에서 재시도가 **사유별로 몇 번** 소진됐는지.

    항목을 그냥 세면 안 된다 - `_record_rejected`는 제안 하나당 **변경
    개수만큼** `Attempt`를 넣으므로, 변경 셋짜리 제안 하나가 거부되면 3으로
    나온다. 세어야 하는 것은 소진된 재시도이므로 서로 다른 `(retry, reason)`
    쌍을 센다. 각 재시도는 승인(break) 아니면 거부 하나(continue)이므로 이
    합은 정확히 실패한 재시도 수와 같다.

    `tuning_history`는 실행 전체에 걸쳐 쌓이므로 **이번 outer_iter로 반드시
    필터**한다. 다섯 키는 0이어도 전부 실린다 - 키의 부재와 0이 같아지면
    "이 사유는 없었다"와 "이 사유가 사라졌다"가 구별되지 않는다.
    """
    exhausted = {
        (attempt.retry, attempt.reason)
        for attempt in history
        if attempt.outer_iter == outer_iter and attempt.outcome == "rejected" and attempt.reason
    }
    counts = {name: 0 for name in REJECTION_REASONS}
    for _retry, reason in exhausted:
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _final_result(
    status: str,
    state: RunState,
    iterations_used: int,
    judge_result: dict | None,
    failure_reason: str | None = None,
    topology_swaps: list[dict] | None = None,
    tuning_history: list[Attempt] | None = None,
) -> dict:
    """`topology_swaps`는 **항상** 실린다(비었으면 빈 목록). 결과는 자기가
    돌려주는 덱을 설명해야 한다 - 실측 실행에서 `BUF_P`의 16소자 본문이 통째로
    교체됐는데도 `result.json`과 `report.md` 어디에도 그 사실이 없었다. 이
    저장소가 최적화 단계에서 이미 같은 값을 치른 모양이다("212.99 µA 옆에
    212.25 µA를 재는 넷리스트"). 키를 조건부로 넣지 않는 이유도 같다:
    "스왑이 없었다"와 "기록이 사라졌다"가 같은 부재로 보이면 안 된다.
    리포트 쪽에서 빈 목록이면 섹션을 그리지 않는다."""
    result = {
        "status": status,
        "final_netlist_paths": state.current_netlist_paths(),
        "run_dir": state.run_dir,
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"] if judge_result else [],
        "topology_swaps": list(topology_swaps or []),
        "attempt_summary": _attempt_summary(list(tuning_history or [])),
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


def _apply_to_all(netlist_texts: dict[str, str], changes: list[dict]) -> dict[str, str]:
    return {name: apply_changes(text, changes) for name, text in netlist_texts.items()}


def _outcome_counts(attempts: list[Attempt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.outcome] = counts.get(attempt.outcome, 0) + 1
    return counts


def _hard_gates(
    netlist_text: str, changes: list[dict], log: Callable
) -> tuple[bool, str | None, str | None]:
    """강등되지 **않은** 세 게이트를 순서대로 돌린다.

    이 셋은 "정당한가"가 아니라 **"적용 자체가 되는가"**를 묻는다 - 없는
    refdes에 변경을 적용하면 에러 없이 아무 일도 일어나지 않는다. 그래서
    면적 게이트와 달리 강등 대상이 아니다.

    반환은 `(통과 여부, 사유 코드, 피드백)`. 통과면 뒤의 둘은 `None`이다.

    **세 게이트는 각각 자기 이벤트를 낸다.** 하나로 뭉치면 `refdes_check`와
    `param_check`가 로그에서 구별되지 않고, 사유 코드는 결정되는 자리에서
    기록되며 `history.jsonl`에서 되찾을 수 없다(둘 다 `feedback` 키를 쓴다).
    첫 실패에서 멈추는 것도 오늘 그대로다 - 뒤의 게이트는 돌지도 로그되지도
    않는다.

    원본 전문을 넘긴다 - 이 게이트들은 초점과 무관하게 판정해야 한다."""
    for reason, check in (
        ("refdes", check_refdes_resolution),
        ("param", check_param_applicability),
        ("stimulus", check_stimulus_untouched),
    ):
        ok, feedback = check(netlist_text, changes)
        log(f"{reason}_check", ok, feedback)
        if not ok:
            return False, reason, feedback
    return True, None, None


def _alternatives_event(
    outer_iter: int,
    retry: int,
    alts: list,
    dropped_over_cap: int,
    surviving: list,
    approved: list,
    selection,
) -> dict:
    """`tuning_alternatives` 이벤트. **매 재시도, 대안이 1개일 때도** 나간다.

    이 저장소의 첫 번째 상비 질문이 "이 게이트가 아무것도 안 할 때 로그가
    어떻게 보이는가"다. 대안 정렬의 존재 이유는 **여러 대안이 다 통과할 때
    면적으로 착지점을 고르는 것**이므로, 그 분기가 한 번도 발화하지 않으면
    면적 정렬은 무력하고 정직한 결론은 되돌리는 것이다. 그것을 보려면
    발화 0도 기록돼야 한다.

    `screened`가 분모다: 후보가 하나뿐이라 고를 것이 없었던 재시도와, 골랐는데
    통과가 1개 이하였던 재시도는 다른 사실이다. 둘을 가르지 않으면 "발화 0"이
    "기회가 없었다"인지 "기회가 있었는데 안 걸렸다"인지 알 수 없다."""
    screened = selection is not None
    return {
        "outer_iter": outer_iter,
        "retry": retry,
        "offered": len(alts),
        "dropped_over_cap": dropped_over_cap,
        "survived_gates": len(surviving),
        "survived_verify_pre": len(approved),
        "screened": screened,
        "simulated": len(selection.candidates) if screened else 0,
        "passing_count": selection.passing_count if screened else None,
        "rule": selection.rule if screened else None,
        "winner": selection.winner.index if screened else (approved[0].index if approved else None),
        "winner_source": (
            selection.winner.source if screened else (approved[0].source if approved else None)
        ),
        # 무조건 싣는다. 0이면 튜너 단계의 면적 정렬은 무력하다.
        "multi_pass_branch_fired": bool(screened and selection.passing_count >= 2),
    }


def _record_rejected(
    history: list[Attempt], outer_iter: int, retry: int, proposal: dict, reason: str, detail: str
) -> None:
    """게이트는 제안 **전체**를 거부하므로 모든 변경이 같은 사유로 항목이 된다.

    어느 변경이 게이트를 촉발했는지는 게이트가 알려 주지 않으므로 추측하지
    않는다 - detail에 게이트가 낸 피드백이 그대로 들어가고, 그 문자열이 보통
    refdes를 이름으로 담고 있다.
    """
    for change in proposal["proposed_changes"]:
        history.append(
            Attempt(
                outer_iter=outer_iter,
                retry=retry,
                refdes=change["refdes"],
                param=change["param"],
                old_value=change["old_value"],
                new_value=change["new_value"],
                outcome="rejected",
                reason=reason,
                detail=detail,
            )
        )


def _candidate_pairs(candidates: list[SwapCandidate]) -> list[tuple[str, str]]:
    return sorted((c.block_path, c.topology_id) for c in candidates)


def _component_signature(component) -> tuple:
    """Structural identity used to tell "still describes the same device" from
    "same refdes, different device" - deliberately excludes refdes itself
    (the caller already matched on that) and any derived/decorative field
    (scope, raw_line, geometry_scale), mirroring topology_match._component_key."""
    return (
        component.ctype,
        component.value,
        tuple(component.nodes),
        tuple(sorted(component.params.items())),
    )


def _resolve_swap_target(
    proposal: dict, candidates: list[SwapCandidate]
) -> tuple[SwapCandidate | None, str | None]:
    """Resolve the tuner's (topology_id, optional block_path) response against
    the candidate list the orchestrator actually offered it - never against
    the full library, and never by guessing a block the deck may not even
    have when there is more than one plausible one.

    - block_path present: it must name an exact (block_path, topology_id)
      pair already in `candidates`.
    - block_path omitted: resolves only when exactly one candidate carries
      that topology_id. Zero or more than one is ambiguous and must be
      retried with feedback listing every candidate pair, not guessed at.
    """
    topology_id = proposal["topology_id"]
    block_path = proposal.get("block_path") or None
    pairs = _candidate_pairs(candidates)

    if block_path is not None:
        for candidate in candidates:
            if candidate.block_path == block_path and candidate.topology_id == topology_id:
                return candidate, None
        return None, (
            f"'{block_path}'/'{topology_id}' is not an available (block_path, topology_id) "
            f"candidate. Choose one of: {pairs}"
        )

    matches = [c for c in candidates if c.topology_id == topology_id]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"'{topology_id}' is not an available candidate topology. Choose one of: {pairs}"
    return None, (
        f"'{topology_id}' matches more than one candidate block "
        f"({sorted(c.block_path for c in matches)}); specify block_path. Choose one of: {pairs}"
    )


async def run_orchestration(
    initial_netlist_texts: dict[str, str],
    spec,
    state: RunState,
    agents: OrchestratorAgents,
    resume: LoopProgress | None = None,
    save_checkpoint: Callable[[LoopProgress], None] | None = None,
) -> dict:
    """`resume`이 있으면 그 outer iteration **시작**부터 이어 돈다.

    이터레이션 중간 재개는 범위 밖이다 - LLM 호출을 리플레이해야 하고 그건 새
    정합성 문제를 만든다. 경계 재개의 최악은 이터레이션 하나를 다시 도는
    것이고, 그건 받아들일 수 있는 값이다. 그래서 `save_checkpoint`는 루프 **머리**
    에서만 불린다: 그 지점의 상태만이 "이 이터레이션은 아직 아무 일도 하지
    않았다"를 뜻한다.

    `resume` 경로에서는 `push_netlist_version`을 **다시 하지 않는다** - v0가
    중복 push되어 버전 번호가 어긋나고, 그러면 중단 없이 돈 실행과 같은 덱을
    돌려주더라도 경로가 달라진다.
    """
    canonical_name = spec.canonical.name
    if resume is None:
        entry_netlist_paths = state.push_netlist_version(initial_netlist_texts)
    else:
        entry_netlist_paths = dict(resume.entry_netlist_paths)
    outer_iter = 0
    judge_result: dict = dict(resume.judge_result) if resume else {}
    # try 밖에서 초기화한다 - 세 except 절이 이것을 읽는데, try 안에서 처음
    # 대입하면 첫 줄에서 터진 실행이 NameError로 바뀐다.
    #
    # `tuning_history`가 같은 이유로 여기 있다. 예전에는 try 안,
    # `index_baseline_components` **뒤에** 있었는데 그 호출은 덱을 파싱하므로
    # `ValueError`를 낼 수 있다 - 즉 `except ValueError` 절이 잡아야 할 바로
    # 그 경로에서 `tuning_history`가 아직 없었다. 가정이 아니라 도달 가능한
    # 줄 순서다.
    topology_swaps: list[dict] = [dict(s) for s in resume.topology_swaps] if resume else []
    tuning_history: list[Attempt] = list(resume.tuning_history) if resume else []

    try:
        # No structural precondition on the deck (no more len(subckts) == 1
        # gate) - every iteration compatible_swaps() decides, from parsed
        # facts (ports/models/scale/no-op), which (block, topology) pairs are
        # actually safe to offer. A pair is a tuple, not a bare topology id,
        # because the same topology can legitimately be tried again against a
        # different block once the first attempt is tried-and-exhausted.
        tried_topologies: set[tuple[str, str]] = set(resume.tried_topologies) if resume else set()
        consecutive_rollbacks = resume.consecutive_rollbacks if resume else 0
        # Intentionally computed once from netlist_v0 and never refreshed after a
        # topology swap: components introduced by a swapped-in topology (e.g. a
        # nulling resistor Rz) have nothing in the original netlist to compare
        # against, so they are simply unconstrained by the area gate for the
        # rest of the run. This is by-design, not a bug - do not "fix" it.
        baseline_components = index_baseline_components(initial_netlist_texts[canonical_name])

        # criterion 이름 -> measurement 이름 -> 그 measurement가 보는 넷 집합,
        # 두 단계로 실패 넷을 찾기 위한 매핑. spec의 criteria/control_block에서만
        # 나오고 iteration마다 바뀌지 않으므로 루프 밖에서 한 번만 만든다.
        # criterion은 넷이 아니라 measurement를 참조하므로(예: "gain")
        # control_block의 meas/let에서만 그 measurement가 실제로 보는 넷을 알
        # 수 있다. measurement_nets가 넷이 아니라 전압원 이름을 낼 수도 있는데
        # (idd -> {"Vdd"}), net_blocks에는 그 이름이 없어 그냥 씨앗을 하나 안
        # 내는 것으로 끝난다 - 별도 처리가 필요 없다.
        measurement_by_criterion = {
            c.name: c.measurement for tb in spec.testbenches for c in tb.criteria
        }
        # 테스트벤치를 가로질러 **합집합**으로 병합한다. dict.update로 덮어쓰면
        # 두 테스트벤치가 같은 measurement 이름을 정의할 때(PSR 테스트벤치들이
        # 실제로 이름을 재사용한다) 앞선 것이 보던 넷이 조용히 사라져, 초점이
        # 마지막 테스트벤치가 본 블록만 가리킨다.
        nets_by_measurement: dict[str, set[str]] = {}
        for tb in spec.testbenches:
            for name, nets in measurement_nets(tb.control_block).items():
                nets_by_measurement.setdefault(name, set()).update(nets)

        start_iter = resume.outer_iter if resume else 1
        for outer_iter in range(start_iter, MAX_OUTER_ITERATIONS + 1):
            # **경계는 여기 하나다.** 이 지점에서 이 이터레이션은 아직 아무
            # 일도 하지 않았으므로, 여기 담긴 상태로 재개하면 최악이 이
            # 이터레이션을 통째로 다시 도는 것이다. 아래 어디에서 죽든 되돌아올
            # 자리가 이 지점이라는 것이 재개 설계 전체의 잠긴 전제다.
            if save_checkpoint is not None:
                save_checkpoint(
                    snapshot_progress(
                        LoopProgress(
                            outer_iter=outer_iter,
                            entry_netlist_paths=entry_netlist_paths,
                            tried_topologies=tried_topologies,
                            consecutive_rollbacks=consecutive_rollbacks,
                            tuning_history=tuning_history,
                            topology_swaps=topology_swaps,
                            judge_result=judge_result,
                        )
                    )
                )

            netlist_texts = state.current_netlist_texts()

            # 파생은 결정론적 파이썬이므로 매 iteration 다시 계산해도 비용이
            # 없다. analyzer가 LLM 호출이었을 때와 달리 캐시할 이유가 없다 -
            # 토폴로지 스왑 직후에도 다음 iteration이 자연히 새 넷리스트에서
            # 다시 파생한다.
            structure = derive_structure(netlist_texts[canonical_name], spec.circuit_name)
            paths = build_signal_paths(structure)

            sim_result = await agents.simulate(netlist_texts, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

            judge_result = await agents.judge(sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, **judge_result})

            if judge_result["overall_pass"]:
                return _final_result(
                    "PASS", state, outer_iter, judge_result, topology_swaps=topology_swaps, tuning_history=tuning_history
                )

            failing_nets: set[str] = set()
            for criterion in judge_result["criteria"]:
                if criterion["pass"]:
                    continue
                measurement = measurement_by_criterion.get(criterion["name"])
                failing_nets |= nets_by_measurement.get(measurement, set())

            # 거부된 시도의 refdes도 들어간다 - 튜너에게 "이 블록에서
            # 거부당했다"고 말하면서 그 블록을 접어서 보여 줄 수는 없다.
            touched_refdes = {attempt.refdes for attempt in tuning_history}
            focus = select_focus(
                structure, paths, failing_nets, touched_refdes, netlist_texts[canonical_name]
            )
            structure_view = render_structure(structure, paths, find_patterns(structure), focus)
            netlist_view = render_netlist(netlist_texts[canonical_name], focus)
            state.log_event(
                "focus",
                {
                    "outer_iter": outer_iter,
                    "blocks": sorted(focus),
                    "netlist_chars": len(netlist_view),
                    "netlist_chars_full": len(netlist_texts[canonical_name]),
                },
            )

            if consecutive_rollbacks >= TOPOLOGY_SWITCH_THRESHOLD:
                candidates, rejections = compatible_swaps(netlist_texts, TOPOLOGY_LIBRARY, tried_topologies)
                # Unconditional - approved, rejected, or never attempted. A
                # gate that only logs on the interesting outcome is how this
                # repo's other three area-gate silences went unnoticed for a
                # whole run; this event exists so "0 candidates" and "we
                # forgot to check" are never the same line of history.jsonl.
                state.log_event(
                    "topology_candidates",
                    {
                        "outer_iter": outer_iter,
                        "candidates": [
                            {"block_path": c.block_path, "topology_id": c.topology_id} for c in candidates
                        ],
                        "rejections": [
                            {
                                "block_path": r.block_path,
                                "topology_id": r.topology_id,
                                "reason": r.reason,
                                "detail": r.detail,
                            }
                            for r in rejections
                        ],
                    },
                )

                if not candidates:
                    # Today's "library exhausted" behavior, generalised: no
                    # applicable (block, topology) pair exists (or all have
                    # already been tried), so stay in parameter-tuning mode
                    # this iteration rather than dead-ending the run.
                    # 사유 코드가 함께 나가야 한다 - `.subckt` 정의가 아예 없는
                    # 덱과 라이브러리를 정말로 소진한 실행이 같은 한 줄을 내면
                    # "검사했고 후보가 없음"과 "검사가 사라짐"이 구별되지 않는다.
                    state.log_event(
                        "topology_unavailable",
                        {
                            "outer_iter": outer_iter,
                            "reason": unavailable_reason(netlist_texts, TOPOLOGY_LIBRARY, rejections),
                        },
                    )
                else:
                    resolved = None
                    rejection_feedback = None
                    for retry in range(1, MAX_TUNING_RETRIES + 1):
                        proposal = await agents.propose_topology(
                            structure_view, judge_result, candidates, TOPOLOGY_LIBRARY, rejection_feedback
                        )
                        state.log_event(
                            "topology_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal}
                        )

                        resolved, feedback = _resolve_swap_target(proposal, candidates)
                        if resolved is not None:
                            break
                        rejection_feedback = feedback

                    if resolved is None:
                        # **에스컬레이션 시도의 실패가 에스컬레이션하지 않은
                        # 것보다 나빠서는 안 된다.** 예전에는 여기서 런 전체를
                        # FAIL로 끝냈는데, 그것은 남은 outer iteration과 아직
                        # 살아 있는 파라미터 튜닝 경로를 통째로 버리는 것이었다.
                        # 실측(bandgap 시드 덱): block_path를 넣으면 iteration
                        # 4에서 PASS(buf0_gain_db 100.158), 빼면 같은 iteration
                        # 4에서 FAIL하고 덱이 netlist_v0(73.515)로 되돌아갔다 -
                        # 스키마가 block_path를 required로 두지 않으므로(약한
                        # 모델 보호) 생략은 언제든 일어날 수 있고, 후보 블록이
                        # 여럿인 덱(이 브랜치가 존재하는 이유인 바로 그 덱)에서
                        # 생략은 **항상** 모호하다.
                        #
                        # 그래서 결정론적 게이트 소진은 파라미터 튜닝 롤백처럼
                        # 다룬다 - area 게이트가 이미 세운 선례이며, 파라미터
                        # 경로도 LLM verifier가 거부했을 때(verify_pre_rejected_any)
                        # 에만 하드 FAIL한다.
                        state.log_event(
                            "topology_unavailable",
                            {
                                "outer_iter": outer_iter,
                                "reason": "proposal_unresolved",
                                "detail": rejection_feedback,
                            },
                        )
                        # 리셋하지 않으면 카운터가 임계값 위에 머물러 이후 모든
                        # iteration이 다시 3번의 토폴로지 LLM 호출을 태운다.
                        # 스왑 iteration은 유지되든 롤백되든 카운터를 리셋한다는
                        # 기존 규칙과도 같다.
                        consecutive_rollbacks = 0
                    else:
                        tried_topologies.add((resolved.block_path, resolved.topology_id))
                        topology = TOPOLOGY_LIBRARY[resolved.topology_id]
                        # Applied to every deck that defines the block, not just
                        # canonical - compatible_swaps' missing_in_testbench rule
                        # guarantees a genuine candidate is defined in all of
                        # them, and push_netlist_version versions every testbench
                        # atomically, so a partial swap across testbenches would
                        # be an inconsistent state no rollback could describe.
                        new_netlist_texts = {
                            name: apply_topology_swap(text, resolved.block_path, topology.subckt_body)
                            for name, text in netlist_texts.items()
                        }

                        swapped_block = parse_netlist(new_netlist_texts[canonical_name]).subckts[resolved.block_path]
                        # Among the swapped-in block's own components, split by
                        # what the frozen baseline (netlist_v0) can still say
                        # about them - fully-qualified "<block_path>.<refdes>"
                        # keys in both lists, since a bare refdes is ambiguous the
                        # moment a deck has more than one amp (this repo's
                        # bandgap benchmark always does).
                        #
                        # unconstrained: no baseline entry at all - a later
                        # area-growth check has nothing to compare against, so
                        # this refdes is simply unbound for the rest of the run.
                        #
                        # stale_baseline_refdes: a baseline entry exists (so a
                        # later check will still run one), but its geometry
                        # belongs to a component this refdes no longer is - the
                        # component parameters differ from what actually got
                        # swapped in. Reporting only "unconstrained" reads as "the
                        # gate is intact everywhere else", when a stale entry
                        # tiers a growth proposal against the PREVIOUS topology's
                        # geometry, not the current one. This is logging only -
                        # the area gate itself, and the choice to never refresh
                        # the baseline, are unchanged; see the baseline_components
                        # comment above.
                        unconstrained_refdes = []
                        stale_baseline_refdes = []
                        for component in swapped_block.components:
                            key = f"{resolved.block_path}.{component.refdes}"
                            baseline_component = baseline_components.get(key)
                            if baseline_component is None:
                                unconstrained_refdes.append(key)
                            elif _component_signature(baseline_component) != _component_signature(component):
                                stale_baseline_refdes.append(key)
                        unconstrained_refdes.sort()
                        stale_baseline_refdes.sort()
                        state.log_event(
                            "topology_swap",
                            {
                                "outer_iter": outer_iter,
                                "block_path": resolved.block_path,
                                "topology_id": resolved.topology_id,
                                # 이 항목이 무엇으로 검증됐는지가 로그에 없으면
                                # `verified_at="nominal"`인 항목을 스왑해 넣은
                                # 실행과 코너 검증된 항목을 넣은 실행이
                                # history.jsonl에서 구별되지 않는다.
                                "provenance": topology.provenance,
                                "verified_at": topology.verified_at,
                                "unconstrained_refdes": unconstrained_refdes,
                                "stale_baseline_refdes": stale_baseline_refdes,
                            },
                        )
                        # history.jsonl에만 남기면 최종 산출물(result.json /
                        # report.md)은 자기가 돌려주는 덱을 설명하지 못한다.
                        # 결과에는 목록이 아니라 **개수**를 싣는다 - 전문은
                        # 이미 위 이벤트에 있고, 리포트가 필요한 것은 "면적
                        # 게이트가 몇 개를 더 이상 묶지 못하는가"다.
                        swap_record = {
                            "outer_iter": outer_iter,
                            "block_path": resolved.block_path,
                            "topology_id": resolved.topology_id,
                            "provenance": topology.provenance,
                            "verified_at": topology.verified_at,
                            "unconstrained_refdes": len(unconstrained_refdes),
                            "stale_baseline_refdes": len(stale_baseline_refdes),
                            "outcome": None,
                        }
                        topology_swaps.append(swap_record)

                        state.push_netlist_version(new_netlist_texts)

                        new_sim_result = await agents.simulate(new_netlist_texts, spec)
                        state.log_event(
                            "simulation", {"outer_iter": outer_iter, "post_topology_swap": True, **new_sim_result}
                        )

                        new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
                        state.log_event(
                            "judge", {"outer_iter": outer_iter, "post_topology_swap": True, **new_judge_result}
                        )

                        # block_path를 함께 넘긴다. 유지/롤백 판정 자체는
                        # before/after judge 결과로 결정되므로 정확성 문제는
                        # 아니지만, verifier의 자유 서술 feedback이
                        # history.jsonl에 남는 사람이 읽는 기록이다 -
                        # bandgap처럼 앰프가 넷인 덱에서 "swapped
                        # folded_cascode_pmos_in_cs"는 어느 블록인지 말하지
                        # 않는다.
                        post_review = await agents.verify_post(
                            judge_result,
                            new_judge_result,
                            [{
                                "topology_id": resolved.topology_id,
                                "block_path": resolved.block_path,
                            }],
                        )
                        state.log_event(
                            "verify_post", {"outer_iter": outer_iter, "topology_swap": True, **post_review}
                        )

                        consecutive_rollbacks = 0

                        if post_review["recommendation"] == "rollback":
                            swap_record["outcome"] = "rolled_back"
                            state.rollback()
                            judge_result = new_judge_result
                            continue

                        swap_record["outcome"] = "kept"

                        if new_judge_result["overall_pass"]:
                            return _final_result(
                                "PASS", state, outer_iter, new_judge_result, topology_swaps=topology_swaps, tuning_history=tuning_history
                            )

                        judge_result = new_judge_result
                        continue

            approved_proposal = None
            approved_retry = 0
            rejection_feedback = None
            verify_pre_rejected_any = False
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                attempts_view = render_attempts(tuning_history)
                rendered = len(tuning_history[-ATTEMPT_RENDER_LIMIT:])
                # 무조건 남긴다. 항목이 0건인 iteration에도 남겨야
                # "기록했고 0건"과 "기록이 사라졌다"가 구별된다.
                state.log_event(
                    "attempt_log",
                    {
                        "outer_iter": outer_iter,
                        "retry": retry,
                        "total": len(tuning_history),
                        "by_outcome": _outcome_counts(tuning_history),
                        "rendered": rendered,
                        "dropped": len(tuning_history) - rendered,
                    },
                )
                proposal = await agents.tune(
                    structure_view, judge_result, attempts_view, rejection_feedback, netlist_view
                )
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                # 튜너는 1차 제안 + 최대 2개의 대안을 낸다. `alternatives`가
                # 없으면 목록은 1개이고 아래 경로는 오늘과 **동작이 같다**.
                alts, dropped_over_cap = normalize(proposal)
                canonical_text = netlist_texts[canonical_name]

                # --- 게이트: 대안별로 돌고, 걸린 것만 버린다 -----------------
                # 오늘은 게이트에 걸린 제안 하나가 재시도를 통째로 태운다.
                surviving: list = []
                feedbacks: list[str] = []
                for alt in alts:
                    area = evaluate_area_growth(baseline_components, alt.changes)
                    state.log_event(
                        "area_check",
                        {
                            "outer_iter": outer_iter,
                            "retry": retry,
                            "alternative": alt.index,
                            # 계산 결과는 그대로다. 바뀐 것은 그 결과로 무엇을
                            # 하는가뿐이다 - `evaluate_area_growth`는 한 줄도
                            # 건드리지 않았고, 최적화 단계와 큐레이션이 쓰는
                            # 같은 함수의 의미가 함께 움직이면 안 되기 때문이다.
                            "approved": area.approved,
                            "feedback": area.feedback,
                            "states": area.states,
                            # **무조건** 싣는다. 키의 부재와 `false`가 구별되어야
                            # "강등됐다"와 "이 계측이 사라졌다"가 갈린다.
                            "blocking": False,
                        },
                    )
                    # 면적 게이트는 **거부하지 않는다**(2026-08-05 강등). 상한
                    # 숫자에 근거가 없다는 것이 이유이고, 성장은 통과 직후 면적
                    # 최소화 단계가 되돌린다. 계산·기록·통보는 전부 남는다 -
                    # 게이트를 지우면 성장이 보이지 않게 되고, 그것은 조용히
                    # 무력한 게이트의 반대 방향 실수다.
                    #
                    # `REJECTION_REASONS`에서 `"area"`는 **지우지 않는다**: 과거
                    # 실행의 `history.jsonl`이 그 코드를 싣고 있고 `attempt_log`
                    # 렌더가 그것을 읽는다. 새 실행에서 0이 되는 것과 키가
                    # 사라지는 것은 다른 사실이다.

                    def _log_gate(step, ok, feedback, _alt=alt):
                        state.log_event(
                            step,
                            {
                                "outer_iter": outer_iter,
                                "retry": retry,
                                "alternative": _alt.index,
                                "approved": ok,
                                "feedback": feedback,
                            },
                        )

                    gate_ok, gate_reason, gate_feedback = _hard_gates(
                        canonical_text, alt.changes, _log_gate
                    )
                    if gate_ok:
                        surviving.append(alt)
                        continue
                    feedbacks.append(gate_feedback or "")
                    _record_rejected(
                        tuning_history, outer_iter, retry, alt.as_proposal(),
                        gate_reason, gate_feedback or "",
                    )

                if not surviving:
                    rejection_feedback = "\n".join(f for f in feedbacks if f)
                    state.log_event(
                        "tuning_alternatives",
                        _alternatives_event(outer_iter, retry, alts, dropped_over_cap,
                                            surviving, [], None),
                    )
                    continue

                # --- verify_pre: 재기 **전에**, 살아남은 전부에 -------------
                # "시뮬 먼저 하고 이긴 것만 verify_pre"는 싸 보이지만 위험하다.
                # `Vin`의 AC 진폭을 100으로 바꾸면 회로를 하나도 안 바꾸고
                # `gain_db`가 20 -> 60으로 뛰고, `Cload`를 줄이면 위상여유와
                # UGBW가 함께 좋아진다. **측정값으로 고르면 이런 치팅이 1등을
                # 한다.** 그것을 막는 것이 verify_pre이므로 재기 전에 있어야 한다.
                approved_alts: list = []
                for alt in surviving:
                    misses = focus_misses(focus, alt.changes, canonical_text)
                    if misses:
                        # 기록만 하고 흐름은 막지 않는다 - 초점이 틀렸다는
                        # 증거이지 제안이 틀렸다는 증거가 아니다.
                        state.log_event(
                            "focus_miss",
                            {"outer_iter": outer_iter, "retry": retry,
                             "alternative": alt.index, "refdes": misses},
                        )
                    # verify_pre에는 초점을 제안이 실제로 지목한 블록까지 넓혀
                    # 넘긴다. 원래 초점만 넘기면, 비초점 서브회로는 본문이 접혀
                    # "* ... (N components elided)"로만 보이는데도 verify_pre의
                    # 프롬프트는 "넷리스트 위의 컴포넌트 줄에 없는 refdes/param은
                    # 거부하라"고 지시한다 - 접힌 블록을 지목한, 게이트를 이미
                    # 통과한 정상적인 제안이 그 문장 그대로 거부 대상처럼 보이게
                    # 되어 verify_pre가 초점 오류를 튜너의 잘못으로 오판한다.
                    resolved_blocks = resolve_change_scopes(canonical_text, alt.changes)
                    verify_netlist_view = render_netlist(canonical_text, focus | resolved_blocks)
                    review = await agents.verify_pre(
                        structure_view, judge_result, alt.as_proposal(), verify_netlist_view
                    )
                    state.log_event(
                        "verify_pre",
                        {"outer_iter": outer_iter, "retry": retry,
                         "alternative": alt.index, **review},
                    )
                    if review["approved"]:
                        approved_alts.append(alt)
                        continue
                    verify_pre_rejected_any = True
                    feedbacks.append(review["feedback"])
                    _record_rejected(
                        tuning_history, outer_iter, retry, alt.as_proposal(),
                        "verify_pre", review["feedback"],
                    )

                if not approved_alts:
                    rejection_feedback = "\n".join(f for f in feedbacks if f)
                    state.log_event(
                        "tuning_alternatives",
                        _alternatives_event(outer_iter, retry, alts, dropped_over_cap,
                                            surviving, approved_alts, None),
                    )
                    continue

                # --- 선별 ---------------------------------------------------
                # **후보가 하나면 재지 않는다.** 고를 것이 없는데 재면 오늘
                # 1회인 시뮬레이션이 2회가 된다 - `alternatives`가 없을 때
                # 오늘 동작과 바이트 동일해야 한다는 제약이 이 분기다.
                selection = None
                if len(approved_alts) > 1:
                    screen = agents.screen_simulate or agents.simulate
                    measured: list[Measured] = []
                    for alt in approved_alts:
                        candidate_texts = _apply_to_all(netlist_texts, alt.changes)
                        candidate_sim = await screen(candidate_texts, spec)
                        # **새 판정 경로를 만들지 않는다** - 루프가 오늘 쓰는
                        # `agents.judge`를 그대로 부른다.
                        candidate_judge = await agents.judge(candidate_sim["measurements"], spec)
                        vs = violation_sum(
                            spec.all_criteria,
                            sim_result["measurements"],
                            candidate_sim["measurements"],
                        )
                        area_total = total_area(candidate_texts[canonical_name])
                        measured.append(
                            Measured(
                                alt=alt,
                                passed=candidate_judge["overall_pass"],
                                # `counted == 0`은 면적 0이 아니라 "못 쟀다"다.
                                area_after=None if area_total.counted == 0 else area_total.area,
                                improvement=vs.improvement,
                            )
                        )
                    selection = select(measured)
                    winner = selection.winner
                else:
                    winner = approved_alts[0]

                state.log_event(
                    "tuning_alternatives",
                    _alternatives_event(outer_iter, retry, alts, dropped_over_cap,
                                        surviving, approved_alts, selection),
                )

                approved_proposal = winner.as_proposal()
                approved_retry = retry
                break

            # **계측만이다 - 아래 분기의 동작은 한 줄도 바뀌지 않았다.**
            #
            # 그 하드 FAIL은 의도된 비대칭이고(토폴로지 경로가 이 규칙을 거울로
            # 삼아 설계됐다) 그대로 둔다. 문제는 그 분기가 **한 번도 발화한 적이
            # 없다는 것**이다 - 기록된 15개 런 중 0건. 발화해야만 존재를 알 수
            # 있고, 얼마나 근접했는지는 로그에 전혀 안 남았다. 실측으로 한
            # 이터레이션 최대 실패는 2회(임계값 3)이니 여유는 **1회**였고,
            # 실패가 있었던 8개 이터레이션 중 6개가 마지막 재시도까지 갔으며
            # 그중 5개는 이미 verify_pre 거부를 안고 있었다 - r3마저 실패했으면
            # 에스컬레이션이 아니라 하드 FAIL이었다.
            #
            # **승인됐을 때도 무조건 남긴다.** 소진된 이터레이션만 남기면
            # "예산을 안 썼다"와 "계측이 사라졌다"가 같은 부재가 되고, 여유가
            # 실행들에 걸쳐 줄고 있는지를 볼 수 없다. `outcome`의 세 값은 아래
            # 세 갈래와 1:1이라 분기를 로그에서 그대로 읽는다.
            #
            # `failures`는 `retry - 1`로 정확히 구한다 - 각 재시도는 승인(break)
            # 아니면 실패(continue)이므로, 다섯 거부 자리를 건드릴 필요가 없다.
            approved = approved_proposal is not None
            failures = (approved_retry - 1) if approved else MAX_TUNING_RETRIES
            if approved:
                retry_outcome = "approved"
            elif verify_pre_rejected_any:
                retry_outcome = "exhausted_hard_fail"
            else:
                retry_outcome = "exhausted_escalate"
            state.log_event(
                "tuning_retries",
                {
                    "outer_iter": outer_iter,
                    "outcome": retry_outcome,
                    "approved_retry": approved_retry if approved else None,
                    "failures": failures,
                    "max_retries": MAX_TUNING_RETRIES,
                    "headroom": MAX_TUNING_RETRIES - failures,
                    # 소진되지 않았을 때도 싣는다 - 이 불리언 하나가 소진 시
                    # 하드 FAIL이냐 에스컬레이션이냐를 가르므로, 여유가 줄고
                    # 있는 이터레이션을 읽는 사람은 어느 쪽으로 떨어질지도
                    # 알아야 한다.
                    "verify_pre_rejected": verify_pre_rejected_any,
                    "by_reason": _retry_reason_counts(tuning_history, outer_iter),
                },
            )

            if approved_proposal is None:
                if verify_pre_rejected_any:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="tuning proposal repeatedly rejected",
                        topology_swaps=topology_swaps, tuning_history=tuning_history,
                    )
                consecutive_rollbacks += 1
                continue

            new_netlist_texts = _apply_to_all(netlist_texts, approved_proposal["proposed_changes"])
            state.push_netlist_version(new_netlist_texts)

            new_sim_result = await agents.simulate(new_netlist_texts, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, "post_tuning": True, **new_sim_result})

            new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, "post_tuning": True, **new_judge_result})

            post_review = await agents.verify_post(
                judge_result, new_judge_result, approved_proposal["proposed_changes"]
            )
            state.log_event("verify_post", {"outer_iter": outer_iter, **post_review})

            outcome = "rolled_back" if post_review["recommendation"] == "rollback" else "kept"
            deltas = deltas_between(judge_result, new_judge_result)
            regressed = regressed_between(judge_result, new_judge_result)
            for change in approved_proposal["proposed_changes"]:
                tuning_history.append(
                    Attempt(
                        outer_iter=outer_iter,
                        retry=approved_retry,
                        refdes=change["refdes"],
                        param=change["param"],
                        old_value=change["old_value"],
                        new_value=change["new_value"],
                        outcome=outcome,
                        deltas=deltas,
                        regressed=regressed,
                    )
                )

            if post_review["recommendation"] == "rollback":
                state.rollback()
                consecutive_rollbacks += 1
                judge_result = new_judge_result
                continue

            consecutive_rollbacks = 0

            if new_judge_result["overall_pass"]:
                return _final_result(
                    "PASS", state, outer_iter, new_judge_result, topology_swaps=topology_swaps, tuning_history=tuning_history
                )

            judge_result = new_judge_result

        return _final_result(
            "FAIL", state, MAX_OUTER_ITERATIONS, judge_result,
            failure_reason="max iterations reached", topology_swaps=topology_swaps, tuning_history=tuning_history,
        )
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result,
            failure_reason=f"agent execution error: {exc}", topology_swaps=topology_swaps, tuning_history=tuning_history,
        )
    except ValueError as exc:
        # Belt-and-braces: check_refdes_resolution above is meant to reject an
        # unresolvable/ambiguous refdes before it ever reaches apply_changes
        # (which raises ValueError for the ambiguous case), so this should be
        # unreachable in normal operation. Kept anyway so a ValueError from
        # anywhere in the tuning-apply path (apply_changes, apply_topology_swap)
        # yields a clean FAIL instead of an uncaught crash - the same guarantee
        # CLAUDE.md documents for the AgentExecutionError catch above.
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result,
            failure_reason=str(exc), topology_swaps=topology_swaps, tuning_history=tuning_history,
        )
    except OSError as exc:
        # **이 루프는 디스크를 되읽는다.** 매 외부 이터레이션 머리의
        # `state.current_netlist_texts()`가 `state.py`에서 `open(path).read()`를
        # 한다 - 롤백으로 되돌아간 버전 파일이 사라지면(tmp reaper, NFS 재연결)
        # 여기서 `FileNotFoundError`가 난다. 가드가 없으면 `_final_result`가
        # 돌지 않고, `cli.main()`의 `write_result_json`/`write_report_md`에
        # 도달하지 못해 이미 여러 이터레이션을 산 실행이 산출물 없이 끝난다 -
        # 최적화 단계가 정확히 이 이유로 `OSError`를 가드에 넣었다.
        #
        # **이 절에서는 디스크를 다시 건드리지 않는다.** `state.log_event`를
        # 부르면 같은 디스크 문제로 핸들러가 다시 터져 가드가 있으나 마나가
        # 된다. 사유는 반환 dict에만 싣는다. `_final_result`가 부르는
        # `current_netlist_paths()`는 메모리 안의 `netlist_versions`만 읽으므로
        # 안전하다.
        #
        # 사유 문구는 `ValueError` 쪽과 구별한다. "넷리스트 적용이 실패했다"와
        # "실행이 자기 덱을 읽지 못했다"는 다른 사실이고, 뭉치면 다음 사람이
        # 튜닝 제안을 들여다보며 원인을 찾는다.
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result,
            failure_reason=f"the run could not read or write its own files: {exc}",
            topology_swaps=topology_swaps, tuning_history=tuning_history,
        )
