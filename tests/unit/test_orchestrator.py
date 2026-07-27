# tests/unit/test_orchestrator.py
import json
from types import SimpleNamespace

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState

PASS_JUDGE = {"overall_pass": True, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}]}
FAIL_JUDGE = {"overall_pass": False, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 18.0, "pass": False, "margin": -1.5}]}


def make_spec(*testbench_names):
    # analyzer 제거 이후 오케스트레이터는 매 iteration derive_structure(...,
    # spec.circuit_name)와 measurement_nets(tb.control_block)를 직접 호출하므로
    # 가짜 spec도 이 필드들을 갖춰야 한다.
    testbenches = [
        SimpleNamespace(name=n, criteria=[], control_block=".control\n.endc\n")
        for n in testbench_names
    ]
    return SimpleNamespace(
        circuit_name="fake", testbenches=testbenches, canonical=testbenches[0]
    )


FAKE_SPEC = make_spec("ac_loop_gain")
MULTI_SPEC = make_spec("ac_loop_gain", "psr_plus")
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}
# FAKE_PROPOSAL targets refdes "Rf" - every netlist fixture used with the
# default tune stub (or any other stub returning FAKE_PROPOSAL) must actually
# contain an "Rf" component, or check_refdes_resolution correctly rejects the
# proposal as matching nothing before verify_pre is ever called.
BASE_NETLIST = "* netlist\nRf vminus vout 10k\n.end\n"
SUBCKT_NETLIST = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "R1 vinp mid 1k\n"
    "R2 mid vout 2k\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    "Rf vminus vout 10k\n"
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
    async def default_simulate(netlist_texts, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return FAKE_PROPOSAL

    async def default_verify_pre(structure_view, judge_result, proposal, netlist_view):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    async def default_propose_topology(structure_view, judge_result, available_topologies, rejection_feedback):
        return FAKE_TOPOLOGY_PROPOSAL

    defaults = dict(
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
        propose_topology=default_propose_topology,
    )
    defaults.update(overrides)
    return OrchestratorAgents(**defaults)


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_immediate_pass_returns_pass_on_first_iteration(tmp_path):
    agents = make_agents()
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert result["final_netlist_paths"] == state.current_netlist_paths()
    assert result["run_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_fail_then_pass_after_tuning(tmp_path):
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert len(state.netlist_versions["ac_loop_gain"]) == 2  # v0 initial + v1 after applied tuning


@pytest.mark.asyncio
async def test_prereview_always_rejected_fails_run(tmp_path):
    async def always_reject(structure_view, judge_result, proposal, netlist_view):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_pre=always_reject)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


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
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 2


@pytest.mark.asyncio
async def test_max_iterations_exhausted_fails_run(tmp_path):
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_post=lambda p, n, c: _async(
        {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no progress"}
    ))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_agent_execution_error_before_loop_returns_fail_with_zero_iterations(tmp_path):
    # analyzer가 사라진 이후로는 구조 파생이 결정론적 파이썬이라 예외를 낼 수
    # 있는 첫 LLM 호출은 iteration 1의 agents.simulate다. 거기서 실패하면
    # 이 iteration은 완결되지 못했으므로 iterations_used는 여전히 0이어야 한다.
    async def failing_simulate(netlist_texts, spec):
        raise AgentExecutionError("boom")

    agents = make_agents(simulate=failing_simulate)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["final_criteria"] == []
    assert result["failure_reason"] == "agent execution error: boom"


@pytest.mark.asyncio
async def test_agent_execution_error_mid_loop_reports_last_completed_iteration(tmp_path):
    call_count = {"n": 0}

    async def simulate_then_fail(netlist_texts, spec):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise AgentExecutionError("simulator backend unreachable")
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    agents = make_agents(simulate=simulate_then_fail, judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["failure_reason"] == "agent execution error: simulator backend unreachable"


@pytest.mark.asyncio
async def test_topology_swap_never_offered_without_exactly_one_subckt(tmp_path):
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return FAKE_TOPOLOGY_PROPOSAL

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

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

    async def propose_topology(structure_view, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls.append([t.id for t in available_topologies])
        return {"topology_id": available_topologies[0].id, "reasoning": "matches phase margin gap", "confidence": 90}

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 4
    assert len(propose_topology_calls) == 1
    assert set(propose_topology_calls[0]) == {"miller_basic", "miller_nulling_resistor"}
    assert len(state.netlist_versions["ac_loop_gain"]) == 2  # v0 initial + v1 after the kept topology swap


@pytest.mark.asyncio
async def test_topology_swap_repeatedly_invalid_id_fails_run(tmp_path):
    async def always_bad_topology(structure_view, judge_result, available_topologies, rejection_feedback):
        return {"topology_id": "not_a_real_topology", "reasoning": "x", "confidence": 50}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=always_bad_topology,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "topology proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_topology_swap_can_recur_with_a_different_topology_after_a_rollback(tmp_path):
    # A rollback of a swap resets consecutive_rollbacks to 0 (not to a
    # "swap already tried" state), so parameter tuning resumes and can drive
    # a second swap threshold later in the same run - and the second attempt
    # must pick from the topologies not yet tried. This used to be entangled
    # with an analyze-call-count assertion; that part no longer applies now
    # that structure derivation isn't an LLM call, but the "can recur with a
    # different topology" behavior itself still needs coverage.
    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_once(structure_view, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_once,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 2


@pytest.mark.asyncio
async def test_area_check_rejects_without_calling_verify_pre(tmp_path):
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(structure_view, judge_result, proposal, netlist_view):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def oversized_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
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
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST}, FAKE_SPEC, state, agents)

    assert verify_pre_calls["count"] == 0
    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_area_check_mixed_with_verify_pre_rejection_hard_fails(tmp_path):
    call_count = {"n": 0}

    async def mixed_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
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

    async def always_reject_verify_pre(structure_view, judge_result, proposal, netlist_view):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=mixed_tune,
        verify_pre=always_reject_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_area_rejection_eventually_triggers_topology_swap(tmp_path):
    async def oversized_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST_WITH_SUBCKT}, FAKE_SPEC, state, agents)

    assert propose_topology_calls["count"] >= 1


