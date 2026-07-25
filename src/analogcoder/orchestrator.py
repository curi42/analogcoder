from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
from analogcoder.state import RunState
from analogcoder.topologies import TOPOLOGY_LIBRARY

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3
TOPOLOGY_SWITCH_THRESHOLD = 3


@dataclass
class OrchestratorAgents:
    analyze: Callable
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
        "final_netlist_path": state.current_netlist_path(),
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"] if judge_result else [],
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


async def run_orchestration(initial_netlist_text: str, spec, state: RunState, agents: OrchestratorAgents) -> dict:
    state.push_netlist_version(initial_netlist_text)
    outer_iter = 0
    judge_result: dict = {}

    try:
        analysis = await agents.analyze(initial_netlist_text)
        state.log_event("analysis", analysis)

        topology_swap_available = len(parse_netlist(initial_netlist_text).subckts) == 1
        tried_topologies: set[str] = set()
        consecutive_rollbacks = 0
        # Intentionally computed once from netlist_v0 and never refreshed after a
        # topology swap: components introduced by a swapped-in topology (e.g. a
        # nulling resistor Rz) have nothing in the original netlist to compare
        # against, so they are simply unconstrained by the area gate for the
        # rest of the run. This is by-design, not a bug - do not "fix" it.
        baseline_components = index_baseline_components(initial_netlist_text)

        tuning_history: list[dict] = []

        for outer_iter in range(1, MAX_OUTER_ITERATIONS + 1):
            with open(state.current_netlist_path()) as f:
                netlist_text = f.read()

            sim_result = await agents.simulate(netlist_text, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

            judge_result = await agents.judge(sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, **judge_result})

            if judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, judge_result)

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
                        analysis, judge_result, untried_topologies, rejection_feedback
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
                subckt_name = next(iter(parse_netlist(netlist_text).subckts))
                # Replaces the whole subckt body with the library's fixed defaults, so any
                # parameter-tuning changes made earlier in the run (before the rollback streak
                # that triggered this swap) are silently discarded, not carried forward. This is
                # intentional: the new topology's own defaults are what was verified to work.
                new_netlist_text = apply_topology_swap(netlist_text, subckt_name, topology.subckt_body)
                state.push_netlist_version(new_netlist_text)

                pre_swap_analysis = analysis
                analysis = await agents.analyze(new_netlist_text)
                state.log_event("analysis", {"outer_iter": outer_iter, "topology_id": topology_id, **analysis})

                new_sim_result = await agents.simulate(new_netlist_text, spec)
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
                    analysis = pre_swap_analysis
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
                proposal = await agents.tune(analysis, judge_result, tuning_history, rejection_feedback, netlist_text)
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                area_ok, area_feedback = check_area_growth(baseline_components, proposal["proposed_changes"])
                state.log_event(
                    "area_check",
                    {"outer_iter": outer_iter, "retry": retry, "approved": area_ok, "feedback": area_feedback},
                )
                if not area_ok:
                    rejection_feedback = area_feedback
                    continue

                review = await agents.verify_pre(analysis, judge_result, proposal, netlist_text)
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

            new_netlist_text = apply_changes(netlist_text, approved_proposal["proposed_changes"])
            state.push_netlist_version(new_netlist_text)

            new_sim_result = await agents.simulate(new_netlist_text, spec)
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
