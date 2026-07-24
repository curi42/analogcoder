# tests/unit/test_orchestrator.py
from types import SimpleNamespace

import pytest

from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState

PASS_JUDGE = {"overall_pass": True, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}]}
FAIL_JUDGE = {"overall_pass": False, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 18.0, "pass": False, "margin": -1.5}]}

FAKE_SPEC = SimpleNamespace(criteria=[])
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}


def make_agents(**overrides):
    async def default_analyze(netlist_text):
        return {"circuit_type": "inverting amplifier"}

    async def default_simulate(netlist_text, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(analysis, judge_result, history, rejection_feedback):
        return FAKE_PROPOSAL

    async def default_verify_pre(analysis, judge_result, proposal):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    defaults = dict(
        analyze=default_analyze,
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
    )
    defaults.update(overrides)
    return OrchestratorAgents(**defaults)


@pytest.mark.asyncio
async def test_immediate_pass_returns_pass_on_first_iteration(tmp_path):
    agents = make_agents()
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1


@pytest.mark.asyncio
async def test_fail_then_pass_after_tuning(tmp_path):
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert len(state.netlist_versions) == 2  # v0 initial + v1 after applied tuning


@pytest.mark.asyncio
async def test_prereview_always_rejected_fails_run(tmp_path):
    async def always_reject(analysis, judge_result, proposal):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_pre=always_reject)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_postreview_rollback_consumes_an_iteration_then_succeeds(tmp_path):
    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        # iter1 pre: FAIL, iter1 post (rolled back): FAIL, iter2 pre: FAIL, iter2 post: PASS
        return [FAIL_JUDGE, FAIL_JUDGE, FAIL_JUDGE, PASS_JUDGE][judge_calls["count"] - 1]

    verify_post_calls = {"count": 0}

    async def verify_post_first_rollback(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        if verify_post_calls["count"] == 1:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "worse"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "better"}

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_first_rollback)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 2


@pytest.mark.asyncio
async def test_max_iterations_exhausted_fails_run(tmp_path):
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_post=lambda p, n, c: _async(
        {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no progress"}
    ))
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