TWO_SUBCKT_COLLIDING_REFDES_NETLIST = (
    "* two subckts whose compensation caps share a refdes\n"
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Xb0 a b c vdd vss BUF_P\n"
    "Xb1 d e f vdd vss BUF_N\n"
    ".end\n"
)


@pytest.mark.asyncio
async def test_ambiguous_refdes_proposal_is_rejected_without_crashing_or_calling_verify_pre(tmp_path):
    # Reproduces the C1 crash from the final-branch review: a tuner proposal
    # against an unqualified refdes that exists in more than one subckt used
    # to raise an uncaught ValueError from _apply_to_all. The deterministic
    # refdes-resolution gate must reject this before verify_pre is ever
    # called, and the run must terminate cleanly (not crash) via the normal
    # tuning-retry-exhausted path.
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(structure_view, judge_result, proposal, netlist_view):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def ambiguous_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return {
            "proposed_changes": [{"refdes": "Xcc", "param": "W", "old_value": "10", "new_value": "15", "reasoning": "x"}],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=ambiguous_tune,
        verify_pre=counting_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": TWO_SUBCKT_COLLIDING_REFDES_NETLIST}, FAKE_SPEC, state, agents
    )

    assert verify_pre_calls["count"] == 0
    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_value_error_from_apply_changes_is_caught_and_returns_clean_fail(tmp_path, monkeypatch):
    # Belt-and-braces: even if a ValueError somehow reaches _apply_to_all
    # (bypassing the deterministic refdes-resolution gate tested above),
    # run_orchestration must not let it escape as an uncaught exception.
    def raising_apply_changes(text, changes):
        raise ValueError(
            "refdes 'Xcc' is ambiguous - it matches components in BUF_N, BUF_P; qualify it as <subckt>.Xcc"
        )

    monkeypatch.setattr("analogcoder.orchestrator.apply_changes", raising_apply_changes)

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert "ambiguous" in result["failure_reason"]


@pytest.mark.asyncio
async def test_multi_testbench_tuning_change_applied_to_every_testbench(tmp_path):
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    initial = {
        "ac_loop_gain": "* ac\nRf vminus vout 10k\n.end\n",
        "psr_plus": "* psr\nRf vminus vout 10k\n.end\n",
    }
    result = await run_orchestration(initial, MULTI_SPEC, state, agents)

    assert result["status"] == "PASS"
    final_texts = state.current_netlist_texts()
    assert "11k" in final_texts["ac_loop_gain"]
    assert "11k" in final_texts["psr_plus"]


