from pathlib import Path
from types import SimpleNamespace

import pytest

from analogcoder.curation import (
    Candidate,
    Slot,
    StageResult,
    check_structure,
    reproduce_characteristics,
)
from analogcoder.simulators.base import RawSimResult
from analogcoder.spec import Criterion

# --- fixtures shared across this file ---------------------------------------

# A minimal deck defining one block with two ports. Used by check_structure
# tests, where the actual device content doesn't matter - only ports/models/
# scale as compatible_swaps reads them.
DECK_TWO_PORT = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
.ends BLOCK
.end
"""


def _dummy_slot(block_path: str = "BLOCK") -> Slot:
    """A Slot whose `spec` field is never read by check_structure (it only
    consults slot.block_path), so a bare stand-in is enough there."""
    return Slot(spec=SimpleNamespace(testbenches=[], all_criteria=[]), spec_dir=Path("."), block_path=block_path)


def test_structure_failure_carries_the_swap_rejection_reason_verbatim():
    """The candidate requires port 'c', which BLOCK does not declare - a
    'ports' rejection from compatible_swaps. check_structure must not
    invent its own wording; it must carry over exactly what
    compatible_swaps itself produced for this (block, topology) pair."""
    from analogcoder.topologies import Topology
    from analogcoder.topology_match import compatible_swaps

    candidate = Candidate(
        topology_id="cand_extra_port",
        subckt_body="R2 a b 2k\n",
        ports=["a", "b", "c"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _dummy_slot()

    # Compute the expected rejection the same way check_structure does
    # internally, so this test asserts "verbatim carry-over", not a
    # hardcoded wording that would break on an unrelated message tweak.
    library = {
        candidate.topology_id: Topology(
            id=candidate.topology_id,
            description="",
            subckt_body=candidate.subckt_body,
            addresses=[],
            ports=candidate.ports,
            assumes_scale=candidate.assumes_scale,
            provenance=candidate.provenance,
            verified_at="nominal",
        )
    }
    _, expected_rejections = compatible_swaps(netlist_texts, library, set())
    expected = next(
        r for r in expected_rejections if r.block_path == "BLOCK" and r.topology_id == "cand_extra_port"
    )
    assert expected.reason == "ports"

    result = check_structure(candidate, slot, netlist_texts)

    assert result.status == "fail"
    assert result.detail["reason"] == expected.reason
    assert result.detail["detail"] == expected.detail


def test_a_structurally_compatible_candidate_passes_structure_check():
    """Sanity/positive path: a candidate whose ports/models/scale are all
    compatible with the block, and whose body differs from the block's
    current body, must be admitted as a structure-check pass. Without this,
    a check_structure that always returns "fail" would still pass the
    verbatim-rejection test above."""
    candidate = Candidate(
        topology_id="cand_ok",
        subckt_body="R2 a b 2k\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _dummy_slot()

    result = check_structure(candidate, slot, netlist_texts)

    assert result.status == "pass"
    assert result.detail["block_path"] == "BLOCK"
    assert result.detail["topology_id"] == "cand_ok"


# X1 instantiates model MODELX (a non-numeric positional value, so
# topology_match._is_model_name reads it as a model name rather than a node).
DECK_MODELX_SCALE_2 = """* t
.option scale=2.0u
.subckt BLOCK a b
X1 a b MODELX
.ends BLOCK
.end
"""

# Same block/ports, but this deck never instantiates MODELX anywhere - it
# fails the "models" check instead (and its own scale is irrelevant, since
# the models check fails first and short-circuits to the next testbench).
DECK_NO_MODELX = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
.ends BLOCK
.end
"""


