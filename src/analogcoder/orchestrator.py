from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area_limits import check_area_growth, index_baseline_components
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
    status: str, state: RunState, iterations_used: int, judge_result: dict | None, failure_reason: str | None = None
) -> dict:
    result = {
        "status": status,
        "final_netlist_paths": state.current_netlist_paths(),
        "run_dir": state.run_dir,
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"] if judge_result else [],
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


def _apply_to_all(netlist_texts: dict[str, str], changes: list[dict]) -> dict[str, str]:
    return {name: apply_changes(text, changes) for name, text in netlist_texts.items()}


async def run_orchestration(
    initial_netlist_texts: dict[str, str], spec, state: RunState, agents: OrchestratorAgents
) -> dict:
    canonical_name = spec.canonical.name
    state.push_netlist_version(initial_netlist_texts)
    outer_iter = 0
    judge_result: dict = {}

    try:
        topology_swap_available = len(parse_netlist(initial_netlist_texts[canonical_name]).subckts) == 1
        tried_topologies: set[str] = set()
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
                return _final_result("PASS", state, outer_iter, judge_result)

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

            untried_topologies = (
                [t for t in TOPOLOGY_LIBRARY.values() if t.id not in tried_topologies]
                if topology_swap_available and consecutive_rollbacks >= TOPOLOGY_SWITCH_THRESHOLD
                else []
            )

            if untried_topologies:
                topology_id = None
                rejection_feedback = None
                for retry in range(1, MAX_TUNING_RETRIES + 1):
                    proposal = await agents.propose_topology(
                        structure_view, judge_result, untried_topologies, rejection_feedback
                    )
                    state.log_event("topology_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                    candidate = proposal["topology_id"]
                    if candidate in TOPOLOGY_LIBRARY and candidate not in tried_topologies:
                        topology_id = candidate
                        break
                    rejection_feedback = (
                        f"'{candidate}' is not an available untried topology. "
                        f"Choose one of: {[t.id for t in untried_topologies]}"
                    )

                if topology_id is None:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="topology proposal repeatedly rejected",
                    )

                tried_topologies.add(topology_id)
                topology = TOPOLOGY_LIBRARY[topology_id]
                subckt_name = next(iter(parse_netlist(netlist_texts[canonical_name]).subckts))
                # Replaces the whole subckt body with the library's fixed defaults, so any
                # parameter-tuning changes made earlier in the run (before the rollback streak
                # that triggered this swap) are silently discarded, not carried forward. This is
                # intentional: the new topology's own defaults are what was verified to work.
                new_netlist_texts = {
                    name: apply_topology_swap(text, subckt_name, topology.subckt_body)
                    for name, text in netlist_texts.items()
                }
                state.push_netlist_version(new_netlist_texts)

                new_sim_result = await agents.simulate(new_netlist_texts, spec)
                state.log_event(
                    "simulation", {"outer_iter": outer_iter, "post_topology_swap": True, **new_sim_result}
                )

                new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
                state.log_event(
                    "judge", {"outer_iter": outer_iter, "post_topology_swap": True, **new_judge_result}
                )

                post_review = await agents.verify_post(
                    judge_result, new_judge_result, [{"topology_id": topology_id}]
                )
                state.log_event("verify_post", {"outer_iter": outer_iter, "topology_swap": True, **post_review})

                consecutive_rollbacks = 0

                if post_review["recommendation"] == "rollback":
                    state.rollback()
                    judge_result = new_judge_result
                    continue

                if new_judge_result["overall_pass"]:
                    return _final_result("PASS", state, outer_iter, new_judge_result)

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

                area_ok, area_feedback = check_area_growth(baseline_components, proposal["proposed_changes"])
                state.log_event(
                    "area_check",
                    {"outer_iter": outer_iter, "retry": retry, "approved": area_ok, "feedback": area_feedback},
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
                return _final_result("PASS", state, outer_iter, new_judge_result)

            judge_result = new_judge_result

        return _final_result("FAIL", state, MAX_OUTER_ITERATIONS, judge_result, failure_reason="max iterations reached")
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result, failure_reason=f"agent execution error: {exc}"
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
            "FAIL", state, max(outer_iter - 1, 0), judge_result, failure_reason=str(exc)
        )