@pytest.mark.asyncio
async def test_multi_testbench_tune_and_verify_pre_receive_only_canonical_text(tmp_path):
    seen_texts = {"tune": None, "verify_pre": None}

    async def spying_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        seen_texts["tune"] = netlist_view
        return FAKE_PROPOSAL

    async def spying_verify_pre(structure_view, judge_result, proposal, netlist_view):
        seen_texts["verify_pre"] = netlist_view
        return {"approved": True, "concerns": [], "feedback": "ok"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE) if seen_texts["tune"] is None else _async(PASS_JUDGE),
        tune=spying_tune,
        verify_pre=spying_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    canonical_text = "* canonical text\nRf vminus vout 10k\n.end\n"
    initial = {"ac_loop_gain": canonical_text, "psr_plus": "* other testbench text\n.end\n"}
    await run_orchestration(initial, MULTI_SPEC, state, agents)

    # tune/verify_pre now receive render_netlist's *view* of the canonical
    # text (no subckts here, so nothing is folded - only the trailing
    # newline is lost by the splitlines/join round trip), not the raw text
    # verbatim, and never anything derived from the other testbench's text.
    assert seen_texts["tune"] == canonical_text.rstrip("\n")
    assert seen_texts["verify_pre"] == canonical_text.rstrip("\n")
    assert "other testbench" not in seen_texts["tune"]


@pytest.mark.asyncio
async def test_multi_testbench_rollback_restores_every_testbench(tmp_path):
    verify_post_calls = {"count": 0}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_post=verify_post_always_rollback)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    initial = {
        "ac_loop_gain": "* ac original\nRf vminus vout 10k\n.end\n",
        "psr_plus": "* psr original\nRf vminus vout 10k\n.end\n",
    }
    await run_orchestration(initial, MULTI_SPEC, state, agents)

    assert verify_post_calls["count"] >= 1
    final_texts = state.current_netlist_texts()
    assert final_texts["ac_loop_gain"] == "* ac original\nRf vminus vout 10k\n.end\n"
    assert final_texts["psr_plus"] == "* psr original\nRf vminus vout 10k\n.end\n"


def test_the_orchestrator_no_longer_calls_an_analyzer_agent():
    # analyzer는 결정론적 파생으로 대체됐다. dataclass에 필드가 남아 있으면
    # cli.py가 조용히 예전 배선을 유지할 수 있다.
    import dataclasses

    assert "analyze" not in {f.name for f in dataclasses.fields(OrchestratorAgents)}


@pytest.mark.asyncio
async def test_the_tuner_receives_a_rendered_structure_not_an_llm_analysis(tmp_path):
    seen = {}
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    async def capturing_tune(structure_view, judge_result, history, feedback, netlist_view):
        seen["structure"] = structure_view
        seen["netlist"] = netlist_view
        return FAKE_PROPOSAL

    agents = make_agents(tune=capturing_tune, judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert isinstance(seen["structure"], str)
    assert "circuit: fake" in seen["structure"]
    assert "Rf" in seen["netlist"]


@pytest.mark.asyncio
async def test_an_inapplicable_param_is_rejected_before_verify_pre_is_called(tmp_path):
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(structure_view, judge_result, proposal, netlist_view):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": ""}

    async def bad_tune(structure_view, judge_result, history, feedback, netlist_view):
        return {"proposed_changes": [{"refdes": "Rf", "param": "width",
                                      "old_value": "10k", "new_value": "15k"}]}

    # SUBCKT_NETLIST (a single subckt) rather than BASE_NETLIST: this also
    # exercises that a param_check rejection that repeats every retry - like
    # the area gate's identical shape - still escalates into a topology-swap
    # attempt after enough consecutive rollbacks, rather than only ever
    # reaching "tuning proposal repeatedly rejected". propose_topology_spy
    # mirrors test_area_rejection_eventually_triggers_topology_swap.
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        tune=bad_tune,
        verify_pre=counting_verify_pre,
        judge=lambda m, s: _async(FAIL_JUDGE),
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert verify_pre_calls["count"] == 0
    events = [json.loads(line) for line in open(state.history_path)]
    assert any(e["step"] == "param_check" and e["approved"] is False for e in events)
    assert propose_topology_calls["count"] >= 1


@pytest.mark.asyncio
async def test_the_focus_decision_is_logged_so_an_elision_is_never_invisible(tmp_path):
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]
    assert focus_events
    assert focus_events[0]["blocks"] == ["AMP"]


