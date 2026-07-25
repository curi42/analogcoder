from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.netlist import apply_changes
from analogcoder.state import RunState

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3


@dataclass
class OrchestratorAgents:
    analyze: Callable
    simulate: Callable
    judge: Callable
    tune: Callable
    verify_pre: Callable
    verify_post: Callable


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

            approved_proposal = None
            rejection_feedback = None
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                proposal = await agents.tune(analysis, judge_result, tuning_history, rejection_feedback, netlist_text)
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                review = await agents.verify_pre(analysis, judge_result, proposal, netlist_text)
                state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

                if review["approved"]:
                    approved_proposal = proposal
                    break
                rejection_feedback = review["feedback"]

            if approved_proposal is None:
                return _final_result(
                    "FAIL", state, outer_iter, judge_result, failure_reason="tuning proposal repeatedly rejected"
                )

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
                judge_result = new_judge_result
                continue

            if new_judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, new_judge_result)

            judge_result = new_judge_result

        return _final_result("FAIL", state, MAX_OUTER_ITERATIONS, judge_result, failure_reason="max iterations reached")
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result, failure_reason=f"agent execution error: {exc}"
        )
