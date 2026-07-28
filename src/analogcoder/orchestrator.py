from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area_limits import evaluate_area_growth, index_baseline_components
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


def _final_result(
    status: str,
    state: RunState,
    iterations_used: int,
    judge_result: dict | None,
    failure_reason: str | None = None,
    topology_swaps: list[dict] | None = None,
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
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


def _apply_to_all(netlist_texts: dict[str, str], changes: list[dict]) -> dict[str, str]:
    return {name: apply_changes(text, changes) for name, text in netlist_texts.items()}


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
    initial_netlist_texts: dict[str, str], spec, state: RunState, agents: OrchestratorAgents
) -> dict:
    canonical_name = spec.canonical.name
    state.push_netlist_version(initial_netlist_texts)
    outer_iter = 0
    judge_result: dict = {}
    # try 밖에서 초기화한다 - 두 except 절이 이것을 읽는데, try 안에서 처음
    # 대입하면 첫 줄에서 터진 실행이 NameError로 바뀐다.
    topology_swaps: list[dict] = []

    try:
        # No structural precondition on the deck (no more len(subckts) == 1
        # gate) - every iteration compatible_swaps() decides, from parsed
        # facts (ports/models/scale/no-op), which (block, topology) pairs are
        # actually safe to offer. A pair is a tuple, not a bare topology id,
        # because the same topology can legitimately be tried again against a
        # different block once the first attempt is tried-and-exhausted.
        tried_topologies: set[tuple[str, str]] = set()
        consecutive_rollbacks = 0
        # Intentionally computed once from netlist_v0 and never refreshed after a
        # topology swap: components introduced by a swapped-in topology (e.g. a
        # nulling resistor Rz) have nothing in the original netlist to compare
        # against, so they are simply unconstrained by the area gate for the
        # rest of the run. This is by-design, not a bug - do not "fix" it.
        baseline_components = index_baseline_components(initial_netlist_texts[canonical_name])

        tuning_history: list[dict] = []

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

        for outer_iter in range(1, MAX_OUTER_ITERATIONS + 1):
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
                    "PASS", state, outer_iter, judge_result, topology_swaps=topology_swaps
                )

            failing_nets: set[str] = set()
            for criterion in judge_result["criteria"]:
                if criterion["pass"]:
                    continue
                measurement = measurement_by_criterion.get(criterion["name"])
                failing_nets |= nets_by_measurement.get(measurement, set())

            touched_refdes = {
                change["refdes"]
                for entry in tuning_history
                for change in entry["proposal"]["proposed_changes"]
            }
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
                                "PASS", state, outer_iter, new_judge_result, topology_swaps=topology_swaps
                            )

                        judge_result = new_judge_result
                        continue

            approved_proposal = None
            rejection_feedback = None
            verify_pre_rejected_any = False
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                proposal = await agents.tune(
                    structure_view, judge_result, tuning_history, rejection_feedback, netlist_view
                )
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                area = evaluate_area_growth(baseline_components, proposal["proposed_changes"])
                area_ok, area_feedback = area.approved, area.feedback
                # states는 승인 여부와 별개로 **게이트가 무엇을 볼 수 있었는지**를
                # 남긴다. 이것 없이는 "볼 것이 없어서 통과"(nf)와 "정의가 include
                # 안에만 있어 볼 수 없어서 통과"(blind)가 로그에서 구별되지
                # 않는다 - 면적 게이트가 조용히 무력해진 두 번의 전례가 모두
                # 실행 로그로는 알아챌 수 없었던 이유다.
                state.log_event(
                    "area_check",
                    {
                        "outer_iter": outer_iter,
                        "retry": retry,
                        "approved": area_ok,
                        "feedback": area_feedback,
                        "states": area.states,
                    },
                )
                if not area_ok:
                    rejection_feedback = area_feedback
                    continue

                refdes_ok, refdes_feedback = check_refdes_resolution(
                    netlist_texts[canonical_name], proposal["proposed_changes"]
                )
                state.log_event(
                    "refdes_check",
                    {"outer_iter": outer_iter, "retry": retry, "approved": refdes_ok, "feedback": refdes_feedback},
                )
                if not refdes_ok:
                    rejection_feedback = refdes_feedback
                    continue

                # 원본 전문을 넘긴다 - 이 게이트는 초점과 무관하게 판정해야
                # 한다 (area_check/refdes_check와 동일한 원칙).
                param_ok, param_feedback = check_param_applicability(
                    netlist_texts[canonical_name], proposal["proposed_changes"]
                )
                state.log_event(
                    "param_check",
                    {"outer_iter": outer_iter, "retry": retry, "approved": param_ok, "feedback": param_feedback},
                )
                if not param_ok:
                    rejection_feedback = param_feedback
                    continue

                # 최상위 자극원/전원을 건드리는 제안은 "회로를 안 고친 채
                # 측정만 바꾸는" 제안이다 - 앞의 세 게이트 어느 것도 그것을
                # 막지 않고, judge는 모든 기준이 좋아졌으니 통과시킨다.
                stimulus_ok, stimulus_feedback = check_stimulus_untouched(
                    netlist_texts[canonical_name], proposal["proposed_changes"]
                )
                state.log_event(
                    "stimulus_check",
                    {
                        "outer_iter": outer_iter,
                        "retry": retry,
                        "approved": stimulus_ok,
                        "feedback": stimulus_feedback,
                    },
                )
                if not stimulus_ok:
                    rejection_feedback = stimulus_feedback
                    continue

                misses = focus_misses(focus, proposal["proposed_changes"], netlist_texts[canonical_name])
                if misses:
                    # 기록만 하고 흐름은 막지 않는다 - 초점이 틀렸다는 증거이지
                    # 제안이 틀렸다는 증거가 아니다.
                    state.log_event(
                        "focus_miss",
                        {"outer_iter": outer_iter, "retry": retry, "refdes": misses},
                    )

                # verify_pre에는 초점을 제안이 실제로 지목한 블록까지 넓혀
                # 넘긴다. 원래 초점만 넘기면, 비초점 서브회로는 본문이 접혀
                # "* ... (N components elided)"로만 보이는데도 verify_pre의
                # 프롬프트는 "넷리스트 위의 컴포넌트 줄에 없는 refdes/param은
                # 거부하라"고 지시한다 - 접힌 블록을 지목한, 게이트를 이미
                # 통과한 정상적인 제안이 그 문장 그대로 거부 대상처럼 보이게
                # 되어 verify_pre가 초점 오류를 튜너의 잘못으로 오판한다.
                # 튜너 쪽 뷰(netlist_view)는 그대로 둔다 - 넓혀야 하는 쪽은
                # 지금 판정 중인 제안을 실제로 봐야 하는 verifier뿐이다.
                resolved_blocks = resolve_change_scopes(
                    netlist_texts[canonical_name], proposal["proposed_changes"]
                )
                verify_netlist_view = render_netlist(netlist_texts[canonical_name], focus | resolved_blocks)

                review = await agents.verify_pre(structure_view, judge_result, proposal, verify_netlist_view)
                state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

                if review["approved"]:
                    approved_proposal = proposal
                    break
                verify_pre_rejected_any = True
                rejection_feedback = review["feedback"]

            if approved_proposal is None:
                if verify_pre_rejected_any:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="tuning proposal repeatedly rejected",
                        topology_swaps=topology_swaps,
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

            tuning_history.append({
                "outer_iter": outer_iter,
                "proposal": approved_proposal,
                "recommendation": post_review["recommendation"],
            })

            if post_review["recommendation"] == "rollback":
                state.rollback()
                consecutive_rollbacks += 1
                judge_result = new_judge_result
                continue

            consecutive_rollbacks = 0

            if new_judge_result["overall_pass"]:
                return _final_result(
                    "PASS", state, outer_iter, new_judge_result, topology_swaps=topology_swaps
                )

            judge_result = new_judge_result

        return _final_result(
            "FAIL", state, MAX_OUTER_ITERATIONS, judge_result,
            failure_reason="max iterations reached", topology_swaps=topology_swaps,
        )
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result,
            failure_reason=f"agent execution error: {exc}", topology_swaps=topology_swaps,
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
            failure_reason=str(exc), topology_swaps=topology_swaps,
        )