def test_multiple_matching_rejections_reports_the_first_testbench_in_sorted_order():
    """A slot with two testbenches where the same (block, topology) pair is
    rejected for a DIFFERENT reason in each testbench: tb_a's deck does
    instantiate MODELX (so its models check passes), but its `.option scale`
    is 2.0u against the candidate's 1e-6 assumption, so it fails on scale.
    tb_b's deck never instantiates MODELX at all, so it fails on models
    before scale is ever checked for it.

    compatible_swaps iterates testbenches in `sorted(netlist_texts)` order
    and appends one rejection per testbench as it visits it, so the
    resulting rejections list has tb_a's ("scale") before tb_b's
    ("models") purely because "tb_a" < "tb_b" alphabetically. check_structure
    documents that it reports matching[0] - the first entry in that
    already-ordered list, not an arbitrary pick - and this test pins that
    behaviour along with the fact that nothing is lost: both reasons are
    still visible in detail["rejections"]."""
    candidate = Candidate(
        topology_id="cand_multi",
        subckt_body="X1 a b MODELX\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    netlist_texts = {"tb_a": DECK_MODELX_SCALE_2, "tb_b": DECK_NO_MODELX}
    slot = _dummy_slot()

    result = check_structure(candidate, slot, netlist_texts)

    assert result.status == "fail"
    assert result.detail["reason"] == "scale"
    reasons = {r["reason"] for r in result.detail["rejections"]}
    assert reasons == {"scale", "models"}
    assert len(result.detail["rejections"]) == 2


# --- reproduce_characteristics -----------------------------------------------

DECK_SWAPPABLE = """* t
.option scale=1.0u
.subckt BLOCK a b vdd vss
R1 a b 1k
.ends BLOCK
Xu1 n1 n2 vdd vss BLOCK
.end
"""


def _slot_with_criteria(criteria: list[Criterion]) -> Slot:
    tb = SimpleNamespace(
        name="tb1",
        netlist_path="/dev/null",
        analyses=["op"],
        control_block=".control\nop\n.endc",
        criteria=criteria,
    )
    spec = SimpleNamespace(testbenches=[tb], all_criteria=criteria, canonical=tb)
    return Slot(spec=spec, spec_dir=Path("."), block_path="BLOCK")


def _candidate() -> Candidate:
    return Candidate(
        topology_id="cand",
        subckt_body="R2 a b 2k\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance="authored",
    )


class _SequencedBackend:
    """Returns one canned measurements dict per sim_backend.run() call, in
    call order: reproduce_characteristics simulates the candidate deck
    first, then the baseline deck (single testbench here, so exactly two
    calls)."""

    def __init__(self, measurement_dicts):
        self._queue = list(measurement_dicts)

    def run(self, netlist_path, testbench_config):
        return RawSimResult(status="success", measurements=self._queue.pop(0), raw_log="", warnings=[])


class _ExplodingBackend:
    def run(self, netlist_path, testbench_config):
        raise RuntimeError("ngspice crashed")


def test_a_missing_measurement_fails_reproduction():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=40.0)]
    slot = _slot_with_criteria(criteria)
    # candidate run produces no gain_db at all; baseline does (irrelevant -
    # the requirement is on the candidate side only).
    backend = _SequencedBackend([{}, {"gain_db": 42.0}])

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "fail"
    assert result.detail["missing"] == ["gain"]
    assert addresses == []


def test_a_measurement_present_as_literal_none_is_treated_as_missing():
    """A measurement key that EXISTS but holds None - a simulator reporting
    "no threshold crossing found" as a null rather than omitting the key -
    must be treated the same as a fully absent key. This repo has hit
    exactly this shape (a settling-time criterion produced no value at 14 of
    45 corners). A key-absence-only check (`c.measurement not in
    candidate_measurements`) would miss it, since the key is present here;
    routing the missing-check through judge_tools.evaluate_criteria (which
    reads `measurements.get(...) is None` and fills NaN either way) catches
    it."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=40.0)]
    slot = _slot_with_criteria(criteria)
    backend = _SequencedBackend([{"gain_db": None}, {"gain_db": 42.0}])

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "fail"
    assert result.detail["missing"] == ["gain"]
    assert addresses == []


def test_a_simulator_exception_is_inconclusive_not_a_rejection():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=40.0)]
    slot = _slot_with_criteria(criteria)
    backend = _ExplodingBackend()

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "inconclusive"
    assert result.status != "fail"
    assert addresses == []


def test_addresses_are_measured_from_the_two_runs_not_declared():
    """Two criteria, opposite operator directions, candidate strictly better
    on both. addresses must reflect the actual measured comparison, not any
    hardcoded/declared value - Candidate carries no 'addresses' field at
    all, so the only way this can be right is by comparing the two
    simulated measurement sets."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1000.0),
    ]
    slot = _slot_with_criteria(criteria)
    candidate_measurements = {"gain_db": 50.0, "iq_ua": 100.0}
    baseline_measurements = {"gain_db": 40.0, "iq_ua": 200.0}
    backend = _SequencedBackend([candidate_measurements, baseline_measurements])

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "pass"
    assert set(addresses) == {"gain", "iq"}


def test_a_criterion_that_ties_is_not_an_address():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _slot_with_criteria(criteria)
    backend = _SequencedBackend([{"gain_db": 50.0}, {"gain_db": 50.0}])

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "pass"
    assert addresses == []


def test_the_operator_direction_decides_what_better_means():
    """A '<=' criterion where the candidate's value is SMALLER than the
    baseline's is an improvement - a implementation that hardcodes
    "better means candidate > baseline" would miss this (200 > 250 is
    False) and wrongly leave it out of addresses."""
    criteria = [Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1000.0)]
    slot = _slot_with_criteria(criteria)
    # candidate (simulated first) = 200, baseline = 250 -> candidate is
    # smaller, i.e. better under <=.
    backend = _SequencedBackend([{"iq_ua": 200.0}, {"iq_ua": 250.0}])

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert addresses == ["iq"]


def test_reproduction_detail_is_populated_even_when_it_passes():
    """Even when nothing is "interesting" (every criterion ties, so
    addresses is empty), detail must still carry the measured values and
    simulation count - a mutation that only populates detail behind an
    "if addresses:" (or similar conditional-logging) guard is caught here,
    since detail would otherwise be missing these keys."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _slot_with_criteria(criteria)
    backend = _SequencedBackend([{"gain_db": 50.0}, {"gain_db": 50.0}])

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "pass"
    assert addresses == []
    assert result.detail["candidate_measurements"] == {"gain_db": 50.0}
    assert result.detail["baseline_measurements"] == {"gain_db": 50.0}
    assert result.detail["addresses"] == []
    assert result.detail["simulation_count"] == 2
    assert result.detail["missing"] == []
    assert "criteria" in result.detail and "gain" in result.detail["criteria"]