TWO_SUBCKT_NETLIST = (
    "* two independent subckts - only AMP drives the measured net\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "R1 vinp mid 1k\n"
    "R2 mid vout 2k\n"
    ".ends AMP\n"
    ".subckt BIAS vdd vss iref\n"
    "Rb vdd iref 1k\n"
    ".ends BIAS\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    "Xbias1 vdd vss iref BIAS\n"
    ".end\n"
)
GAIN_CONTROL_BLOCK = ".control\nac dec 10 1 1meg\nmeas ac gain_db find vdb(vout) at=1k\n.endc\n"


def _two_subckt_spec():
    gain_criterion = SimpleNamespace(name="gain", measurement="gain_db", operator=">=", threshold=19.5)
    tb = SimpleNamespace(name="ac_loop_gain", criteria=[gain_criterion], control_block=GAIN_CONTROL_BLOCK)
    return SimpleNamespace(circuit_name="two_subckt", testbenches=[tb], canonical=tb)


@pytest.mark.asyncio
async def test_the_criterion_to_measurement_to_net_mapping_produces_a_non_degenerate_focus(tmp_path):
    # Every other focus test in this file uses FAKE_SPEC/MULTI_SPEC, whose
    # testbenches all have criteria=[] - so measurement_by_criterion is
    # always {} and failing_nets is always set(), meaning select_focus
    # always takes the "no seed -> expose every block" fallback. That
    # fallback happens to look identical to a correctly-computed focus
    # whenever there is only one subckt (as in SUBCKT_NETLIST), so swapping
    # c.name/c.measurement, transposing the two dicts built from spec, or
    # deleting the nets_by_measurement.update loop entirely would leave
    # every existing orchestrator test green. A real criterion plus a
    # control_block whose meas line names a net that only one of two
    # subckts drives forces a genuine, non-fallback focus decision: if the
    # mapping is broken, this asserts {"AMP", "BIAS"} instead of {"AMP"}.
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": TWO_SUBCKT_NETLIST}, _two_subckt_spec(), state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]
    assert focus_events
    assert focus_events[0]["blocks"] == ["AMP"]


@pytest.mark.asyncio
async def test_verify_pre_sees_the_full_body_of_an_out_of_focus_block_the_proposal_touches(tmp_path):
    # Regression for a verify_pre-specific failure mode: render_netlist folds
    # an out-of-focus subckt's body to "* ... (N components elided)", but
    # verify_pre's own prompt instructs it to reject any refdes/param that
    # isn't an exact token "in the netlist above" - so a proposal against a
    # real, gate-approved component that merely lives in an out-of-focus
    # block would look, from inside that folded view, exactly like the
    # thing the verifier is told to reject. Here focus is {"AMP"} (BIAS
    # never touches the failing net), and the proposal targets Rb inside
    # BIAS - unqualified, so this also exercises resolving an unscoped
    # refdes into its real (out-of-focus) subckt rather than guessing from
    # a dotted prefix that doesn't exist here at all.
    seen = {}
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    async def spying_verify_pre(structure_view, judge_result, proposal, netlist_view):
        seen["netlist_view"] = netlist_view
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def tune_bias(structure_view, judge_result, history, feedback, netlist_view):
        return {
            "proposed_changes": [
                {"refdes": "Rb", "param": "value", "old_value": "1k", "new_value": "2k", "reasoning": "x"}
            ]
        }

    agents = make_agents(judge=judge_fails_then_passes, tune=tune_bias, verify_pre=spying_verify_pre)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": TWO_SUBCKT_NETLIST}, _two_subckt_spec(), state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]
    assert focus_events[0]["blocks"] == ["AMP"]  # BIAS starts out of focus
    assert "elided" not in seen["netlist_view"]
    assert "Rb vdd iref 1k" in seen["netlist_view"]
