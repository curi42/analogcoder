# tests/unit/test_orchestrator.py
from types import SimpleNamespace

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState

PASS_JUDGE = {"overall_pass": True, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}]}
FAIL_JUDGE = {"overall_pass": False, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 18.0, "pass": False, "margin": -1.5}]}

FAKE_SPEC = SimpleNamespace(criteria=[])
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}
SUBCKT_NETLIST = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "R1 vinp mid 1k\n"
    "R2 mid vout 2k\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)
FAKE_TOPOLOGY_PROPOSAL = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
AREA_TEST_NETLIST = (
    "* test\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".end\n"
)
AREA_TEST_NETLIST_WITH_SUBCKT = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)


def make_agents(**overrides):
    async def default_analyze(netlist_text):
        return {"circuit_type": "inverting amplifier"}

    async def default_simulate(netlist_text, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return FAKE_PROPOSAL

    async def default_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    async def default_propose_topology(analysis, judge_result, available_topologies, rejection_feedback):
        return FAKE_TOPOLOGY_PROPOSAL

    defaults = dict(
        analyze=default_analyze,
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
        propose_topology=default_propose_topology,
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
    async def always_reject(analysis, judge_result, proposal, netlist_text):
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


@pytest.mark.asyncio
async def test_agent_execution_error_before_loop_returns_fail_with_zero_iterations(tmp_path):
    async def failing_analyze(netlist_text):
        raise AgentExecutionError("boom")

    agents = make_agents(analyze=failing_analyze)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["final_criteria"] == []
    assert result["failure_reason"] == "agent execution error: boom"


@pytest.mark.asyncio
async def test_agent_execution_error_mid_loop_reports_last_completed_iteration(tmp_path):
    call_count = {"n": 0}

    async def simulate_then_fail(netlist_text, spec):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise AgentExecutionError("simulator backend unreachable")
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    agents = make_agents(simulate=simulate_then_fail, judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["failure_reason"] == "agent execution error: simulator backend unreachable"


@pytest.mark.asyncio
async def test_topology_swap_never_offered_without_exactly_one_subckt(tmp_path):
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return FAKE_TOPOLOGY_PROPOSAL

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 0


@pytest.mark.asyncio
async def test_topology_swap_triggers_after_threshold_consecutive_rollbacks(tmp_path):
    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        return PASS_JUDGE if judge_calls["count"] == 8 else FAIL_JUDGE

    verify_post_calls = {"count": 0}

    async def verify_post_sequence(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        if verify_post_calls["count"] <= 3:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "fixed"}

    propose_topology_calls = []

    async def propose_topology(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls.append([t.id for t in available_topologies])
        return {"topology_id": available_topologies[0].id, "reasoning": "matches phase margin gap", "confidence": 90}

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(SUBCKT_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 4
    assert len(propose_topology_calls) == 1
    assert set(propose_topology_calls[0]) == {"miller_basic", "miller_nulling_resistor"}
    assert len(state.netlist_versions) == 2  # v0 initial + v1 after the kept topology swap


@pytest.mark.asyncio
async def test_topology_swap_repeatedly_invalid_id_fails_run(tmp_path):
    async def always_bad_topology(analysis, judge_result, available_topologies, rejection_feedback):
        return {"topology_id": "not_a_real_topology", "reasoning": "x", "confidence": 50}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=always_bad_topology,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(SUBCKT_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "topology proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_topology_swap_rollback_restores_analysis_without_reanalyzing(tmp_path):
    analyze_calls = {"count": 0}

    async def counting_analyze(netlist_text):
        analyze_calls["count"] += 1
        return {"circuit_type": "amp", "call": analyze_calls["count"]}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_once(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        analyze=counting_analyze,
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_once,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(SUBCKT_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    # initial analyze + one re-analyze per topology attempt (library has 2 entries,
    # each gets exactly one attempt across the 10-iteration budget); rollback restores
    # the saved pre-swap analysis instead of triggering a third analyze call
    assert analyze_calls["count"] == 3
    assert propose_topology_calls["count"] == 2


@pytest.mark.asyncio
async def test_area_check_rejects_without_calling_verify_pre(tmp_path):
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(analysis, judge_result, proposal, netlist_text):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def oversized_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        verify_pre=counting_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(AREA_TEST_NETLIST, FAKE_SPEC, state, agents)

    assert verify_pre_calls["count"] == 0
    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_area_check_mixed_with_verify_pre_rejection_hard_fails(tmp_path):
    call_count = {"n": 0}

    async def mixed_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        call_count["n"] += 1
        if call_count["n"] % 2 == 1:
            new_value = "100u"  # oversized -> area-rejected, 2.5x
        else:
            new_value = "50u"  # right-sized, 1.25x -> passes area, reaches verify_pre
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": new_value, "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    async def always_reject_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=mixed_tune,
        verify_pre=always_reject_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(AREA_TEST_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_area_rejection_eventually_triggers_topology_swap(tmp_path):
    async def oversized_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path))

    await run_orchestration(AREA_TEST_NETLIST_WITH_SUBCKT, FAKE_SPEC, state, agents)

    assert propose_topology_calls["count"] >= 1
