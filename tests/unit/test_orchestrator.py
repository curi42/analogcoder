# tests/unit/test_orchestrator.py
import json
import os
from types import SimpleNamespace

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState
from analogcoder.topologies import TOPOLOGY_LIBRARY
from tests.unit.wrapper_decks import INCLUDE_ONLY_DECK

PASS_JUDGE = {"overall_pass": True, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}]}
FAIL_JUDGE = {"overall_pass": False, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 18.0, "pass": False, "margin": -1.5}]}


def make_spec(*testbench_names):
    # analyzer 제거 이후 오케스트레이터는 매 iteration derive_structure(...,
    # spec.circuit_name)와 measurement_nets(tb.control_block)를 직접 호출하므로
    # 가짜 spec도 이 필드들을 갖춰야 한다.
    testbenches = [
        SimpleNamespace(name=n, criteria=[], control_block=".control\n.endc\n", fragments=None)
        for n in testbench_names
    ]
    return SimpleNamespace(
        circuit_name="fake",
        testbenches=testbenches,
        canonical=testbenches[0],
        # 실제 `TargetSpec`이 갖는 필드다. 대안 선별이 개선량을 계산할 때
        # `spec.all_criteria`를 읽으므로 가짜 spec도 갖춰야 한다 - 여기서는
        # 비어 있고(위 테스트벤치들의 criteria가 전부 []), 그러면 개선량은
        # 0이 되어 선택이 면적 분기로만 갈린다.
        all_criteria=[c for tb in testbenches for c in tb.criteria],
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
# M6 (ctype 'M', model NMOSG, unit-suffixed "40u") isn't a candidate for any
# library topology: NMOSG is a model no library body instantiates
# (compatible_swaps rejects every pair on "models"), and this fixture also
# declares no `.option scale`, which the topologies require to be 1e-6 for
# "scale" too. Both a real .option scale and a device the library's models
# check can see are needed for a swap to ever trigger - but simply adding
# `.option scale=1.0u` next to M6's own unit-suffixed "40u" would double the
# deck's scale into M6's tiering (geometry_scale multiplies every component's
# baseline value, not just bare-numeral sky130 primitives - see the
# `.option scale` gotcha in CLAUDE.md), silently loosening its tier and
# breaking the area-rejection this test needs. So the oversized device is
# rewritten in the same X-prefixed, bare-geometry sky130 style the library
# itself uses (Xm6, W=40 with no unit suffix) rather than kept as a generic
# M-refdes device once scale is declared; Xp1/Xcc make AMP model-compatible
# with both miller variants.
AREA_TEST_NETLIST_WITH_SUBCKT = (
    "* test\n"
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "Xm6 vout outA vss vss sky130_fd_pr__nfet_01v8 L=1 W=40\n"
    "Xp1 vout vinp vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=8\n"
    "Xcc vout 0 sky130_fd_pr__cap_mim_m3_1 w=6 l=6 mf=1\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)
NINE_PORTS = "vinp vinn vout vdd vss nbias ncas pbias pcas"
# A generic body that instantiates the same three sky130 model families the
# folded-cascode topologies need (nfet/pfet/res_high_po), but with a
# component sequence that matches neither library body - so compatible_swaps
# never rejects it as identical_body. Ports match the folded-cascode
# topologies' 9-port interface exactly; the two miller variants (5 ports)
# are rejected on "ports" for this shape.
GENERIC_9PORT_SWAPPABLE_BODY = (
    "Xn1 vout nbias vss vss sky130_fd_pr__nfet_01v8 L=1 W=8\n"
    "Xp1 vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=8\n"
    "XRz vout ncas 0 sky130_fd_pr__res_high_po w=1 l=15\n"
)
MULTI_BLOCK_9PORT_NETLIST = (
    "* two 9-port blocks - today impossible to swap at all (len(subckts) == 1)\n"
    ".option scale=1.0u\n"
    f".subckt BLOCK1 {NINE_PORTS}\n"
    f"{GENERIC_9PORT_SWAPPABLE_BODY}"
    ".ends BLOCK1\n"
    f".subckt BLOCK2 {NINE_PORTS}\n"
    f"{GENERIC_9PORT_SWAPPABLE_BODY}"
    ".ends BLOCK2\n"
    f"Xb1 {NINE_PORTS} BLOCK1\n"
    "Xb2 vinp2 vinn2 vout2 vdd vss nbias ncas pbias pcas BLOCK2\n"
    "Rf vminus vout 10k\n"
    ".end\n"
)
# A single 5-port block whose body instantiates the three sky130 model
# families both miller variants need, but isn't textually identical to
# either body - so both miller_basic and miller_nulling_resistor are real
# candidates (the two 9-port folded-cascode topologies are rejected on
# "ports" for a 5-port block).
GENERIC_5PORT_SWAPPABLE_BODY = (
    "Xn1 vout vinn vss vss sky130_fd_pr__nfet_01v8 L=1 W=8\n"
    "Xp1 vout vinp vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=8\n"
    "Xcc vout 0 sky130_fd_pr__cap_mim_m3_1 w=6 l=6 mf=1\n"
)
GENERIC_5PORT_SWAPPABLE_NETLIST = (
    "* one 5-port block compatible with both miller variants\n"
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    f"{GENERIC_5PORT_SWAPPABLE_BODY}"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    "Rf vminus vout 10k\n"
    ".end\n"
)
# Two 5-port blocks, both compatible with both miller variants - so an
# omitted block_path naming only a topology_id (not a specific block) is
# genuinely ambiguous between AMP1 and AMP2.
TWO_BLOCK_5PORT_SWAPPABLE_NETLIST = (
    "* two 5-port blocks, both compatible with both miller variants\n"
    ".option scale=1.0u\n"
    ".subckt AMP1 vinp vinn vout vdd vss\n"
    f"{GENERIC_5PORT_SWAPPABLE_BODY}"
    ".ends AMP1\n"
    ".subckt AMP2 vinp vinn vout vdd vss\n"
    f"{GENERIC_5PORT_SWAPPABLE_BODY}"
    ".ends AMP2\n"
    "Xamp1 vinp vinn vout vdd vss AMP1\n"
    "Xamp2 vinp2 vinn2 vout2 vdd vss AMP2\n"
    "Rf vminus vout 10k\n"
    ".end\n"
)
# AMP is swappable; OTHER carries a resistor (Rx) that a normal
# parameter-tuning proposal can independently tune and keep, unrelated to any
# topology in the library.
TARGET_PLUS_OTHER_NETLIST = (
    "* AMP is swappable; OTHER carries a separately-tunable resistor\n"
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    f"{GENERIC_5PORT_SWAPPABLE_BODY}"
    ".ends AMP\n"
    ".subckt OTHER a b\n"
    "Rx a b 1k\n"
    ".ends OTHER\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    "Xother1 n1 n2 OTHER\n"
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

    async def default_propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
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
async def test_a_multi_block_deck_can_now_swap(tmp_path):
    """Today this is structurally impossible - the old gate was
    len(subckts) == 1, and this deck has two. compatible_swaps replaces that
    structural precondition with a per-(block, topology) applicability check,
    so a two-block deck must still be able to swap after enough consecutive
    rollbacks. Reverting to the old len(subckts) == 1 gate makes
    propose_topology never get called on this fixture, so the run never
    swaps and just exhausts max iterations instead of reaching PASS - that
    is the mutation this test catches."""
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

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_topology_calls.append(candidates)
        chosen = candidates[0]
        return {
            "topology_id": chosen.topology_id, "block_path": chosen.block_path,
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": MULTI_BLOCK_9PORT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert len(propose_topology_calls) == 1
    assert propose_topology_calls[0]  # a non-empty candidate list actually reached the agent
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 1
    assert swap_events[0]["block_path"] in {"BLOCK1", "BLOCK2"}


@pytest.mark.asyncio
async def test_no_compatible_candidate_logs_topology_unavailable_and_stays_in_parameter_mode(tmp_path):
    # SUBCKT_NETLIST's AMP body is plain generic resistors - it instantiates
    # no model the library's topologies need, so compatible_swaps rejects
    # every (block, topology) pair on "models" and offers zero candidates.
    # Today's equivalent of "library exhausted": log it and keep tuning
    # parameters rather than dead-ending the run.
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, candidates, library, rejection_feedback):
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

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 0

    events = [json.loads(line) for line in open(state.history_path)]
    candidates_events = [e for e in events if e["step"] == "topology_candidates"]
    unavailable_events = [e for e in events if e["step"] == "topology_unavailable"]
    assert candidates_events
    assert all(e["candidates"] == [] for e in candidates_events)
    assert all(e["rejections"] for e in candidates_events)  # rejections recorded, not just silence
    assert unavailable_events
    assert len(candidates_events) == len(unavailable_events)
    # Zero candidates is one observation covering several different facts.
    # This deck DOES define a block, and every pair was refused by the
    # compatibility rules - which is not the same fact as "the library was
    # exhausted" or "there is no .subckt here at all". A reason code that
    # collapses those (or is missing entirely) is what this pins.
    assert all(e["reason"] == "all_pairs_rejected" for e in unavailable_events)


@pytest.mark.asyncio
async def test_topology_candidates_is_logged_even_when_a_swap_is_approved(tmp_path):
    """Catches a mutation that makes topology_candidates conditional (e.g.
    logging it only when there are zero candidates, or only inside a
    rejection retry) - the event must be unconditional, so it must still
    appear even when a candidate is offered AND resolved on the very first
    try AND the swap is ultimately kept."""
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

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        chosen = candidates[0]
        return {
            "topology_id": chosen.topology_id, "block_path": chosen.block_path,
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    events = [json.loads(line) for line in open(state.history_path)]
    candidates_events = [e for e in events if e["step"] == "topology_candidates"]
    assert len(candidates_events) == 1
    assert candidates_events[0]["candidates"]  # non-empty, and logged despite the approved swap
    # GENERIC_5PORT_SWAPPABLE_NETLIST's AMP also gets rejected for both
    # folded_cascode topologies (9 ports vs its 5) - those rejections must
    # still be reported even though this same iteration ALSO has real
    # candidates and an approved swap. Catches forcing rejections to [] on
    # any iteration that has a non-empty candidates list.
    assert candidates_events[0]["rejections"]


@pytest.mark.asyncio
async def test_an_omitted_block_path_resolves_when_only_one_block_is_a_candidate(tmp_path):
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

    async def propose_topology_without_block_path(structure_view, judge_result, candidates, library, rejection_feedback):
        # Deliberately omits "block_path" - TOPOLOGY_SCHEMA allows it, and
        # GENERIC_5PORT_SWAPPABLE_NETLIST has exactly one block (AMP), so
        # "miller_nulling_resistor" identifies exactly one candidate.
        return {"topology_id": "miller_nulling_resistor", "reasoning": "x", "confidence": 90}

    agents = make_agents(
        judge=judge_sequence, verify_post=verify_post_sequence,
        propose_topology=propose_topology_without_block_path,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert result["status"] == "PASS"
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 1
    assert swap_events[0]["block_path"] == "AMP"
    assert swap_events[0]["topology_id"] == "miller_nulling_resistor"


@pytest.mark.asyncio
async def test_an_omitted_block_path_with_two_candidate_blocks_retries_with_feedback(tmp_path):
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

    proposal_calls = []

    async def propose_topology_ambiguous_then_specific(structure_view, judge_result, candidates, library, rejection_feedback):
        proposal_calls.append(rejection_feedback)
        if len(proposal_calls) == 1:
            # TWO_BLOCK_5PORT_SWAPPABLE_NETLIST has AMP1 and AMP2, both
            # compatible with miller_nulling_resistor - omitting block_path
            # here is genuinely ambiguous.
            return {"topology_id": "miller_nulling_resistor", "reasoning": "x", "confidence": 90}
        return {
            "topology_id": "miller_nulling_resistor", "block_path": "AMP2",
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(
        judge=judge_sequence, verify_post=verify_post_sequence,
        propose_topology=propose_topology_ambiguous_then_specific,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": TWO_BLOCK_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert len(proposal_calls) == 2
    assert proposal_calls[0] is None  # first attempt, no feedback yet
    assert proposal_calls[1] is not None
    assert "AMP1" in proposal_calls[1] and "AMP2" in proposal_calls[1]
    assert result["status"] == "PASS"
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 1
    assert swap_events[0]["block_path"] == "AMP2"


@pytest.mark.asyncio
async def test_a_block_path_topology_id_pair_not_in_candidates_is_rejected(tmp_path):
    """A (block_path, topology_id) pair the agent invents - one that isn't
    actually in the candidate list compatible_swaps offered - must be
    rejected with retryable feedback, never applied unchecked. Applying it
    unchecked would swap a port-mismatched body into a real block silently;
    an invented topology_id would instead reach TOPOLOGY_LIBRARY[topology_id]
    as an uncaught KeyError (caught by neither `except AgentExecutionError`
    nor `except ValueError`), crashing the run instead of failing cleanly.
    Here the pair names two things that are each real on their own -
    "AMP" is a real block, "folded_cascode_nmos_in_cs" is a real library
    topology - just never a real candidate together (folded_cascode_* needs
    9 ports; AMP has 5), so this also exercises the exact-pair check rather
    than a lookup that would trivially fail on a nonexistent name."""
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

    proposal_calls = []

    async def propose_topology_hallucinated_then_valid(structure_view, judge_result, candidates, library, rejection_feedback):
        proposal_calls.append(rejection_feedback)
        if len(proposal_calls) == 1:
            return {
                "topology_id": "folded_cascode_nmos_in_cs", "block_path": "AMP",
                "reasoning": "x", "confidence": 90,
            }
        return {
            "topology_id": "miller_nulling_resistor", "block_path": "AMP",
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(
        judge=judge_sequence, verify_post=verify_post_sequence,
        propose_topology=propose_topology_hallucinated_then_valid,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert len(proposal_calls) == 2
    assert proposal_calls[0] is None  # first attempt, no feedback yet
    assert proposal_calls[1] is not None  # rejected with retryable feedback, not a crash
    assert result["status"] == "PASS"
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 1
    assert swap_events[0]["topology_id"] == "miller_nulling_resistor"
    assert swap_events[0]["block_path"] == "AMP"


@pytest.mark.asyncio
async def test_a_swap_is_applied_to_every_testbench_that_defines_the_block(tmp_path):
    """compatible_swaps' missing_in_testbench rule guarantees a genuine
    candidate is defined in every testbench that gets versioned together, so
    the orchestrator must actually rewrite the block in all of them, not
    just canonical. Mutating the swap's dict comprehension to only touch
    canonical_name (e.g. `apply_topology_swap(...) if name == canonical_name
    else text`) leaves every single-testbench topology test in this file
    green - this is the one shape only a multi-testbench test can catch.
    Measured consequence on a real spec: `two_stage_opamp/spec.yaml` has 4
    testbenches and `bandgap/spec.yaml` has 5; under that mutant the
    non-canonical decks would keep the OLD block body while judge merges
    measurements from two different circuits, and push_netlist_version would
    version that inconsistent set."""
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

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        chosen = next(c for c in candidates if c.topology_id == "miller_nulling_resistor")
        return {
            "topology_id": chosen.topology_id, "block_path": chosen.block_path,
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    initial = {
        "ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST,
        "psr_plus": GENERIC_5PORT_SWAPPABLE_NETLIST,
    }
    result = await run_orchestration(initial, MULTI_SPEC, state, agents)

    assert result["status"] == "PASS"
    final_texts = state.current_netlist_texts()
    # Rz only exists in miller_nulling_resistor's body - if it's missing from
    # either testbench, that deck was never actually swapped.
    assert "Rz" in final_texts["ac_loop_gain"]
    assert "Rz" in final_texts["psr_plus"]


@pytest.mark.asyncio
async def test_a_swap_replaces_only_the_target_block_and_keeps_other_blocks_tuning(tmp_path):
    """A kept parameter-tuning change to an unrelated block (OTHER.Rx) must
    survive a later topology swap of a different block (AMP) - the swap
    rewrites only the block it targets, never the whole deck. A mutation
    that swaps the whole deck (e.g. re-parsing and replacing everything
    rather than just the addressed block) would lose OTHER's kept 1k -> 2k
    change - that is the mutation this test catches."""
    tune_calls = {"count": 0}

    async def tune_then_repeat(structure_view, judge_result, history, rejection_feedback, netlist_view):
        tune_calls["count"] += 1
        if tune_calls["count"] == 1:
            return {
                "proposed_changes": [
                    {"refdes": "Rx", "param": "value", "old_value": "1k", "new_value": "2k", "reasoning": "x"}
                ],
                "overall_reasoning": "x", "confidence": 90,
            }
        # A safe shrink (never an area-tier violation, unlike a further
        # growth from the already-at-tier-boundary 2k) so this reaches
        # verify_post every time instead of being rejected earlier by the
        # area gate - the point of these repeats is to accumulate rollbacks
        # through verify_post, not through a different gate.
        return {
            "proposed_changes": [
                {"refdes": "Rx", "param": "value", "old_value": "2k", "new_value": "1.9k", "reasoning": "x"}
            ],
            "overall_reasoning": "x", "confidence": 90,
        }

    verify_post_calls = {"count": 0}

    async def verify_post_sequence(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        if verify_post_calls["count"] == 1:
            return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}
        if verify_post_calls["count"] <= 4:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "fixed"}

    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        return PASS_JUDGE if judge_calls["count"] == 10 else FAIL_JUDGE

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        # Pick miller_nulling_resistor specifically (not just "the first AMP
        # candidate") so the final assert below can look for something (Rz)
        # that only that topology's body introduces.
        assert any(
            c.block_path == "AMP" and c.topology_id == "miller_nulling_resistor" for c in candidates
        )
        return {
            "topology_id": "miller_nulling_resistor", "block_path": "AMP",
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(
        tune=tune_then_repeat, verify_post=verify_post_sequence,
        judge=judge_sequence, propose_topology=propose_topology,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": TARGET_PLUS_OTHER_NETLIST}, FAKE_SPEC, state, agents
    )

    assert result["status"] == "PASS"
    final_text = state.current_netlist_texts()["ac_loop_gain"]
    assert "Rx a b 2k" in final_text  # OTHER's earlier kept tuning survives the AMP swap
    # The swap itself must also have been kept (not rolled back) for this to
    # actually exercise "swap + unrelated earlier tuning coexist" - Rz only
    # exists in miller_nulling_resistor's body, never in AMP's original one.
    assert "Rz" in final_text
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 1
    assert swap_events[0]["block_path"] == "AMP"


@pytest.mark.asyncio
async def test_the_swap_event_records_which_refdes_the_area_gate_can_no_longer_bound(tmp_path):
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

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        return {
            "topology_id": "miller_nulling_resistor", "block_path": "AMP",
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert result["status"] == "PASS"
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 1
    unconstrained = set(swap_events[0]["unconstrained_refdes"])
    stale = set(swap_events[0]["stale_baseline_refdes"])
    # GENERIC_5PORT_SWAPPABLE_BODY's Xn1/Xcc happen to share a refdes with two
    # of miller_nulling_resistor's own components, but with entirely
    # different nodes/params - so those two keep a baseline entry, and it is
    # now stale (bounded against the OLD device's geometry, not the new
    # topology's). Everything else the new topology introduces has never
    # been indexed at all and is genuinely unconstrained. Keys are fully
    # qualified ("<block_path>.<refdes>") since a bare "Rz" is ambiguous the
    # moment a deck has more than one amp.
    assert "AMP.Xn1" not in unconstrained
    assert "AMP.Xcc" not in unconstrained
    assert stale == {"AMP.Xn1", "AMP.Xcc"}
    assert {"AMP.Rz", "AMP.Xp3", "AMP.Rbias", "AMP.Xn2"} <= unconstrained


@pytest.mark.asyncio
async def test_a_topology_proposal_that_never_resolves_falls_back_to_parameter_tuning(tmp_path):
    """A failed *escalation attempt* must never be worse than not escalating.

    This used to return FAIL("topology proposal repeatedly rejected"),
    throwing away the remaining outer iterations and a still-viable
    parameter-tuning path. That mirrors the precedent CLAUDE.md records for
    the area gate: exhausting all retries on a *deterministic* gate is
    treated like a parameter-tuning rollback, never an immediate run failure
    - the parameter path itself hard-FAILs only when an LLM verifier
    rejected (verify_pre_rejected_any).

    Two mutations this catches:

    1. Restoring the `return _final_result("FAIL", ..., "topology proposal
       repeatedly rejected")`: failure_reason becomes that string, and no
       tuning_proposal is logged at the iteration the escalation failed.
    2. Dropping the `consecutive_rollbacks = 0` reset: the counter stays at
       or above TOPOLOGY_SWITCH_THRESHOLD forever, so EVERY subsequent
       iteration burns another MAX_TUNING_RETRIES topology LLM calls. With
       the reset, the threshold is re-reached only every 3rd iteration, so
       iterations 4/7/10 escalate - 3 attempts x 3 retries = 9 calls. Without
       it, iterations 4..10 all escalate - 7 x 3 = 21.
    """
    propose_calls = []

    async def always_unresolvable_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_calls.append(rejection_feedback)
        return {"topology_id": "not_a_real_topology", "reasoning": "x", "confidence": 50}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=always_unresolvable_topology,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"

    # The retry loop still ran the full MAX_TUNING_RETRIES with feedback -
    # the coverage the old FAIL-pinning test provided must not be lost.
    assert len(propose_calls) == 9  # 3 escalating iterations x MAX_TUNING_RETRIES
    assert propose_calls[0] is None
    assert propose_calls[1] is not None and propose_calls[2] is not None
    assert "not_a_real_topology" in propose_calls[1]

    events = [json.loads(line) for line in open(state.history_path)]
    unavailable = [e for e in events if e["step"] == "topology_unavailable"]
    assert [e["reason"] for e in unavailable] == ["proposal_unresolved"] * 3
    assert unavailable[0]["outer_iter"] == 4
    # ...and the SAME iteration went on to tune parameters instead of ending.
    assert any(e["step"] == "tuning_proposal" and e["outer_iter"] == 4 for e in events)


@pytest.mark.asyncio
async def test_an_always_omitted_block_path_on_an_ambiguous_deck_does_not_end_the_run(tmp_path):
    """The measured scenario behind this fix.

    TOPOLOGY_SCHEMA deliberately leaves block_path out of `required` (a weak
    model omitting a required field would hard-FAIL every spec), and on the
    decks this branch exists for an omitted block_path is ALWAYS ambiguous:
    bandgap/spec.yaml offers 6 candidates with 3 blocks per topology id.
    So the resolver correctly refuses to guess, all MAX_TUNING_RETRIES are
    spent, and the old code ended the run right there.

    Measured on real ngspice with only the agent stubbed: with block_path
    present the run PASSes at iteration 4 (buf0_gain_db 100.158); with it
    omitted it FAILed at iteration 4 with the deck back at netlist_v0
    (73.515). Here the same shape is reproduced with stubs: the failed
    escalation must leave the run free to reach PASS through parameter
    tuning in that very iteration. Restoring the FAIL return turns this into
    FAIL("topology proposal repeatedly rejected") - the mutation caught.
    """
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

    propose_calls = []

    async def propose_topology_always_omits_block_path(structure_view, judge_result, candidates, library, rejection_feedback):
        # TWO_BLOCK_5PORT_SWAPPABLE_NETLIST defines AMP1 and AMP2, both
        # compatible with miller_nulling_resistor - so this is genuinely
        # ambiguous and the deterministic layer must not guess a block.
        propose_calls.append(rejection_feedback)
        return {"topology_id": "miller_nulling_resistor", "reasoning": "x", "confidence": 90}

    agents = make_agents(
        judge=judge_sequence, verify_post=verify_post_sequence,
        propose_topology=propose_topology_always_omits_block_path,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": TWO_BLOCK_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 4
    assert len(propose_calls) == 3  # the escalation really was attempted and exhausted
    assert "AMP1" in propose_calls[1] and "AMP2" in propose_calls[1]

    events = [json.loads(line) for line in open(state.history_path)]
    assert [e["step"] for e in events].count("topology_swap") == 0  # nothing was guessed
    unavailable = [e for e in events if e["step"] == "topology_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0]["reason"] == "proposal_unresolved"
    assert any(e["step"] == "tuning_proposal" and e["outer_iter"] == 4 for e in events)


@pytest.mark.asyncio
async def test_a_kept_swap_reaches_the_result_with_its_block_topology_and_area_counts(tmp_path):
    """A topology swap replaces a block's ENTIRE body - in the measured
    bandgap run, BUF_P's 16 devices, a different polarity and a different
    sizing - and neither result.json nor report.md said so. CLAUDE.md already
    records this exact shape from the optimization phase: "the result must
    describe the deck it returns".

    Deleting the swap record (or the `topology_swaps=` argument on the PASS
    return, so the result is built from an empty list) is what this catches;
    so is dropping the area counts, which are what says how much of the deck
    the area gate can no longer bound for the rest of the run."""
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

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        return {
            "topology_id": "miller_nulling_resistor", "block_path": "AMP",
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert result["status"] == "PASS"
    assert result["topology_swaps"] == [{
        "outer_iter": 4,
        "block_path": "AMP",
        "topology_id": "miller_nulling_resistor",
        # M6: what this entry was actually verified with must travel with the
        # swap record. Without it, `history.jsonl` and result.json cannot tell
        # a run that swapped in a `verified_at="nominal"` entry from one that
        # swapped in a corner-verified one - which is the entire reason F2
        # added these two fields to Topology.
        # 2026-08-04: 이 값은 "corners" 였다. 바이어스 수정과 함께 이 항목의
        # 코너 주장이 실측으로 반증되어 "nominal" 로 내려갔다 - 이 테스트가
        # 계속 못박는 것은 **필드가 스왑 기록을 타고 흐른다는 것**이다.
        # 다만 이제 두 값을 가르는 대비는 folded_cascode 항목들에만 남아 있다.
        "provenance": "extracted",
        "verified_at": "nominal",
        # miller_nulling_resistor's body has 14 components (16 until the
        # 2026-08-04 bias change dropped Xp4/Rdeg/Rstart and added Rbias);
        # Xn1 and Xcc share a refdes with GENERIC_5PORT_SWAPPABLE_BODY (so
        # they keep a - now stale - baseline entry), the other 12 were never
        # indexed at all.
        "unconstrained_refdes": 12,
        "stale_baseline_refdes": 2,
        "outcome": "kept",
    }]


@pytest.mark.asyncio
async def test_a_rolled_back_swap_is_recorded_as_rolled_back_not_omitted(tmp_path):
    """A swap that was tried and rolled back is a fact about the run, not a
    non-event: it consumed an iteration and burned a library entry. Recording
    only kept swaps (e.g. appending after the rollback check instead of
    before it) would make a run that attempted two swaps look like it never
    escalated at all."""
    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    async def propose_topology_first_candidate(structure_view, judge_result, candidates, library, rejection_feedback):
        chosen = candidates[0]
        return {
            "topology_id": chosen.topology_id, "block_path": chosen.block_path,
            "reasoning": "x", "confidence": 80,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_first_candidate,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    swaps = result["topology_swaps"]
    assert len(swaps) == 2
    assert [s["outcome"] for s in swaps] == ["rolled_back", "rolled_back"]
    assert [s["block_path"] for s in swaps] == ["AMP", "AMP"]
    assert swaps[0]["topology_id"] != swaps[1]["topology_id"]


@pytest.mark.asyncio
async def test_a_run_without_any_swap_carries_an_empty_swap_list(tmp_path):
    """The key is unconditional so "no swap happened" and "the record was
    dropped" are not the same absence - the report is what decides not to
    draw an empty section."""
    agents = make_agents()
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["topology_swaps"] == []


@pytest.mark.asyncio
async def test_verify_post_is_told_which_block_the_swap_replaced(tmp_path):
    """The keep/rollback verdict is fully determined by the before/after
    judge results, so this is not a correctness issue - but the verifier's
    free-text feedback is the human-readable record that lands in
    history.jsonl, and "swapped folded_cascode_pmos_in_cs" is ambiguous
    across the four amplifiers of benchmarks/bandgap. Dropping block_path
    from the applied_changes payload is the mutation this catches."""
    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        return PASS_JUDGE if judge_calls["count"] == 8 else FAIL_JUDGE

    verify_post_calls = {"count": 0}
    applied_seen = []

    async def verify_post_sequence(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        applied_seen.append(applied_changes)
        if verify_post_calls["count"] <= 3:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "fixed"}

    async def propose_topology(structure_view, judge_result, candidates, library, rejection_feedback):
        return {
            "topology_id": "miller_nulling_resistor", "block_path": "AMP",
            "reasoning": "x", "confidence": 90,
        }

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration(
        {"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents
    )

    assert result["status"] == "PASS"
    assert applied_seen[-1] == [
        {"topology_id": "miller_nulling_resistor", "block_path": "AMP"}
    ]


@pytest.mark.asyncio
async def test_topology_swap_can_recur_with_a_different_topology_after_a_rollback(tmp_path):
    # A rollback of a swap resets consecutive_rollbacks to 0 (not to a
    # "swap already tried" state), so parameter tuning resumes and can drive
    # a second swap threshold later in the same run - and the second attempt
    # must pick from the (block, topology) pairs not yet tried. This used to
    # be entangled with an analyze-call-count assertion; that part no longer
    # applies now that structure derivation isn't an LLM call, but the "can
    # recur with a different topology" behavior itself still needs coverage.
    #
    # The propose_topology fake always returns candidates[0] - the ONLY thing
    # that can make the second attempt differ from the first is
    # tried_topologies actually excluding the first pair from the second
    # call's candidate list. Asserting propose_topology_calls["count"] == 2
    # alone does not pin this: with always-rollback, 10 outer iterations
    # allow exactly 2 swap attempts regardless of whether `tried` records
    # anything, so deleting the `tried_topologies.add(...)` call - or storing
    # a bare topology_id instead of the (block_path, topology_id) tuple -
    # leaves this count assertion green while both attempts silently pick the
    # SAME topology (candidates[0] never changes). The topology_id
    # comparison below is what actually distinguishes "recur with a
    # different topology" from "retry the same one twice".
    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_once(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_topology_calls["count"] += 1
        chosen = candidates[0]
        return {
            "topology_id": chosen.topology_id, "block_path": chosen.block_path,
            "reasoning": "x", "confidence": 80,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_once,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 2
    events = [json.loads(line) for line in open(state.history_path)]
    swap_events = [e for e in events if e["step"] == "topology_swap"]
    assert len(swap_events) == 2
    assert swap_events[0]["topology_id"] != swap_events[1]["topology_id"]


@pytest.mark.asyncio
async def test_the_library_can_genuinely_exhaust_and_the_run_falls_back_to_parameter_tuning(tmp_path, monkeypatch):
    """Distinct from test_no_compatible_candidate_logs_topology_unavailable_...:
    that one reaches zero candidates via a "models" rejection on a fixture
    that never had a real candidate at all. Here the fixture
    (GENERIC_5PORT_SWAPPABLE_NETLIST) has exactly one genuine candidate once
    the library is monkeypatched down to a single topology - so the run can
    exhaust it within the iteration budget - and this proves
    `tried_topologies` is what turns "tried once" into "genuinely
    unavailable next time", not a structural fact about the deck.

    Deleting `tried_topologies.add(...)` (or storing a bare topology_id
    instead of the (block_path, topology_id) tuple) leaves the same one
    candidate available forever, so a second call to propose_topology would
    happen instead of topology_unavailable ever being logged - that is the
    mutation this test catches.
    """
    monkeypatch.setattr(
        "analogcoder.orchestrator.TOPOLOGY_LIBRARY",
        {"miller_nulling_resistor": TOPOLOGY_LIBRARY["miller_nulling_resistor"]},
    )

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_once(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_topology_calls["count"] += 1
        chosen = candidates[0]
        return {
            "topology_id": chosen.topology_id, "block_path": chosen.block_path,
            "reasoning": "x", "confidence": 80,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_once,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 1  # tried once, never re-offered

    events = [json.loads(line) for line in open(state.history_path)]
    unavailable_events = [e for e in events if e["step"] == "topology_unavailable"]
    assert unavailable_events  # the (only) candidate was genuinely exhausted
    # ...and it says WHY. Before this, a genuinely exhausted library and a
    # deck with no .subckt at all emitted byte-identical history:
    # {"step":"topology_candidates","candidates":[],"rejections":[]} then
    # {"step":"topology_unavailable","outer_iter":N} - neither of which is
    # distinguishable from someone deleting the check. Exhaustion is now a
    # recorded rejection ("already_tried"), not an absence.
    assert all(e["reason"] == "all_pairs_already_tried" for e in unavailable_events)
    candidates_events = [e for e in events if e["step"] == "topology_candidates"]
    assert candidates_events[-1]["rejections"]
    assert {r["reason"] for r in candidates_events[-1]["rejections"]} == {"already_tried"}


@pytest.mark.asyncio
async def test_a_deck_with_no_subckt_at_all_says_so_rather_than_just_no_candidates(tmp_path):
    """`benchmarks/inverting_amp/spec.yaml` is this shape: a flat deck with no
    `.subckt` definition anywhere, so no (block, topology) pair can even be
    enumerated. That is a different fact from "the library was exhausted"
    and from "every pair was refused by the rules", and all three used to
    produce identical history. Collapsing the reason codes back into one
    string - or reporting this deck as all_pairs_rejected, which would claim
    rejections that never happened - is the mutation this catches."""
    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_topology_calls["count"] += 1
        return FAKE_TOPOLOGY_PROPOSAL

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

    events = [json.loads(line) for line in open(state.history_path)]
    unavailable_events = [e for e in events if e["step"] == "topology_unavailable"]
    assert unavailable_events
    assert all(e["reason"] == "no_subckt_definitions" for e in unavailable_events)
    candidates_events = [e for e in events if e["step"] == "topology_candidates"]
    # No pair exists to reject, so an empty rejections list here is the truth -
    # which is exactly why the reason code has to carry the fact instead.
    assert all(e["candidates"] == [] and e["rejections"] == [] for e in candidates_events)


@pytest.mark.asyncio
async def test_area_check_no_longer_blocks_but_the_record_survives(tmp_path):
    """2026-08-05 강등. 예전에는 면적 게이트가 거부하고 `verify_pre`를 부르지도
    않았다. 지금은 지나가되 **무엇을 얼마나 키웠는지가 그대로 남는다** -
    게이트를 지우면 성장이 보이지 않게 되고, 그것은 조용히 무력한 게이트의
    반대 방향 실수다."""
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

    # 강등: 거부하지 않으므로 LLM 검토자까지 간다.
    assert verify_pre_calls["count"] > 0

    events = [json.loads(line) for line in open(state.history_path)]
    area_events = [e for e in events if e["step"] == "area_check"]
    assert area_events
    for e in area_events:
        # 계산 결과는 그대로다 - 2.5배는 여전히 티어를 넘는다.
        assert e["approved"] is False
        # **무조건** 실린다. 키의 부재와 false 가 구별되어야 "강등됐다"와
        # "이 계측이 사라졌다"가 갈린다.
        assert e["blocking"] is False
        assert e["feedback"]

    # 그리고 면적은 더 이상 거부 사유로 기록되지 않는다.
    assert result["attempt_summary"]["rejected_by_reason"]["area"] == 0


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
async def test_gate_rejection_eventually_triggers_topology_swap(tmp_path):
    """결정론 게이트만으로 재시도가 소진되면 하드 FAIL 이 아니라 토폴로지
    승격이다. 2026-08-05 이전에는 면적 게이트로 이것을 핀했는데 그 게이트가
    강등되어 더 이상 거부하지 않으므로, **아직 거부하는** 게이트로 옮겼다 -
    핀하는 동작은 그대로다."""
    async def blocked_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return {
            "proposed_changes": [
                {"refdes": "Znope", "param": "value", "old_value": "1k", "new_value": "2k", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_topology_calls["count"] += 1
        chosen = candidates[0]
        return {"topology_id": chosen.topology_id, "block_path": chosen.block_path, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=blocked_tune,
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

    # GENERIC_5PORT_SWAPPABLE_NETLIST (a single, library-compatible subckt)
    # rather than BASE_NETLIST: this also exercises that a param_check
    # rejection that repeats every retry - like the area gate's identical
    # shape - still escalates into a topology-swap attempt after enough
    # consecutive rollbacks, rather than only ever reaching "tuning proposal
    # repeatedly rejected". propose_topology_spy mirrors
    # test_area_rejection_eventually_triggers_topology_swap.
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(structure_view, judge_result, candidates, library, rejection_feedback):
        propose_topology_calls["count"] += 1
        chosen = candidates[0]
        return {"topology_id": chosen.topology_id, "block_path": chosen.block_path, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        tune=bad_tune,
        verify_pre=counting_verify_pre,
        judge=lambda m, s: _async(FAIL_JUDGE),
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": GENERIC_5PORT_SWAPPABLE_NETLIST}, FAKE_SPEC, state, agents)

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
    tb = SimpleNamespace(name="ac_loop_gain", criteria=[gain_criterion], control_block=GAIN_CONTROL_BLOCK, fragments=None)
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
async def test_two_testbenches_defining_the_same_measurement_name_do_not_collapse(tmp_path):
    # nets_by_measurement.update(...)는 테스트벤치를 가로질러 last-writer-wins
    # 로 병합했다. 두 테스트벤치가 같은 measurement 이름을 정의하면(PSR
    # 테스트벤치들이 실제로 이름을 재사용한다) 앞선 테스트벤치가 보던 넷이
    # 조용히 사라져, 초점이 원인 블록 대신 마지막 테스트벤치의 블록만
    # 가리킨다. 합집합이어야 한다.
    gain = SimpleNamespace(name="gain", measurement="gain_db", operator=">=", threshold=19.5)
    tb_out = SimpleNamespace(
        name="ac_loop_gain",
        criteria=[gain],
        control_block=".control\nmeas ac gain_db find vdb(vout) at=1k\n.endc\n",
        fragments=None)
    tb_ref = SimpleNamespace(
        name="psr",
        criteria=[],
        control_block=".control\nmeas ac gain_db find vdb(iref) at=1k\n.endc\n",
        fragments=None)
    spec = SimpleNamespace(
        circuit_name="two_subckt", testbenches=[tb_out, tb_ref], canonical=tb_out
    )

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr"])

    await run_orchestration(
        {"ac_loop_gain": TWO_SUBCKT_NETLIST, "psr": TWO_SUBCKT_NETLIST}, spec, state, agents
    )

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]
    assert focus_events[0]["blocks"] == ["AMP", "BIAS"]


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


@pytest.mark.asyncio
async def test_area_check_event_records_what_the_gate_could_see(tmp_path):
    # M1/I1. approved/feedback만 남기면 "nf라서 볼 것이 없었다"와 "정의가
    # include에만 있어 볼 수 없었다"가 로그에서 바이트 단위로 똑같다. 이
    # 저장소에서 면적 게이트가 조용히 무력해진 적이 두 번 있었고, 둘 다
    # 실행 로그로는 알아챌 수 없었다.
    async def blind_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return {
            "proposed_changes": [
                {"refdes": "xwrap1", "param": "wn", "old_value": "2e-6", "new_value": "2e-3",
                 "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=blind_tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration(
        {"ac_loop_gain": INCLUDE_ONLY_DECK}, FAKE_SPEC, state, agents
    )

    with open(state.history_path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    area_events = [e for e in events if e["step"] == "area_check"]

    assert area_events
    assert all(e["approved"] is True for e in area_events)
    assert all(e["states"] == {"xwrap1.wn": "blind"} for e in area_events)


TWO_CRITERION_BEFORE = {
    "overall_pass": False,
    "criteria": [
        {"name": "pm", "target": ">=60", "actual": 50.0, "pass": False, "margin": -10.0},
        {"name": "ugbw", "target": ">=1e6", "actual": 2e6, "pass": True, "margin": 1e6},
    ],
}
TWO_CRITERION_AFTER = {
    "overall_pass": False,
    "criteria": [
        {"name": "pm", "target": ">=60", "actual": 58.0, "pass": False, "margin": -2.0},
        {"name": "ugbw", "target": ">=1e6", "actual": 0.5e6, "pass": False, "margin": -0.5e6},
    ],
}


@pytest.mark.asyncio
async def test_a_rolled_back_attempt_carries_its_measured_deltas_to_the_next_proposal(tmp_path):
    """어느 변형을 잡는가: 히스토리에 recommendation만 남기는 원래 구현.
    "롤백됨"만으로는 무엇이 얼마나 움직였는지 알 수 없고, 그 숫자는
    new_judge_result 안에 이미 있다. verify_post의 regressed_criteria를
    일부러 비워 둔 것도 변형 탐지다 - 회귀가 거기서 온다면 이 테스트가 통과할
    수 없다."""
    seen = []
    calls = {"n": 0}

    async def judge(measurements, spec):
        calls["n"] += 1
        return TWO_CRITERION_BEFORE if calls["n"] == 1 else TWO_CRITERION_AFTER

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        seen.append(attempts_view)
        return FAKE_PROPOSAL

    async def rollback_verify_post(prev_judge, new_judge, applied_changes):
        return {
            "improved": False,
            "regressed_criteria": [],  # 비워 둔다 - 우리는 이것을 쓰지 않는다
            "recommendation": "rollback",
            "feedback": "regressed",
        }

    agents = make_agents(judge=judge, tune=tune, verify_post=rollback_verify_post)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert seen[0] == ""                  # 첫 제안에는 히스토리가 없다
    assert "rolled_back" in seen[1]
    assert "pm +8" in seen[1]             # 측정된 델타
    assert "ugbw -1.5e+06" in seen[1]
    assert "regressed [ugbw]" in seen[1]  # verify_post가 아니라 judge에서 나온 회귀


@pytest.mark.asyncio
async def test_the_attempt_log_event_is_written_even_before_any_attempt_exists(tmp_path):
    """어느 변형을 잡는가: 항목이 있을 때만 로그를 남기는 구현.
    "기록했고 0건"과 "기록 자체가 사라졌다"가 history.jsonl에서 구별되어야
    한다 - 이 저장소에서 조용히 무력해진 게이트가 아홉 번 나왔고, 그 중 여섯
    번은 실행 로그로 알아챌 수 없었다."""
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    logs = [e for e in events if e["step"] == "attempt_log"]

    assert logs, "attempt_log가 하나도 없다"
    assert logs[0]["total"] == 0
    assert logs[0]["rendered"] == 0
    assert logs[0]["dropped"] == 0


STIMULUS_NETLIST = "* tb\nVin in 0 1\nRf vminus vout 10k\n.end\n"


def one_change(refdes, param, old_value, new_value):
    return {
        "proposed_changes": [
            {"refdes": refdes, "param": param, "old_value": old_value,
             "new_value": new_value, "reasoning": "x"}
        ],
        "overall_reasoning": "x",
        "confidence": 90,
    }


@pytest.mark.parametrize(
    "reason, netlist, proposal, reject_verify_pre",
    [
        # 면적 행은 2026-08-05 강등과 함께 빠졌다 - 그 게이트는 더 이상 거부하지
        # 않으므로 사유 코드를 만들지 않는다. `REJECTION_REASONS`에는 남아 있고
        # (과거 history.jsonl 이 그 코드를 싣는다) 강등 자체는
        # `test_area_check_no_longer_blocks_but_the_record_survives`가 핀한다.
        # refdes: 어느 컴포넌트와도 안 맞는다
        ("refdes", BASE_NETLIST, one_change("Znope", "value", "1k", "2k"), False),
        # param: "width" 는 Rf 줄에도 동일 모델 peer 에도 없는 이름이다
        ("param", BASE_NETLIST, one_change("Rf", "width", "10k", "11k"), False),
        # stimulus: 최상위 V 원이다
        ("stimulus", STIMULUS_NETLIST, one_change("Vin", "value", "1", "100"), False),
        # verify_pre: 게이트는 전부 통과하고 LLM 검토자가 거부한다
        ("verify_pre", BASE_NETLIST, FAKE_PROPOSAL, True),
    ],
)
@pytest.mark.asyncio
async def test_each_gate_records_its_own_reason_code(
    tmp_path, reason, netlist, proposal, reject_verify_pre
):
    """어느 변형을 잡는가: 다섯 게이트의 사유를 하나로 뭉개는 구현 -
    "rejected"만 남기거나, 이벤트 스트림에서 사유를 다시 파싱하는 구현
    (area_check와 refdes_check가 둘 다 feedback 키를 쓰므로 그쪽에서는
    복원되지 않는다). 다섯 파라미터가 다섯을 구별한다."""
    seen = []

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        seen.append(attempts_view)
        return proposal

    overrides = {"judge": lambda m, s: _async(FAIL_JUDGE), "tune": tune}
    if reject_verify_pre:
        async def reject(structure_view, judge_result, proposal_, netlist_view):
            return {"approved": False, "concerns": [], "feedback": "not justified"}
        overrides["verify_pre"] = reject

    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    await run_orchestration(
        {"ac_loop_gain": netlist}, FAKE_SPEC, state, make_agents(**overrides)
    )

    assert any(f"{reason}:" in view for view in seen), f"{reason} 사유가 튜너에게 안 보인다"


@pytest.mark.asyncio
async def test_a_gate_rejection_survives_into_the_next_outer_iteration(tmp_path):
    """어느 변형을 잡는가: 거부를 rejection_feedback으로만 나르는 원래 구현.
    그 변수는 outer 이터레이션마다 None으로 리셋되고 할당마다 덮어써지므로,
    이터레이션 1에서 막힌 노브는 이터레이션 2에서 존재하지 않는다.
    이 테스트가 사라지면 그 회귀가 조용해진다."""
    seen = []

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        seen.append(attempts_view)
        # 항상 refdes 게이트에 막힌다. 면적 게이트를 쓰던 것을 2026-08-05
        # 강등과 함께 옮겼다 - 핀하는 것은 "거부가 이터레이션 경계를 넘어
        # 살아남는가"이지 어느 게이트가 거부했는가가 아니다.
        return one_change("Znope", "value", "1k", "2k")

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    # 이터레이션 1은 재시도 MAX_TUNING_RETRIES(3)회로 끝난다 -> seen[0..2].
    # seen[3]은 이터레이션 2의 첫 호출이고, 원래 구현에서는 여기가 "" 였다.
    assert seen[0] == ""
    assert seen[3].count("refdes:") == 3


# AMP와 OTHER 두 블록 - OTHER만 top-level 넷 vother를 구동해서, 아래 spec의
# measurement가 그 넷을 가리키면 select_focus의 씨앗이 OTHER 하나로만
# 잡히고("전 블록 노출" 폴백을 타지 않는다), AMP는 거부를 통해서만 초점에
# 들어온다.
FOCUS_TEST_NETLIST = (
    "* two blocks - only OTHER drives the net the spec's measurement watches\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "R1 vinp mid 1k\n"
    "R2 mid vout 2k\n"
    ".ends AMP\n"
    ".subckt OTHER a b\n"
    "Rx a b 1k\n"
    ".ends OTHER\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    "Xother1 vother n2 OTHER\n"
    "Rf vminus vout 10k\n"
    ".end\n"
)
FOCUS_TEST_SPEC = SimpleNamespace(
    circuit_name="fake",
    testbenches=[
        SimpleNamespace(
            name="ac_loop_gain",
            criteria=[SimpleNamespace(name="gain", measurement="gain")],
            control_block=".control\nmeas ac gain find v(vother)\n.endc\n",
        )
    ],
        fragments=None)
FOCUS_TEST_SPEC.canonical = FOCUS_TEST_SPEC.testbenches[0]


@pytest.mark.asyncio
async def test_a_rejected_attempt_puts_its_block_into_focus(tmp_path):
    """어느 변형을 잡는가: 거부 항목을 touched_refdes에서 빼는 구현.
    튜너에게 "이 블록에서 거부당했다"고 말하면서 그 블록을 접어서 보여 주는
    것은, verify_pre에 접힌 덱을 주면서 "덱에 없는 것은 거부하라"고
    지시했던 것과 같은 모양이다."""
    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        return one_change("AMP.R1", "width", "1k", "2k")  # param 게이트가 막는다

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": FOCUS_TEST_NETLIST}, FOCUS_TEST_SPEC, state, agents)

    events = [json.loads(line) for line in open(state.history_path)]
    focus_events = [e for e in events if e["step"] == "focus"]

    # FAKE_SPEC의 테스트벤치는 criteria가 비어 있어 measurement_by_criterion이
    # 항상 비고, failing_nets도 항상 비어 select_focus가 씨앗을 하나도 못
    # 잡는다 - 그 경우 select_focus는 "모르면 침묵" 대신 "전 블록 노출"로
    # 폴백하므로(구조_view.select_focus의 마지막 줄 `return focus or
    # definitions`), AMP는 이미 focus_events[0]에도 들어 있다. 그래서 실측
    # 결과는 이 자산이 애초에 서지 않는다는 것이었다: 두 assert 모두 원래
    # 문안대로는 통과할 수 없다(실행 로그로 확인). 이는 select_focus의
    # 버그가 아니라 - 그 폴백은 의도된 것이고 다른 테스트가 지킨다 - 이
    # 픽스처가 "씨앗이 이미 있다"는 가정과 충돌한다는 뜻이다.
    #
    # 같은 성질(거부된 refdes가 touched_refdes에 들어가 다음 focus를
    # 끌어온다)을 다른 경로로 검사한다: 씨앗이 진짜로 OTHER 하나만 잡히도록
    # 두 번째 블록과 그 블록을 가리키는 measurement를 가진 spec을 쓴다. 그러면
    # iteration 1의 focus는 폴백 없이 {OTHER}뿐이고, AMP는 거부를 통해서만
    # 들어온다.
    assert focus_events[0]["blocks"] == ["OTHER"]   # 아직 아무것도 안 건드렸다 - 폴백 아님
    assert "AMP" in focus_events[1]["blocks"]       # 거부가 초점을 끌어왔다


# --- 감사 2.7: OSError가 run_orchestration을 그대로 뚫는다 --------------------
#
# CLAUDE.md는 `OSError`를 최적화 단계에만 넣은 이유를 "`run_orchestration`은
# 디스크를 되읽지 않으므로 그런 경우가 없다"로 적었다. 코드는 반대다 -
# `orchestrator.py`는 **매 외부 이터레이션 머리**에서
# `state.current_netlist_texts()`를 부르고, 그것이 `state.py`에서
# `open(path).read()`를 한다. 가드를 좁힌 근거가 성립하지 않는다.


def _rollback_that_loses_the_deck(state, broken):
    """롤백으로 v0로 되돌아간 **직후** 그 파일이 사라지는 상황(tmp reaper,
    NFS 재연결). 이 지점과 다음 이터레이션 머리의 되읽기 사이에는 로그 호출이
    없으므로, 뒤이어 터지는 것은 정확히 `current_netlist_texts`의 OSError다."""
    real_rollback = state.rollback

    def rollback():
        paths = real_rollback()
        for path in paths.values():
            os.remove(path)
        broken["disk"] = True
        return paths

    return rollback


@pytest.mark.asyncio
async def test_a_vanished_netlist_version_is_a_clean_fail_not_an_uncaught_crash(tmp_path):
    """**어떤 변형을 잡는가**: `except`에서 `OSError`를 빼는 변형. 그 상태에서는
    예외가 `run_orchestration`을 뚫고 나가 `_final_result`가 돌지 않고,
    `cli.main()`의 `write_result_json`/`write_report_md`에 도달하지 못해
    `result.json`도 `report.md`도 써지지 않는다 - 최적화 단계가 크래시해서
    산출물이 통째로 사라졌던 사건과 같은 모양이다.
    """
    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=lambda prev, new, changes: _async(
            {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no progress"}
        ),
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.rollback = _rollback_that_loses_the_deck(state, {"disk": False})

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    # 결과는 여전히 자기가 낸 덱을 설명해야 한다.
    assert result["final_netlist_paths"] == state.current_netlist_paths()
    assert result["topology_swaps"] == []
    # 사유가 "에이전트가 실패했다"나 "넷리스트 적용이 실패했다"로 읽히면 안
    # 된다 - 실행이 자기 덱을 **읽지** 못한 것이다.
    assert "could not read" in result["failure_reason"]
    assert "netlist_v0_ac_loop_gain.cir" in result["failure_reason"]

    # 그리고 이 가드가 사는 이유 그 자체: `cli.main()`이 하는 일을 여기서
    # 그대로 해 본다. 예외가 새면 이 두 줄에 도달조차 못 한다.
    from analogcoder.report import write_report_md, write_result_json

    assert os.path.exists(write_result_json(str(tmp_path), result))
    assert os.path.exists(write_report_md(str(tmp_path), result))


@pytest.mark.asyncio
async def test_the_oserror_handler_does_not_write_to_the_same_broken_disk(tmp_path):
    """핸들러가 `state.log_event`를 부르면 같은 디스크 문제로 핸들러가 다시
    터진다 - 그러면 가드가 있으나 마나다. `_final_result`는 메모리 안의
    `netlist_versions`만 읽으므로 안전하고, 이 테스트는 그 성질을 못박는다.

    **어떤 변형을 잡는가**: except 절 안에 `state.log_event(...)`를 넣는 변형
    (사유를 이력에 남기고 싶은 자연스러운 충동이다).
    """
    broken = {"disk": False}
    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=lambda prev, new, changes: _async(
            {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no progress"}
        ),
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.rollback = _rollback_that_loses_the_deck(state, broken)

    real_log_event = state.log_event

    def log_event(step, data):
        if broken["disk"]:
            raise OSError(28, "No space left on device")
        real_log_event(step, data)

    state.log_event = log_event

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert "could not read" in result["failure_reason"]


# ------------------- 제안이 어떻게 끝났는지가 결과 산출물에 남는다


@pytest.mark.asyncio
async def test_the_result_says_how_this_run_s_proposals_ended(tmp_path):
    """감사 §3.8. 이 집계는 `history.jsonl`에만 있었고 `result.json`에는
    없었다. 그래서 **모든 제안이 면적 게이트에 막혀 덱이 한 번도 안 바뀐
    실행**과 **제안이 대부분 채택된 실행**의 `result.json`이 구조적으로
    동일했다.

    거짓을 말한 것은 아니고 생략한 것이다. 그러나 D1의 교훈이 정확히
    "이 지표가 다른 답을 낼 조건이 이 런에 있었는가를 **런 자신이** 답할 수
    있어야 한다"이다 - D1 측정이 무효였던 이유가 기준선 런의 실패 이벤트가
    0건이었다는 것이고, 그 사실은 `history.jsonl`을 따로 파야만 나왔다."""

    async def tune(structure_view, judge_result, attempts_view, rejection_feedback, netlist_view):
        return one_change("Znope", "value", "1k", "2k")  # 항상 refdes 게이트에 막힌다

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    summary = result["attempt_summary"]
    assert summary["by_outcome"]["kept"] == 0
    assert summary["by_outcome"]["rolled_back"] == 0
    assert summary["by_outcome"]["rejected"] > 0
    assert summary["rejected_by_reason"]["refdes"] == summary["by_outcome"]["rejected"]


@pytest.mark.asyncio
async def test_a_run_that_never_proposed_anything_still_carries_the_summary(tmp_path):
    """**0으로 채운 dict로 항상 실린다.** 이것이 이 수정의 요점이다 - 조건부로
    실으면 "실패가 한 번도 없었다"와 "집계가 사라졌다"가 같은 부재가 되고,
    그것이 D1 측정을 무효로 만든 바로 그 모양이다. `topology_swaps`가 항상
    실리는 것과 같은 규칙이다."""
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, make_agents())

    assert result["status"] == "PASS"
    assert result["attempt_summary"] == {
        "changes": 0,
        "by_outcome": {"kept": 0, "rolled_back": 0, "rejected": 0},
        "rejected_by_reason": {
            "area": 0, "refdes": 0, "param": 0, "stimulus": 0, "verify_pre": 0
        },
    }


@pytest.mark.asyncio
async def test_a_kept_change_and_a_rollback_land_in_different_boxes(tmp_path):
    """세 결과가 서로 다른 칸에 센다. `rejected_by_reason`의 합은
    `by_outcome["rejected"]`와 같아야 한다 - 게이트는 제안 **전체**를
    거부하므로 변경 하나마다 항목이 하나다."""
    verdicts = iter([FAIL_JUDGE, FAIL_JUDGE, PASS_JUDGE])

    agents = make_agents(judge=lambda m, s: _async(next(verdicts, PASS_JUDGE)))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    summary = result["attempt_summary"]
    assert sum(summary["rejected_by_reason"].values()) == summary["by_outcome"]["rejected"]
    assert sum(summary["by_outcome"].values()) == summary["changes"]


# --- 재시도 예산 계측 (`tuning_retries`) -------------------------------------
#
# 이 세 테스트가 붙어 있는 이유: `if approved_proposal is None:` 아래의
# 하드 FAIL 분기는 **의도된 비대칭**이고 바뀌지 않는다. 바뀐 것은 그 분기가
# 얼마나 근접했는지가 로그에 전혀 안 남았다는 것뿐이다(기록된 15개 런 중
# 발화 0건). 그래서 계측이 **동작을 안 바꿨다**는 것도 같은 테스트가 단언한다.

# M6/M7 두 소자 - 제안 하나에 변경이 **여럿**인 경우를 만들 수 있어야
# `by_reason`의 과다 계수 결함(_record_rejected가 변경 개수만큼 Attempt를
# 넣는다)이 드러난다.
TWO_DEVICE_AREA_NETLIST = (
    "* test\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    "M7 vout outB vdd vdd PMOSG W=20u L=1u\n"
    ".end\n"
)

ALL_REASONS_ZERO = {"area": 0, "refdes": 0, "param": 0, "stimulus": 0, "verify_pre": 0}


def _retry_events(state):
    events = [json.loads(line) for line in open(state.history_path)]
    return [e for e in events if e["step"] == "tuning_retries"]


@pytest.mark.asyncio
async def test_tuning_retries_is_logged_even_when_the_first_retry_is_approved(tmp_path):
    """**게이트가 아무것도 안 할 때의 로그가 이것이다.** 승인만 있고 실패가
    0건인 이터레이션에도 이벤트가 나와야 "예산을 안 썼다"와 "계측이
    사라졌다"가 구별된다. 다섯 사유 키는 0이어도 **전부** 실린다."""
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    events = _retry_events(state)
    assert len(events) == 1
    event = events[0]
    assert event["outer_iter"] == 1
    assert event["outcome"] == "approved"
    assert event["approved_retry"] == 1
    assert event["failures"] == 0
    assert event["max_retries"] == 3
    assert event["headroom"] == 3
    assert event["verify_pre_rejected"] is False
    assert event["by_reason"] == ALL_REASONS_ZERO


@pytest.mark.asyncio
async def test_a_gate_rejection_and_a_verify_pre_rejection_are_both_counted_once(tmp_path):
    """실측된 `r1:area r2:verify_pre r3:OK` 모양 그대로. 소진은 게이트 거부와
    verify_pre 거부가 **섞여서** 일어나므로, verify_pre 거부만 세면 근접도를
    과소평가한다.

    r2의 제안은 변경이 **둘**이다 - `_record_rejected`는 변경 개수만큼
    `Attempt`를 넣으므로 그냥 세면 `verify_pre`가 2로 나온다. 사유별 개수는
    서로 다른 `(retry, reason)` 쌍의 개수여야 한다."""
    tune_calls = {"n": 0}

    async def mixed_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        tune_calls["n"] += 1
        if tune_calls["n"] == 1:
            # r1 은 **게이트** 거부여야 한다. 예전에는 면적(40u -> 100u, 2.5x)을
            # 썼는데 2026-08-05 강등으로 그 게이트가 거부하지 않으므로 refdes 로
            # 옮겼다 - 실측된 `r1:<게이트> r2:verify_pre r3:OK` 모양은 그대로다.
            changes = [
                {"refdes": "Znope", "param": "value", "old_value": "1k", "new_value": "2k", "reasoning": "x"}
            ]
        elif tune_calls["n"] == 2:
            changes = [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "50u", "reasoning": "x"},
                {"refdes": "M7", "param": "W", "old_value": "20u", "new_value": "22u", "reasoning": "x"},
            ]
        else:
            changes = [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "45u", "reasoning": "x"}
            ]
        return {"proposed_changes": changes, "overall_reasoning": "x", "confidence": 90}

    async def reject_second_retry_only(structure_view, judge_result, proposal, netlist_view):
        approved = len(proposal["proposed_changes"]) == 1
        return {"approved": approved, "concerns": [], "feedback": "try again"}

    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(
        judge=judge_fails_then_passes, tune=mixed_tune, verify_pre=reject_second_retry_only
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": TWO_DEVICE_AREA_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    events = _retry_events(state)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "approved"
    assert event["approved_retry"] == 3
    assert event["failures"] == 2
    assert event["headroom"] == 1
    # 소진되지 않았어도 싣는다 - 이 불리언이 소진 시 하드FAIL이냐
    # 에스컬레이션이냐를 가르므로, 여유가 줄고 있는 이터레이션을 읽는 사람이
    # 어느 쪽으로 떨어질지 알아야 한다.
    assert event["verify_pre_rejected"] is True
    assert event["by_reason"] == {**ALL_REASONS_ZERO, "refdes": 1, "verify_pre": 1}
    assert sum(event["by_reason"].values()) == event["failures"]


@pytest.mark.asyncio
async def test_exhaustion_by_verify_pre_is_labelled_hard_fail_and_still_hard_fails(tmp_path):
    """소진 두 갈래 중 verify_pre를 안고 있는 쪽. **동작은 그대로다** -
    계측이 하드 FAIL을 안 바꿨다는 증거가 아래 두 줄이다."""
    async def right_sized_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "45u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    async def always_reject(structure_view, judge_result, proposal, netlist_view):
        return {"approved": False, "concerns": ["no"], "feedback": "try again"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE), tune=right_sized_tune, verify_pre=always_reject
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": TWO_DEVICE_AREA_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"

    events = _retry_events(state)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "exhausted_hard_fail"
    assert event["approved_retry"] is None
    assert event["failures"] == 3
    assert event["headroom"] == 0
    assert event["verify_pre_rejected"] is True
    assert event["by_reason"] == {**ALL_REASONS_ZERO, "verify_pre": 3}


@pytest.mark.asyncio
async def test_exhaustion_by_gate_rejections_alone_is_labelled_escalate_and_the_run_continues(tmp_path):
    """같은 소진, 다른 갈래. 게이트 거부만으로 소진되면 하드 FAIL이 아니라
    `consecutive_rollbacks` 증가 - 즉 토폴로지 에스컬레이션 쪽이다. 실행이
    "tuning proposal repeatedly rejected"로 끝나지 **않는** 것이 그 증거다."""
    async def blocked_tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        # 변경 **둘**을 한 제안에 담는다 - 사유별 개수가 변경 개수가 아니라
        # 서로 다른 `(retry, reason)` 쌍의 개수여야 한다는 것이 요점이다.
        # 면적 게이트를 쓰던 것을 2026-08-05 강등과 함께 refdes 로 옮겼다.
        return {
            "proposed_changes": [
                {"refdes": "Znope", "param": "value", "old_value": "1k", "new_value": "2k", "reasoning": "x"},
                {"refdes": "Zalso", "param": "value", "old_value": "1k", "new_value": "2k", "reasoning": "x"},
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=blocked_tune)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"

    events = _retry_events(state)
    assert len(events) == 10  # 하드 FAIL로 끊기지 않고 예산 전부를 돌았다
    for event in events:
        assert event["outcome"] == "exhausted_escalate"
        assert event["approved_retry"] is None
        assert event["failures"] == 3
        assert event["headroom"] == 0
        assert event["verify_pre_rejected"] is False
        # 제안 하나에 변경이 둘인데도 사유별 개수는 재시도 수와 같다.
        assert event["by_reason"] == {**ALL_REASONS_ZERO, "refdes": 3}


# --- 대안 정렬 (2026-08-05, 2단계 Task 5) -------------------------------------

TWO_KNOB_NETLIST = "* netlist\nRf vminus vout 10k\nRg vplus 0 5k\n.end\n"


def _judge_fail_then_pass():
    """첫 판정만 FAIL. 그 뒤(선별 후보들과 튜닝 후 판정)는 전부 PASS 이므로
    루프가 한 이터레이션에 착지한다."""
    calls = {"n": 0}

    async def judge(measurements, spec):
        calls["n"] += 1
        return FAIL_JUDGE if calls["n"] == 1 else PASS_JUDGE

    return judge



def _alt_proposal(*specs):
    """`specs`는 `(refdes, new_value)` 튜플들. 첫 번째가 1차 제안이다."""
    (r0, v0), *rest = specs
    return {
        "proposed_changes": [
            {"refdes": r0, "param": "value", "old_value": "10k",
             "new_value": v0, "reasoning": "p"}
        ],
        "alternatives": [
            {"changes": [{"refdes": r, "param": "value", "old_value": "10k",
                          "new_value": v, "reasoning": "a"}], "reasoning": "a"}
            for r, v in rest
        ],
        "overall_reasoning": "x",
        "confidence": 90,
    }


def _alt_events(state):
    return [
        json.loads(line)
        for line in open(state.history_path)
        if json.loads(line)["step"] == "tuning_alternatives"
    ]


@pytest.mark.asyncio
async def test_the_alternatives_event_is_written_even_when_the_tuner_offers_one(tmp_path):
    """"이 분기가 아무것도 안 할 때 로그가 어떻게 보이는가" - 이 저장소의 첫
    번째 상비 질문이다. 발화 0을 읽으려면 분모가 로그에 있어야 하므로 대안이
    하나뿐인 재시도에도 이벤트가 나간다."""
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    events = _alt_events(state)
    assert events
    first = events[0]
    assert first["offered"] == 1
    assert first["dropped_over_cap"] == 0
    assert first["survived_gates"] == 1
    assert first["survived_verify_pre"] == 1
    # 고를 것이 없으므로 재지 않는다 - 오늘 1회인 시뮬이 2회가 되면 안 된다.
    assert first["screened"] is False
    assert first["simulated"] == 0
    assert first["passing_count"] is None
    assert first["rule"] is None
    assert first["multi_pass_branch_fired"] is False
    assert first["winner_source"] == "primary"


@pytest.mark.asyncio
async def test_a_single_alternative_never_reaches_the_screening_simulator(tmp_path):
    """대안이 없으면 오늘 동작과 바이트 동일해야 한다. 선별 시뮬레이터가 한
    번도 안 불리는 것이 그 증거다."""
    screen_calls = {"n": 0}

    async def screen(netlist_texts, spec):
        screen_calls["n"] += 1
        return {"measurements": {"gain_db": 20.0}, "status": "success"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE))
    agents.screen_simulate = screen
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": BASE_NETLIST}, FAKE_SPEC, state, agents)

    assert screen_calls["n"] == 0


@pytest.mark.asyncio
async def test_one_alternative_failing_a_hard_gate_drops_only_that_alternative(tmp_path):
    """오늘은 게이트에 걸린 제안 하나가 재시도를 통째로 태운다. 대안별로
    돌면 걸린 것만 버린다."""
    async def tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        # 두 번째가 없는 refdes - refdes 게이트가 그것만 버려야 한다.
        return _alt_proposal(("Rf", "11k"), ("Znope", "12k"), ("Rg", "13k"))

    async def screen(netlist_texts, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), tune=tune)
    agents.screen_simulate = screen
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": TWO_KNOB_NETLIST}, FAKE_SPEC, state, agents)

    first = _alt_events(state)[0]
    assert first["offered"] == 3
    assert first["survived_gates"] == 2
    assert first["survived_verify_pre"] == 2
    assert first["screened"] is True


@pytest.mark.asyncio
async def test_verify_pre_runs_on_every_survivor_before_any_screening_simulation(tmp_path):
    """측정값으로 고르면 `Cload` 축소 같은 치팅이 1등을 한다. verify_pre 는
    재기 **전에** 있어야 한다."""
    order = []

    async def tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return _alt_proposal(("Rf", "11k"), ("Rg", "6k"))

    async def verify_pre(structure_view, judge_result, proposal, netlist_view):
        order.append("verify_pre")
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def screen(netlist_texts, spec):
        order.append("screen")
        return {"measurements": {"gain_db": 20.0}, "status": "success"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE), tune=tune, verify_pre=verify_pre
    )
    agents.screen_simulate = screen
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": TWO_KNOB_NETLIST}, FAKE_SPEC, state, agents)

    # 첫 재시도의 앞 네 항목: verify_pre 둘이 screen 둘보다 **모두** 앞선다.
    assert order[:4] == ["verify_pre", "verify_pre", "screen", "screen"]


@pytest.mark.asyncio
async def test_when_two_alternatives_pass_the_smaller_area_wins_end_to_end(tmp_path):
    """선택 규칙의 면적 분기가 오케스트레이터를 통해 실제로 착지점을 바꾼다.
    `Rg` 를 5k -> 1k 로 줄이는 쪽이 면적이 작으므로 이긴다."""
    async def tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return {
            "proposed_changes": [
                {"refdes": "Rf", "param": "value", "old_value": "10k",
                 "new_value": "99k", "reasoning": "크게"}
            ],
            "alternatives": [
                {"changes": [{"refdes": "Rg", "param": "value", "old_value": "5k",
                              "new_value": "1k", "reasoning": "작게"}], "reasoning": "a"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    async def screen(netlist_texts, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success"}

    # 선별 판정은 둘 다 통과, 최종 judge 도 통과시켜 실행을 끝낸다.
    agents = make_agents(judge=_judge_fail_then_pass(), tune=tune)
    agents.screen_simulate = screen
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": TWO_KNOB_NETLIST}, FAKE_SPEC, state, agents)

    first = _alt_events(state)[0]
    assert first["screened"] is True
    assert first["simulated"] == 2


@pytest.mark.asyncio
async def test_verify_post_runs_once_on_the_winner_only(tmp_path):
    """승자에 대해서만 돈다 - 대안마다 돌면 LLM 호출이 대안 수만큼 는다."""
    calls = {"n": 0}

    async def tune(structure_view, judge_result, history, rejection_feedback, netlist_view):
        return _alt_proposal(("Rf", "11k"), ("Rg", "6k"))

    async def screen(netlist_texts, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success"}

    async def verify_post(prev_judge, new_judge, applied_changes):
        calls["n"] += 1
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    agents = make_agents(
        judge=_judge_fail_then_pass(), tune=tune, verify_post=verify_post
    )
    agents.screen_simulate = screen
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": TWO_KNOB_NETLIST}, FAKE_SPEC, state, agents)

    assert calls["n"] == 1
