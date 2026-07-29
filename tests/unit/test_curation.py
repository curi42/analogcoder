import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area_limits import index_baseline_components
from analogcoder.curation import (
    COMPARISON_REL_TOLERANCE,
    INCUMBENT_POINT_LABEL,
    MAX_VARIANT_AUTHOR_RETRIES,
    Candidate,
    Slot,
    StageResult,
    author_and_verify_variant,
    candidate_from_deck,
    candidate_from_file,
    candidate_from_technique,
    check_structure,
    prompt_available_models,
    reproduce_characteristics,
    scoped_comparison,
    verify_corners,
)
from analogcoder.simulators.base import RawSimResult
from analogcoder.spec import Criterion, PVTCorners

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
    return Slot(spec=SimpleNamespace(testbenches=[], all_criteria=[]), block_path=block_path)


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
        fragments=None)
    spec = SimpleNamespace(testbenches=[tb], all_criteria=criteria, canonical=tb)
    return Slot(spec=spec, block_path="BLOCK")


def _candidate(provenance: str = "authored") -> Candidate:
    return Candidate(
        topology_id="cand",
        subckt_body="R2 a b 2k\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance=provenance,
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


# --- scoped_comparison --------------------------------------------------------

# One tunable knob: R1's positional value (1k = 1000). Baseline 1000 falls in
# RESISTOR_TIERS' second tier (1e3 <= baseline < 10e3, since the check is
# strict "<"), so allowed_multiplier = 2.0 and the sweep range is [500, 2000].
DECK_ONE_KNOB = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
.ends BLOCK
.end
"""

# Two tunable knobs, both plain resistor values, so test_only_one_knob_moves
# can assert that any given simulated deck differs from baseline in exactly
# one of them.
DECK_TWO_KNOBS = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
R2 a b 2k
.ends BLOCK
.end
"""

# Three tunable knobs, sorted order R1 < R2 < R3 - used to distinguish
# knob_names selection from max_knobs truncation: naming {R2, R3} and
# capping to max_knobs=1 must keep R2 (first of the requested set), not R1
# (first of the full set), which only holds if selection is applied before
# the cap.
DECK_THREE_KNOBS = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
R2 a b 2k
R3 a b 3k
.ends BLOCK
.end
"""

# A count-valued knob (m=4 on a bipolar). Not X-prefixed, so _tier_baseline_value
# can't resolve a geometry tier for it (the positional value is a model name,
# not a number) - but m is a count token, so _direct_target still assigns it
# COUNT_ALLOWED_MULTIPLIER (2.0) regardless. Baseline m=4, range [2, 8].
DECK_COUNT_KNOB = """* t
.option scale=1.0u
.subckt BLOCK a b c
Q1 a b c QMOD m=4
.ends BLOCK
.end
"""

# Same shape, but baseline m=1: range [0.5, 2.0]. Rounding alone can produce 0
# (a deleted device, not a tuning within the gate's allowance) - see
# test_count_knobs_never_sweep_below_one.
DECK_COUNT_KNOB_AT_ONE = """* t
.option scale=1.0u
.subckt BLOCK a b c
Q1 a b c QMOD m=1
.ends BLOCK
.end
"""

# Two independent blocks. Only BLOCK's own knob (R1.value) may ever appear in
# scoped_comparison's scope for a slot addressing BLOCK - OTHER's knob must
# never be swept, and since it was never a candidate knob for this slot in
# the first place, it must not appear as "omitted" either (that word is
# reserved for max_knobs truncation of this block's own knobs).
DECK_TWO_BLOCKS = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
.ends BLOCK
.subckt OTHER a b
R9 a b 5k
.ends OTHER
.end
"""


def _scoped_slot(criteria: list[Criterion], netlist_text: str, block_path: str = "BLOCK") -> Slot:
    """Unlike _slot_with_criteria, scoped_comparison also reads
    slot.spec.circuit_name (it derives the block's tunable index via
    structure.derive_structure, which requires a circuit_name label) -
    _slot_with_criteria's bare SimpleNamespace doesn't carry one."""
    tb = SimpleNamespace(
        name="tb1",
        netlist_path="/dev/null",
        analyses=["op"],
        control_block=".control\nop\n.endc",
        criteria=criteria,
        fragments=None)
    spec = SimpleNamespace(
        testbenches=[tb],
        all_criteria=criteria,
        canonical=tb,
        circuit_name="test",
    )
    return Slot(spec=spec, block_path=block_path)


class _ConstantBackend:
    """Every simulated point reports the same fixed measurements, regardless
    of which knob or value was swept - used where the test only cares about
    the *scope* recorded (which knobs, how many points, omissions), not
    about triggering or avoiding domination via realistic circuit values."""

    def __init__(self, measurements: dict):
        self._measurements = dict(measurements)

    def run(self, netlist_path, testbench_config):
        return RawSimResult(status="success", measurements=dict(self._measurements), raw_log="", warnings=[])


class _ValueFunctionBackend:
    """Reads the swept component's own resolved value directly off the deck
    ngspice would actually see (via area_limits.index_baseline_components,
    the same resolver the area gate itself uses - not a hand-rolled regex),
    and reports measurements as a deterministic function of that value. This
    lets a test construct an exact trade-off (e.g. "raising R1 improves gain
    but worsens iq") without needing a real simulator."""

    def __init__(self, refdes: str, param: str, functions: dict):
        self._refdes = refdes
        self._param = param
        self._functions = functions

    def run(self, netlist_path, testbench_config):
        with open(netlist_path) as f:
            text = f.read()
        component = index_baseline_components(text)[self._refdes]
        value = component.resolved_value if self._param == "value" else component.resolved_params[self._param]
        measurements = {name: fn(value) for name, fn in self._functions.items()}
        return RawSimResult(status="success", measurements=measurements, raw_log="", warnings=[])


class _RecordingBackend:
    """Records the full text of every deck it is asked to simulate, so a
    test can inspect exactly what changed relative to the untouched
    baseline. Reports a constant measurement - what happened structurally
    to the deck is what these tests check, not the measured outcome."""

    def __init__(self, measurements: dict):
        self._measurements = dict(measurements)
        self.calls: list[str] = []

    def run(self, netlist_path, testbench_config):
        with open(netlist_path) as f:
            self.calls.append(f.read())
        return RawSimResult(status="success", measurements=dict(self._measurements), raw_log="", warnings=[])


def test_a_single_sweep_point_dominating_every_criterion_rejects():
    """R1's value sweeps to 500 and 2000 (points=2, so neither lands on the
    excluded baseline 1000). At R1=2000: gain_db=2000 (>=1500, beats
    candidate) and iq_ua=1e6/2000=500 (<=700, beats candidate too) - a single
    sweep point at least as good as the candidate on BOTH criteria, which
    must reject (status='fail') and name that point as the dominating one."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1e9),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ValueFunctionBackend(
        refdes="BLOCK.R1",
        param="value",
        functions={"gain_db": lambda v: v, "iq_ua": lambda v: 1_000_000 / v},
    )
    candidate_measurements = {"gain_db": 1500.0, "iq_ua": 700.0}
    # The incumbent as shipped (R1 = 1000, its own baseline) is now a
    # dominance candidate too - here it loses on gain (1000 < 1500), so the
    # rejection below is genuinely the SWEPT point's doing.
    incumbent_measurements = {"gain_db": 1000.0, "iq_ua": 1000.0}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=2,
    )

    assert result.name == "comparison"
    assert result.detail["dominating_point"]["point"] == "swept"
    assert result.status == "fail"
    assert result.detail["dominating_point"] is not None
    assert result.detail["dominating_point"]["knob"] == "BLOCK.R1.value"
    assert result.detail["dominating_point"]["swept_value"] == pytest.approx(2000.0)


def test_a_candidate_winning_one_criterion_survives():
    """파레토다. 모든 기준에서 이겨야 하는 것이 아니다.

    Both gain_db and iq_ua rise together with R1 (a manufactured trade-off:
    raising R1 helps the >= criterion but hurts the <= one). The candidate
    sits exactly between the two swept points (500 and 2000) on both axes, so
    at R1=2000 the candidate still wins on iq (1500 < 2000) and at R1=500 the
    candidate still wins on gain (1500 > 500). No single point is at least as
    good as the candidate on every criterion, so the candidate must survive -
    a rule that instead required beating the candidate on every criterion to
    be REJECTED (inverted admission direction) would still flip this to
    'fail' by accident since it's the same predicate name; the real
    distinguishing mutation is requiring ALL criteria to win for the
    candidate to be admitted, rather than ANY one."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1e9),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ValueFunctionBackend(
        refdes="BLOCK.R1", param="value", functions={"gain_db": lambda v: v, "iq_ua": lambda v: v}
    )
    candidate_measurements = {"gain_db": 1500.0, "iq_ua": 1500.0}
    # The incumbent (R1 = 1000 -> gain 1000, iq 1000) beats the candidate on
    # iq but loses on gain, so it does not dominate either.
    incumbent_measurements = {"gain_db": 1000.0, "iq_ua": 1000.0}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=2,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None


def test_a_sweep_point_with_a_missing_measurement_cannot_dominate():
    """Every sweep point reports a tiny iq_ua (1.0, trivially <= the
    candidate's 1000 - already dominates on its own) but never reports
    gain_db at all. This is deliberately the dangerous direction for a
    missing-value bug: gain_db's operator is '>=', so a mutation that
    defaults a missing actual to +inf (rather than excluding the point)
    would make that criterion look satisfied too (+inf >= anything), and
    combined with iq already dominating, the point would be wrongly
    rejected. (Had iq_ua been the missing one instead, a '<=' criterion
    defaulted to +inf would coincidentally still fail to dominate, hiding
    the bug - the missing criterion's direction has to be the one an
    infinity default favours.) The correct rule excludes any point missing
    a measurement from domination entirely, so nothing here can ever
    reject - the candidate must survive even though the one criterion that
    IS measured looks overwhelmingly dominated."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1e9),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"iq_ua": 1.0})  # gain_db never reported
    candidate_measurements = {"gain_db": 100.0, "iq_ua": 1000.0}
    # The incumbent's own stage-2 run is missing gain_db for the same reason,
    # so the incumbent point is excluded from domination on the same rule.
    incumbent_measurements = {"iq_ua": 1.0}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=2,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None


def test_count_knobs_are_swept_at_integers_only():
    """Q1's m=4 is a count, not a length - COUNT_ALLOWED_MULTIPLIER=2.0 gives
    range [2, 8]. Every swept value must be a whole number, none may repeat,
    and the baseline (4) itself must be excluded.

    points=9 (not 5) is load-bearing for the dedup assertion: with 5 points
    over [2,8] rounding never collides, so `len(values) == len(set(values))`
    would pass even with dedup deleted entirely (it would be vacuously
    true - there'd just be nothing to deduplicate). With 9 points, i=0,1
    both round to 2 and i=2,3 both round to 3 (verified: raw points are
    [2.0, 2.38, 2.83, 3.36, 4.0(baseline), 4.76, 5.66, 6.73, 8.0]) - so the
    baseline-excluded, deduplicated result must be exactly [2,3,5,6,7,8]."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_COUNT_KNOB)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_COUNT_KNOB}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=9
    )

    swept = result.detail["knobs_swept"]
    assert len(swept) == 1
    assert swept[0]["knob"] == "BLOCK.Q1.m"
    values = swept[0]["swept_values"]
    assert all(v == int(v) for v in values)
    assert len(values) == len(set(values))
    assert 4.0 not in values
    assert sorted(values) == [2.0, 3.0, 5.0, 6.0, 7.0, 8.0]


def test_count_knobs_never_sweep_below_one():
    """I1: Q1's m=1 gives range [0.5, 2.0]. Rounding alone can produce 0 (a
    deleted device - a change outside the gate's allowance, not a tuning
    within it, since m=0 isn't a value check_area_growth would ever approve
    either). Every swept value must be >= 1."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_COUNT_KNOB_AT_ONE)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_COUNT_KNOB_AT_ONE}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=5
    )

    swept = result.detail["knobs_swept"]
    assert len(swept) == 1
    values = swept[0]["swept_values"]
    assert values, "expected at least one swept value"
    assert all(v >= 1.0 for v in values)


def test_the_scope_is_always_recorded_even_when_the_candidate_survives():
    """Even when the candidate survives (nothing dominates it), detail must
    still carry the full comparison scope - a mutation that only records
    knobs_swept/simulation_count/best_per_criterion behind an "if rejected"
    guard is caught here."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=100.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    # Constant, always worse than the candidate - nothing can ever dominate.
    backend = _ConstantBackend({"gain_db": 10.0})
    candidate_measurements = {"gain_db": 100.0}
    incumbent_measurements = {"gain_db": 10.0}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=3,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None
    assert len(result.detail["knobs_swept"]) == 1
    assert result.detail["knobs_swept"][0]["knob"] == "BLOCK.R1.value"
    assert result.detail["knobs_swept"][0]["baseline"] == 1000.0
    assert result.detail["simulation_count"] > 0
    assert result.detail["best_per_criterion"]["gain"]["value"] == 10.0


def test_omitted_knobs_are_named_when_max_knobs_truncates():
    """Two tunable knobs exist (BLOCK.R1.value, BLOCK.R2.value); max_knobs=1
    keeps only the first in sorted order and must name the other in
    knobs_omitted rather than silently dropping it."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_TWO_KNOBS)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_TWO_KNOBS}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=1, points=2
    )

    assert len(result.detail["knobs_swept"]) == 1
    assert result.detail["knobs_omitted"] == ["BLOCK.R2.value"]


def test_knob_names_default_none_means_no_named_narrowing_was_requested():
    """The default (knob_names omitted) must record 'no named narrowing was
    requested' as None, distinct from 'a narrowing to zero knobs was
    requested' (an empty list) - collapsing them would misreport an
    ordinary max_knobs-only run (this one has no cap either) as one that was
    deliberately scoped to specific knobs."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_ONE_KNOB}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=2
    )

    assert result.detail["knob_names_requested"] is None


def test_knob_names_restricts_the_sweep_and_records_the_rest_as_omitted():
    """Two tunable knobs exist. Naming just BLOCK.R2.value must sweep ONLY
    that one, name BLOCK.R1.value in knobs_omitted (the same 'don't
    silently drop it' rule test_omitted_knobs_are_named_when_max_knobs_
    truncates pins for max_knobs), and separately record the request itself
    in knob_names_requested - the fact that this run's scope was
    deliberately narrowed to a specific knob must survive independently of
    knobs_swept's length (a max_knobs=1 run reaching the same swept-count
    would look identical in knobs_swept alone)."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_TWO_KNOBS)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_TWO_KNOBS},
        backend,
        {"gain_db": 1.0},
        {"gain_db": 1.0},
        max_knobs=None,
        points=2,
        knob_names=[("BLOCK.R2", "value")],
    )

    assert [k["knob"] for k in result.detail["knobs_swept"]] == ["BLOCK.R2.value"]
    assert result.detail["knobs_omitted"] == ["BLOCK.R1.value"]
    assert result.detail["knob_names_requested"] == ["BLOCK.R2.value"]


def test_a_requested_knob_name_absent_from_the_tunable_index_is_recorded_unresolved():
    """BLOCK.R9.value was never a real knob on this block (a typo or a stale
    name). Silently sweeping nothing here would make that mistake
    indistinguishable from 'we correctly narrowed to zero knobs' - it must
    be named in knobs_unresolved with a reason that says it was explicitly
    requested, distinct from the sweep's own 'unresolved' reasons (baseline
    value / tier)."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        {"gain_db": 1.0},
        {"gain_db": 1.0},
        max_knobs=None,
        points=2,
        knob_names=[("BLOCK.R9", "value")],
    )

    assert result.detail["knobs_swept"] == []
    entry = result.detail["knobs_unresolved"][0]
    assert entry["knob"] == "BLOCK.R9.value"
    assert "explicitly requested" in entry["reason"]


def test_knob_names_selection_is_applied_before_max_knobs_caps():
    """Three tunable knobs exist in sorted order R1 < R2 < R3. Requesting
    {R2, R3} and capping to max_knobs=1 must keep R2 - the first knob of the
    REQUESTED, already-filtered set - not R1, which is only the first knob
    of the FULL set. A mutation that applies max_knobs to the full knob
    list before intersecting with knob_names would instead keep R1 (never
    requested at all) or, if capping ran first and only then filtered,
    could drop the requested knobs entirely - either way this assertion on
    exactly which knob got swept catches it."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_THREE_KNOBS)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_THREE_KNOBS},
        backend,
        {"gain_db": 1.0},
        {"gain_db": 1.0},
        max_knobs=1,
        points=2,
        knob_names=[("BLOCK.R2", "value"), ("BLOCK.R3", "value")],
    )

    assert [k["knob"] for k in result.detail["knobs_swept"]] == ["BLOCK.R2.value"]
    assert "BLOCK.R1.value" in result.detail["knobs_omitted"]
    assert "BLOCK.R3.value" in result.detail["knobs_omitted"]


def test_only_one_knob_moves_per_sweep_point():
    """스윕 지점마다 기준선과 다른 파라미터가 정확히 하나여야 한다.

    Records every deck actually handed to the simulator and checks, against
    the untouched baseline, that exactly one of the two tunable knobs
    (BLOCK.R1.value, BLOCK.R2.value) differs - never zero, never both."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_TWO_KNOBS)
    backend = _RecordingBackend({"gain_db": 1.0})
    baseline = index_baseline_components(DECK_TWO_KNOBS)
    baseline_values = {"BLOCK.R1": baseline["BLOCK.R1"].resolved_value, "BLOCK.R2": baseline["BLOCK.R2"].resolved_value}

    scoped_comparison(
        _candidate(), slot, {"tb1": DECK_TWO_KNOBS}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=3
    )

    assert backend.calls, "expected at least one simulated deck"
    for text in backend.calls:
        components = index_baseline_components(text)
        changed = [key for key, value in baseline_values.items() if components[key].resolved_value != value]
        assert len(changed) == 1


def test_a_tie_between_a_sweep_point_and_the_candidate_counts_as_domination():
    """브리프 규칙 3: 후보 '이상'이면 거부 - 동률도 포함한다. `points=1` gives
    a single swept value (R1's baseline/M = 500, since with only one point
    the log-spaced interpolation degenerates to the low endpoint), chosen to
    equal the candidate's own iq_ua exactly - a tie, not a strict win. If
    domination required strictly beating the candidate (_is_better instead
    of _at_least_as_good), this single point would never dominate and the
    candidate would wrongly survive.

    The criterion is '<=' (lower is better) precisely so the INCUMBENT point
    - R1 at its own baseline 1000, now a dominance candidate in its own right
    - cannot be the one that rejects: 1000 is worse than the candidate's 500
    under '<=', so the rejection below can only come from the swept point at
    500. Under '>=' the incumbent would dominate first and this test would no
    longer be about the tie rule at all."""
    criteria = [Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1e9)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ValueFunctionBackend(refdes="BLOCK.R1", param="value", functions={"iq_ua": lambda v: v})
    candidate_measurements = {"iq_ua": 500.0}
    incumbent_measurements = {"iq_ua": 1000.0}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=1,
    )

    assert result.detail["knobs_swept"][0]["swept_values"] == [pytest.approx(500.0)]
    assert result.status == "fail"
    assert result.detail["dominating_point"]["point"] == "swept"
    assert result.detail["dominating_point"]["swept_value"] == pytest.approx(500.0)


def test_knobs_from_other_blocks_are_not_swept():
    """I3: a second block (OTHER) exists in the deck alongside the slot's own
    BLOCK. OTHER.R9.value must never be swept, and must not be named in
    knobs_omitted either - it was never one of BLOCK's own knobs to begin
    with, so it isn't a truncation, it's simply out of scope. On a
    multi-block deck like benchmarks/bandgap, sweeping another block's knobs
    would let tuning a *different* block "dominate" a candidate for this
    one - the normal case there, not an edge case."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_TWO_BLOCKS, block_path="BLOCK")
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_TWO_BLOCKS}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=2
    )

    swept_knobs = {entry["knob"] for entry in result.detail["knobs_swept"]}
    assert swept_knobs == {"BLOCK.R1.value"}
    assert "OTHER.R9.value" not in swept_knobs
    assert "OTHER.R9.value" not in result.detail["knobs_omitted"]


def test_a_missing_measurement_on_a_lower_bound_criterion_cannot_dominate():
    """I4 - the mirror of test_a_sweep_point_with_a_missing_measurement_cannot_dominate.
    That test used a missing '>=' criterion (the dangerous direction for a
    '+inf' default). This one uses a missing '<=' criterion instead, which is
    the dangerous direction for a '-inf' default: iq_ua (operator '<=') is
    never reported, while gain_db (operator '>=') is always reported at a
    value that already dominates the candidate on its own (999999 >= 100). A
    mutation defaulting a missing actual to -inf would make '-inf <= 1000'
    true for the never-measured iq criterion too, and combined with gain
    already dominating, would wrongly reject a candidate that was never
    actually beaten on iq at all. '<=' criteria are the majority in this
    repo's real specs (iq_ua, psrr_dc, psr_minus_db), so this direction is
    not a corner case."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
        Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1e9),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 999_999.0})  # iq_ua never reported
    candidate_measurements = {"gain_db": 100.0, "iq_ua": 1000.0}
    incumbent_measurements = {"gain_db": 999_999.0}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=2,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None


def test_unresolvable_knobs_are_named_with_a_reason():
    """Minor 4: a component in the tunable index whose baseline value cannot
    be resolved (a named param referencing an identifier declared nowhere in
    the deck) must be named in knobs_unresolved with a reason - not silently
    dropped. Given C1, this is the field that would be a run's only warning
    that the stage examined nothing for a whole class of knobs.

    M1's positional value ("NMOSG") is a model name, not a number, so
    structure.py never adds param="value" to the tunable index for it
    (editing a non-numeric positional token would corrupt the deck) - the
    only tunable entry here is the named param W=wn, and wn is never
    declared anywhere, so resolve_value has nothing to resolve it against."""
    deck = """* t
.option scale=1.0u
.subckt BLOCK a b c d
M1 a b c d NMOSG W=wn
.ends BLOCK
.end
"""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, deck)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(_candidate(), slot, {"tb1": deck}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=3)

    assert result.detail["knobs_swept"] == []
    assert len(result.detail["knobs_unresolved"]) == 1
    entry = result.detail["knobs_unresolved"][0]
    assert entry["knob"] == "BLOCK.M1.W"
    assert entry["reason"]


def test_a_neutral_knob_is_reported_distinctly_from_a_genuinely_unresolved_one():
    """Minor 2: nf (finger count) has no tier because it is structurally
    area-neutral (CLAUDE.md's 'neutral' visibility state - nothing to judge,
    not "couldn't judge"), which is a different fact from a param whose
    baseline genuinely can't be resolved or simply has no tier for another
    reason. Collapsing both into the same reason string ("no size tier")
    would make a reader unable to tell "designed to be unconstrained" apart
    from "the gate is blind here"."""
    deck = """* t
.option scale=1.0u
.subckt BLOCK a b c d
M1 a b c d NMOSG nf=2
.ends BLOCK
.end
"""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, deck)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(_candidate(), slot, {"tb1": deck}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=3)

    assert result.detail["knobs_swept"] == []
    entry = result.detail["knobs_unresolved"][0]
    assert entry["knob"] == "BLOCK.M1.nf"
    assert "neutral" in entry["reason"]
    assert entry["reason"] != "the area gate has no size tier for this parameter"


def test_excluded_points_are_named_with_a_reason():
    """Minor 4: a sweep point whose simulation raises must be named in
    excluded_points with a reason - not silently absorbed into "the
    candidate survived" with no trace of why that point didn't count."""

    class _AlwaysFailsBackend:
        def run(self, netlist_path, testbench_config):
            raise RuntimeError("synthetic sim failure")

    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_ONE_KNOB}, _AlwaysFailsBackend(), {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=2
    )

    assert result.detail["excluded_points"], "expected at least one excluded point"
    entry = result.detail["excluded_points"][0]
    assert entry["knob"] == "BLOCK.R1.value"
    assert "synthetic sim failure" in entry["reason"]


def test_a_non_canonical_testbench_apply_failure_excludes_the_point_not_the_stage():
    """Minor 3: a slot with two testbenches where the block being swept
    exists (unambiguously) in the canonical testbench but the refdes is
    ambiguous in the second testbench's text (two components sharing that
    plain refdes at top level, with no scope to disambiguate) - apply_changes
    raises ValueError there. That must exclude the sweep point (like any
    other simulation failure), not propagate out of scoped_comparison
    entirely and kill the whole stage."""
    canonical_text = DECK_ONE_KNOB
    # A second testbench deck where the SAME qualified refdes "BLOCK.R1" is
    # ambiguous: BLOCK exists here too, but with two components both named
    # R1 in its body, so a scoped lookup restricted to "BLOCK" still matches
    # more than one physical line.
    ambiguous_text = """* t2
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
R1 a b 2k
.ends BLOCK
.end
"""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    tb1 = SimpleNamespace(
        name="tb1", netlist_path="/dev/null", analyses=["op"], control_block=".control\nop\n.endc", criteria=criteria,
        fragments=None)
    tb2 = SimpleNamespace(
        name="tb2", netlist_path="/dev/null", analyses=["op"], control_block=".control\nop\n.endc", criteria=[],
        fragments=None)
    spec = SimpleNamespace(testbenches=[tb1, tb2], all_criteria=criteria, canonical=tb1, circuit_name="test")
    slot = Slot(spec=spec, block_path="BLOCK")
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": canonical_text, "tb2": ambiguous_text},
        backend,
        {"gain_db": 1.0},
        {"gain_db": 1.0},
        max_knobs=None,
        points=2,
    )

    assert result.status in ("pass", "fail")  # did not raise
    assert result.detail["excluded_points"], "expected the ambiguous testbench to exclude every point"


def test_the_reported_range_matches_the_first_and_last_swept_values_exactly():
    """Minor 5: math.exp(math.log(low)) can reintroduce float noise at the
    endpoints (e.g. 500 -> 499.99999999999983), so a naive implementation's
    swept_values[0] disagrees with range[0] even though both are supposed to
    be the same number, baseline/allowed_multiplier. They must match
    exactly, not just approximately, because a reader comparing "the range I
    said I'd sweep" against "the values I actually swept" should never see
    17 significant digits of noise."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(), slot, {"tb1": DECK_ONE_KNOB}, backend, {"gain_db": 1.0}, {"gain_db": 1.0}, max_knobs=None, points=3
    )

    knob = result.detail["knobs_swept"][0]
    assert knob["swept_values"][0] == knob["range"][0]
    assert knob["swept_values"][-1] == knob["range"][1]


def test_the_incumbent_as_shipped_is_itself_a_dominance_candidate():
    """C1, measured: a candidate strictly WORSE than doing nothing used to be
    ADMITted. `_sweep_values` drops the baseline point (stage 2 already
    measured it), but stage 2's measurement of that point was never handed to
    this stage, so the zero-tuning point - the cheapest tuning that exists,
    trivially inside any area allowance - was the one point the gate refused
    to consider. The reviewer's real run:

        gain (>=): candidate 5.0, incumbent 10.0, better=False
        knob swept [500, 2000] -> 1.5 at both points
        VERDICT: ADMIT   addresses: []

    Reproduced exactly here (constant backend at 1.5, so neither swept point
    can dominate). The incumbent at 10.0 does dominate, so this must reject -
    and name the incumbent, not a swept knob.

    Mutation this catches: dropping the incumbent from the dominance scan
    (i.e. the pre-fix behaviour) makes this 'pass'."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 1.5})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        {"gain_db": 5.0},
        {"gain_db": 10.0},
        max_knobs=None,
        points=2,
    )

    assert result.status == "fail"
    dominating = result.detail["dominating_point"]
    assert dominating is not None
    # Distinguishable from a swept point, per C1's requirement.
    assert dominating["point"] == "incumbent"
    assert dominating["knob"] is None
    assert dominating["swept_value"] is None
    assert dominating["label"] == INCUMBENT_POINT_LABEL
    assert dominating["measurements"] == {"gain_db": 10.0}


def test_the_incumbent_point_is_recorded_in_the_scope_and_costs_no_simulation():
    """The incumbent point must appear in the recorded scope like any other
    point (C1: "It must appear in the recorded scope"), including when it does
    NOT dominate - otherwise a reader cannot tell "the zero-tuning point was
    considered and lost" from "it was never considered". It must also be
    marked as not simulated here: its numbers come from stage 2's own baseline
    run, and counting it in simulation_count would overstate what this stage
    cost. One knob, points=2 -> exactly 2 swept simulations and no more.

    Mutation this catches: re-simulating the unchanged deck (simulation_count
    becomes 3), or recording the incumbent as just another swept entry
    (knobs_swept grows to 2 / incumbent_point missing)."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        {"gain_db": 100.0},  # candidate beats everything - nothing dominates
        {"gain_db": 2.0},
        max_knobs=None,
        points=2,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None
    incumbent = result.detail["incumbent_point"]
    assert incumbent["point"] == "incumbent"
    assert incumbent["label"] == INCUMBENT_POINT_LABEL
    assert incumbent["measurements"] == {"gain_db": 2.0}
    assert incumbent["simulated_here"] is False
    assert result.detail["simulation_count"] == 2
    assert len(result.detail["knobs_swept"]) == 1


def test_a_difference_inside_the_relative_tolerance_does_not_block_domination():
    """C2, measured on the shipped slot: with a zero-tolerance Pareto rule two
    criteria decoupled from the swept knob blocked domination at every point -
    core_phase_margin short by 0.0011 deg on 66 (1.7e-5 relative) and
    trim_loop_gain short by 0.0001 dB on 87.5 - while the real trade-off on
    the same run was +8.3 deg on 81 (0.102 relative). This test reproduces
    that exact shape at exact numbers: the swept point is a hair BELOW the
    candidate on `pm` (4.2e-5 relative, the largest noise the reviewer
    measured) and far above it on `trim_pm`.

    Mutation this catches: COMPARISON_REL_TOLERANCE = 0, dropping the band
    from _at_least_as_good, or making the band ABSOLUTE (`band = tolerance`,
    i.e. 0.001 of a degree here) instead of relative - each makes this 'pass',
    restoring the bug where solver noise rescues a candidate."""
    criteria = [
        Criterion(name="pm", measurement="core_pm_deg", operator=">=", threshold=0.0),
        Criterion(name="trim_pm", measurement="trim_pm_deg", operator=">=", threshold=0.0),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"core_pm_deg": 66.08070, "trim_pm_deg": 99.90330})
    candidate_measurements = {"core_pm_deg": 66.08350, "trim_pm_deg": 89.42130}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        {"core_pm_deg": 66.08070, "trim_pm_deg": 81.13820},  # incumbent loses on trim_pm
        max_knobs=None,
        points=2,
    )

    assert result.detail["tolerance"] == COMPARISON_REL_TOLERANCE
    assert result.status == "fail"
    assert result.detail["dominating_point"]["point"] == "swept"


def test_a_difference_larger_than_the_relative_tolerance_still_blocks_domination():
    """The other side of the same band: the tolerance must not swallow a real
    difference. Same shape as the test above, but the swept point is short by
    2% on `pm` (20x the tolerance) instead of 4.2e-5 - a real regression on
    that axis, so the candidate survives.

    Mutation this catches: a tolerance widened to (say) 0.1 - it would call
    this 2% regression a tie and wrongly reject a candidate that genuinely
    beats every swept point on one axis."""
    criteria = [
        Criterion(name="pm", measurement="core_pm_deg", operator=">=", threshold=0.0),
        Criterion(name="trim_pm", measurement="trim_pm_deg", operator=">=", threshold=0.0),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"core_pm_deg": 64.76, "trim_pm_deg": 99.90330})
    candidate_measurements = {"core_pm_deg": 66.08350, "trim_pm_deg": 89.42130}

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        candidate_measurements,
        {"core_pm_deg": 64.76, "trim_pm_deg": 81.13820},
        max_knobs=None,
        points=2,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None


def test_a_criterion_with_an_equality_operator_can_still_be_dominated():
    """I6: '==' is legal in this repo's spec language (judge_tools._OPERATORS).
    `_is_better` returning False for it is correct - there is no improvement
    direction - but `_at_least_as_good` returning False fails OPEN: no point
    can then ever be "at least as good" on that criterion, so no point can
    ever dominate, every candidate passes stage 3 for free, and nothing in
    knobs_unresolved/excluded_points/any field says so. Here the swept point
    matches the candidate exactly on the '==' criterion and beats it on the
    other, so it must dominate.

    Mutation this catches: `return False` for '==' in _at_least_as_good makes
    this 'pass' - a stage structurally unable to reject."""
    criteria = [
        Criterion(name="ref", measurement="vref_v", operator="==", threshold=1.2),
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"vref_v": 1.2, "gain_db": 99.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        {"vref_v": 1.2, "gain_db": 50.0},
        {"vref_v": 1.2, "gain_db": 1.0},  # incumbent loses on gain
        max_knobs=None,
        points=2,
    )

    assert result.status == "fail"
    assert result.detail["dominating_point"]["point"] == "swept"


def test_an_equality_criterion_outside_the_tolerance_is_not_at_least_as_good():
    """The mirror of the test above: '==' means "at least as good WHEN EQUAL
    within the tolerance", not "always true". The swept point misses the
    candidate's vref by 5% (50x the tolerance), so it must not dominate even
    though it wins on gain.

    Mutation this catches: `return True` for '==' in _at_least_as_good (the
    lazy fix for I6), which would let any point dominate on that axis."""
    criteria = [
        Criterion(name="ref", measurement="vref_v", operator="==", threshold=1.2),
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"vref_v": 1.26, "gain_db": 99.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        {"vref_v": 1.2, "gain_db": 50.0},
        {"vref_v": 1.2, "gain_db": 1.0},
        max_knobs=None,
        points=2,
    )

    assert result.status == "pass"
    assert result.detail["dominating_point"] is None


def test_an_operator_the_rule_cannot_judge_is_inconclusive_and_names_it():
    """I6: "I could not judge this" and "nothing dominated" are different
    facts. An operator this stage's comparison rule cannot handle must end
    the stage as `inconclusive` with the operator NAMED - never as a silent
    pass. It must also cost nothing: the check runs before any simulation, so
    an exploding backend proves no point was ever simulated.

    Mutation this catches: falling back to `status='pass'` (or letting
    _at_least_as_good return False) - the exploding backend also catches a
    version that checks the operators only after sweeping."""

    class _AlwaysFailsBackend:
        def run(self, netlist_path, testbench_config):
            raise AssertionError("no simulation may run when the operators cannot be judged")

    criteria = [
        Criterion(name="weird", measurement="gain_db", operator="!=", threshold=0.0),
        Criterion(name="ok", measurement="iq_ua", operator="<=", threshold=1e9),
    ]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        _AlwaysFailsBackend(),
        {"gain_db": 1.0, "iq_ua": 1.0},
        {"gain_db": 1.0, "iq_ua": 1.0},
        max_knobs=None,
        points=2,
    )

    assert result.status == "inconclusive"
    assert result.status != "pass"
    assert result.detail["unjudgeable_operators"] == [{"criterion": "weird", "operator": "!="}]
    assert "!=" in result.detail["why"]
    assert "weird" in result.detail["why"]
    assert result.detail["simulation_count"] == 0
    assert result.detail["dominating_point"] is None


def test_the_recorded_scope_says_the_low_sweep_bound_is_self_imposed():
    """I8: the sweep runs [baseline/M, baseline*M], but only the HIGH end is
    the area gate's bound - evaluate_area_growth short-circuits on
    `ratio <= 1.0`, so the gate does not restrict shrinking at all. The
    docstring used to call the whole range "the multiplier the area gate
    allows", which overstates the comparison: a tuner may legally shrink
    below baseline/M, and this stage did not look there. The recorded scope
    must say so, since the omission biases this stage toward ADMIT.

    Mutation this catches: dropping the note, or restoring a note that claims
    both ends are the gate's."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)
    backend = _ConstantBackend({"gain_db": 1.0})

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        backend,
        {"gain_db": 1.0},
        {"gain_db": 1.0},
        max_knobs=None,
        points=2,
    )

    note = result.detail["sweep_bounds_note"]
    assert "self-imposed" in note.lower()
    assert "shrink" in note.lower()
    assert "does not restrict shrinking" in note


def test_a_near_tie_is_not_measured_as_an_improvement():
    """C2's other direction, in the stage that produces `addresses`: with a
    zero-tolerance comparison the reviewer's real run reported

        core_phase_margin: cand 66.08350 base 66.08070  better=True (+0.0028)
        trim_loop_gain:    cand 87.54500 base 87.54490  better=True (+0.0001)
        trim_phase_margin: cand 89.42130 base 81.13820  better=True (+8.3, real)

    and all three names went into topology_candidate.py's `addresses`, which
    agents/tuner.py renders straight into the swap-selection prompt - so
    stage 4's whole purpose (no unverified claim reaches the tuner) was
    defeated by measurement noise. Only the real improvement may survive.

    Mutation this catches: COMPARISON_REL_TOLERANCE = 0, or dropping the band
    from _is_better - either puts all three names back in addresses."""
    criteria = [
        Criterion(name="core_phase_margin", measurement="core_pm_deg", operator=">=", threshold=0.0),
        Criterion(name="trim_loop_gain", measurement="trim_gain_db", operator=">=", threshold=0.0),
        Criterion(name="trim_phase_margin", measurement="trim_pm_deg", operator=">=", threshold=0.0),
    ]
    slot = _slot_with_criteria(criteria)
    backend = _SequencedBackend(
        [
            {"core_pm_deg": 66.08350, "trim_gain_db": 87.54500, "trim_pm_deg": 89.42130},
            {"core_pm_deg": 66.08070, "trim_gain_db": 87.54490, "trim_pm_deg": 81.13820},
        ]
    )

    result, addresses = reproduce_characteristics(_candidate(), slot, {"tb1": DECK_SWAPPABLE}, backend)

    assert result.status == "pass"
    assert addresses == ["trim_phase_margin"]
    assert result.detail["criteria"]["core_phase_margin"]["better"] is False
    assert result.detail["criteria"]["trim_loop_gain"]["better"] is False


# --- verify_corners ------------------------------------------------------------

# A swappable block with an explicit Vdd source, so a fake backend can read
# the corner's rendered voltage straight off the deck text (pvt.render_corner_
# netlist rewrites this line's DC value per corner) without needing a real
# ngspice run or real PDK include files.
DECK_CORNER = """* t
.option scale=1.0u
.subckt BLOCK a b vdd vss
R1 a b 1k
.ends BLOCK
Xu1 n1 n2 vdd vss BLOCK
Vdd vdd 0 DC 1.8
.end
"""


def _corner_slot(criteria: list[Criterion], netlist_text: str, pvt_corners: PVTCorners | None) -> Slot:
    tb = SimpleNamespace(
        name="tb1",
        netlist_path="/dev/null",
        analyses=["op"],
        control_block=".control\nop\n.endc",
        criteria=criteria,
        fragments=None)
    spec = SimpleNamespace(
        testbenches=[tb],
        all_criteria=criteria,
        canonical=tb,
        circuit_name="test",
        pvt_corners=pvt_corners,
    )
    return Slot(spec=spec, block_path="BLOCK")


class _CornerBackend:
    """A fake corner-aware backend: reports measurements as an arbitrary
    function of (is_candidate, voltage), read straight off the rendered deck
    text - which body it is (candidate's swapped-in 'R2 a b' line vs the
    baseline's original 'R1 a b') and which corner voltage
    pvt.render_corner_netlist wrote into the Vdd line. Lets a test construct
    an exact per-corner, per-deck trade-off without a real simulator."""

    def __init__(self, measurement_fn):
        self._measurement_fn = measurement_fn

    def run(self, netlist_path, testbench_config):
        with open(netlist_path) as f:
            text = f.read()
        is_candidate = "R2 a b" in text
        voltage = float(re.search(r"Vdd\s+\S+\s+\S+\s+DC\s+(\S+)", text).group(1))
        measurements = self._measurement_fn(is_candidate, voltage)
        return RawSimResult(status="success", measurements=measurements, raw_log="", warnings=[])


def test_an_extracted_candidate_skips_corners_and_records_why():
    """provenance == 'extracted' must never trigger a corner sweep at all -
    an exploding backend proves it, since any call to sim_backend.run would
    raise. A mutation that removed the provenance gate (so every candidate
    gets swept) would hit that raise and fail this test with a RuntimeError
    instead of the expected StageResult."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    candidate = _candidate(provenance="extracted")

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, _ExplodingBackend(), addresses=["gain"])

    assert result.name == "corners"
    assert result.status == "skipped"
    assert "extracted" in result.detail["why"]


def test_a_file_candidate_also_skips_corners():
    """The asymmetry is 'authored only', not 'authored vs extracted' - a
    file-provenance candidate must skip too. Without this, a mutation that
    narrowed the gate to `provenance == "extracted"` (inverted, effectively
    "not authored" only for one of the two non-authored sources) would slip
    through the extracted test above but still wrongly sweep a file candidate."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    candidate = _candidate(provenance="file")

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, _ExplodingBackend(), addresses=["gain"])

    assert result.status == "skipped"
    assert "file" in result.detail["why"]


def test_an_authored_candidate_requires_corners():
    """The mirror of the extracted test: an authored candidate on a spec that
    DOES declare pvt_corners must actually run the corner sweep and produce a
    real verdict, not 'skipped'. If the provenance gate were inverted (skip
    authored, sweep everything else), this would wrongly come back
    'skipped' instead of 'pass'."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    # Candidate strictly beats baseline at every corner voltage.
    backend = _CornerBackend(lambda is_candidate, voltage: {"gain_db": 100.0 if is_candidate else 10.0})
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=["gain"])

    assert result.status == "pass"


def test_an_authored_candidate_on_a_spec_without_corners_is_inconclusive_not_rejected():
    """'이 회로가 나쁘다'와 '재보지 못했다'는 다른 사실이다. No pvt_corners
    declared on the slot's spec means there is nothing to sweep - the result
    must be 'inconclusive', never 'fail' (a mutation collapsing the two would
    reject a candidate this stage never actually measured at any corner), and
    never 'skipped' either (that status is reserved for the provenance gate -
    an authored candidate on a corner-less spec WAS asked for a corner
    verdict, it just couldn't get one)."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _corner_slot(criteria, DECK_CORNER, pvt_corners=None)
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, _ExplodingBackend(), addresses=["gain"])

    assert result.status == "inconclusive"
    assert result.status != "fail"
    assert result.status != "skipped"


def test_a_missing_measurement_at_any_corner_fails():
    """The 'settle' criterion is NOT in addresses, and its measurement is
    never reported at 1.62V, for either the candidate or the baseline deck.
    The one addressed criterion ('gain') is won by the candidate at every
    corner. If requirement 1 (every criterion's measurement must appear at
    every corner) were dropped and only the addressed criteria were checked,
    this would wrongly come back 'pass' - the missing-elsewhere criterion is
    what must independently sink it."""
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0),
        Criterion(name="settle", measurement="settle_us", operator="<=", threshold=100.0),
    ]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)

    def measurement_fn(is_candidate, voltage):
        measurements = {"gain_db": 100.0 if is_candidate else 10.0}
        if voltage == 1.8:
            measurements["settle_us"] = 10.0
        return measurements

    backend = _CornerBackend(measurement_fn)
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=["gain"])

    assert result.status == "fail"
    assert "settle" in result.detail["missing"]


def test_winning_at_nominal_but_losing_at_the_worst_corner_fails():
    """At 1.8V (a stand-in for 'nominal') the candidate beats the baseline
    (100 > 50) - a naive comparison using only that corner would call this a
    win. But 'gain' is a '>=' criterion, so the worst case is the MINIMUM
    across corners: candidate's worst is 10 (at 1.62V), baseline's worst is
    50 (at 1.8V). 10 is not better than 50, so the correct, corner-aware
    comparison must fail this candidate even though it wins at 1.8V."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    candidate_gain = {1.8: 100.0, 1.62: 10.0}
    baseline_gain = {1.8: 50.0, 1.62: 60.0}
    backend = _CornerBackend(
        lambda is_candidate, voltage: {"gain_db": (candidate_gain if is_candidate else baseline_gain)[voltage]}
    )
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=["gain"])

    assert result.status == "fail"
    assert "gain" in result.detail["worse"]
    assert result.detail["criteria"]["gain"]["candidate_worst"] == pytest.approx(10.0)
    assert result.detail["criteria"]["gain"]["baseline_worst"] == pytest.approx(50.0)


def test_a_lower_bound_criterion_wins_at_nominal_but_loses_at_the_worst_corner_fails():
    """The '<=' mirror of test_winning_at_nominal_but_losing_at_the_worst_
    corner_fails (which only ever exercises '>='). This repo's real criteria
    are majority '<=' (iq_ua, psrr_dc, psr_minus_db), and a direction bug of
    exactly this shape escaped Task 3's first pass - every other test in this
    file uses a '>=' criterion, so a comparison hardcoded to '>=' inside
    verify_corners would pass all of them and only be caught here.

    At 1.8V ("nominal") the candidate beats the baseline (150 < 200 - lower is
    better for iq_ua). But '<=' means the worst case is the MAXIMUM across
    corners: candidate's worst is 250 (at 1.62V), baseline's worst is 220 (at
    1.62V too). 250 is not <= (better than) 220, so the candidate must fail
    despite winning at 1.8V."""
    criteria = [Criterion(name="iq", measurement="iq_ua", operator="<=", threshold=1000.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    candidate_iq = {1.8: 150.0, 1.62: 250.0}
    baseline_iq = {1.8: 200.0, 1.62: 220.0}
    backend = _CornerBackend(
        lambda is_candidate, voltage: {"iq_ua": (candidate_iq if is_candidate else baseline_iq)[voltage]}
    )
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=["iq"])

    assert result.status == "fail"
    assert "iq" in result.detail["worse"]
    assert result.detail["criteria"]["iq"]["candidate_worst"] == pytest.approx(250.0)
    assert result.detail["criteria"]["iq"]["baseline_worst"] == pytest.approx(220.0)


def test_only_two_sweeps_are_run(monkeypatch):
    """Regardless of how many corners/testbenches a real sweep would cover,
    verify_corners must call run_full_pvt_sweep EXACTLY twice - once for the
    candidate-swapped deck, once for the untouched baseline. A mutation that
    swept per-knob at corners too (mirroring stage 3's scoped_comparison,
    which the brief explicitly rules out) would call it more than twice."""
    import analogcoder.curation as curation_module

    calls: list[dict] = []

    def fake_run_full_pvt_sweep(netlist_texts, spec, sim_backend):
        calls.append(netlist_texts)
        return {
            "criteria": [{"name": "gain", "actual": 100.0, "pass": True, "target": ">=0.0", "margin": 100.0}],
            "worst_case_corners": {"gain": {"process": "tt", "voltage": 1.8, "temperature": 27.0, "value": 100.0}},
        }

    monkeypatch.setattr(curation_module, "run_full_pvt_sweep", fake_run_full_pvt_sweep)

    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    candidate = _candidate()

    result = curation_module.verify_corners(candidate, slot, {"tb1": DECK_CORNER}, object(), addresses=["gain"])

    assert len(calls) == 2
    assert result.status in ("pass", "fail")  # ran to completion without raising


def test_a_simulator_exception_during_corner_verification_is_inconclusive_not_a_rejection():
    """Matches the rest of this repo's rule for every stage that touches
    sim_backend: a simulator exception is not evidence the candidate is bad,
    so it must not surface as 'fail'."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, _ExplodingBackend(), addresses=["gain"])

    assert result.status == "inconclusive"
    assert result.status != "fail"


def test_an_empty_addresses_list_records_that_requirement_2_compared_nothing():
    """I7, measured: an authored candidate with `addresses: []` shipped
    `verified_at="corners"` having compared nothing at corners. Requirement 2
    iterates `addresses`; with none it compares zero criteria, `worse` stays
    empty and the stage returns 'pass' - a pass that means only "requirement 1
    held", not "the candidate beats the incumbent at its worst corner". The
    real run read `corners: pass, criteria: {}, worse: []` and ADMITted.

    The stage must record that fact. Not "log it only when it matters": this
    repo's rule is that a check which records nothing when it finds nothing
    is indistinguishable from a check that has disappeared.

    Mutation this catches: dropping addresses_compared/requirement_2_note, or
    writing a note that claims a comparison happened regardless."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    # The candidate is WORSE than the incumbent everywhere, which is exactly
    # how `addresses` ends up empty in the measured case.
    backend = _CornerBackend(lambda is_candidate, voltage: {"gain_db": 10.0 if is_candidate else 100.0})
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=[])

    assert result.status == "pass"  # requirement 1 held; requirement 2 had nothing to do
    assert result.detail["addresses_compared"] == 0
    assert result.detail["criteria"] == {}
    note = result.detail["requirement_2_note"]
    assert "NOTHING" in note
    assert "requirement 1" in note


def test_a_non_empty_addresses_list_records_how_many_criteria_it_compared():
    """The positive control for the test above: when requirement 2 DOES
    compare something, the recorded count must say so rather than being a
    constant. Without this, a mutation hardcoding addresses_compared to 0 (or
    always emitting the "compared NOTHING" note) would pass the test above."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    backend = _CornerBackend(lambda is_candidate, voltage: {"gain_db": 100.0 if is_candidate else 10.0})
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=["gain"])

    assert result.status == "pass"
    assert result.detail["addresses_compared"] == 1
    assert "NOTHING" not in result.detail["requirement_2_note"]
    assert "gain" in result.detail["requirement_2_note"]


def test_the_scope_limit_is_recorded_when_the_stage_actually_runs():
    """Requirement 5: this stage must say in words that it does not extend
    stage 3's comparison to corners, whenever it actually ran a comparison
    (pass or fail) - not just when something goes wrong. A mutation that
    dropped this note (or only added it on the fail path) is caught here."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    pvt = PVTCorners(process=["tt"], voltage=[1.8, 1.62], temperature=[27.0])
    slot = _corner_slot(criteria, DECK_CORNER, pvt)
    backend = _CornerBackend(lambda is_candidate, voltage: {"gain_db": 100.0 if is_candidate else 10.0})
    candidate = _candidate()

    result = verify_corners(candidate, slot, {"tb1": DECK_CORNER}, backend, addresses=["gain"])

    assert result.status == "pass"
    assert "scope_note" in result.detail
    assert "stage 3" in result.detail["scope_note"] or "3단" in result.detail["scope_note"]


# --- candidate_from_deck / candidate_from_file (Task 5, sources A/B) --------


def test_extracting_from_a_deck_reproduces_the_shipped_library_entry():
    """benchmarks/bandgap/netlist_loops.cir 의 BUF_P 를 추출하면
    TOPOLOGY_LIBRARY['folded_cascode_pmos_in_cs'] 와 본문·포트·스케일이 같다.

    F1이 이 항목을 손으로 옮겨 적어 라이브러리에 넣었다는 사실은 이미
    알려져 있다 - 이 테스트는 "손으로 옮겨 적은 것이 우연히 맞았다"가
    아니라 "F1의 항목이 이 파이프라인으로 실제로 재생산 가능하다"를
    증명한다. 손으로 적은 상수로 추출을 대체하는 변형은, 그 상수가 이
    라이브러리 항목과 글자 하나 다르지 않은 한 걸리지 않지만, 파싱 경로
    자체가 깨지는 어떤 변형(공백 처리, 스코프 판정, 스케일 판독)도 여기서
    걸린다."""
    from pathlib import Path

    from analogcoder.topologies import TOPOLOGY_LIBRARY

    deck_text = Path("benchmarks/bandgap/netlist_loops.cir").read_text()
    shipped = TOPOLOGY_LIBRARY["folded_cascode_pmos_in_cs"]

    candidate = candidate_from_deck(deck_text, "BUF_P", "extracted_buf_p")

    assert candidate.subckt_body == shipped.subckt_body
    assert candidate.ports == shipped.ports
    assert candidate.assumes_scale == shipped.assumes_scale
    assert candidate.provenance == "extracted"
    assert candidate.topology_id == "extracted_buf_p"


def test_candidate_from_deck_raises_when_the_block_path_does_not_exist():
    deck_text = "* t\n.option scale=1.0u\n.subckt BLOCK a b\nR1 a b 1k\n.ends BLOCK\n.end\n"
    with pytest.raises(ValueError):
        candidate_from_deck(deck_text, "NOPE", "whatever")


def test_a_file_candidate_with_every_declared_port_referenced_is_accepted():
    body = "R1 a b 1k\nR2 b c 2k\n"
    candidate = candidate_from_file(body, ports=["a", "c"], assumes_scale=1e-6, topology_id="from_file")
    assert candidate.subckt_body == body
    assert candidate.ports == ["a", "c"]
    assert candidate.assumes_scale == 1e-6
    assert candidate.provenance == "file"


def test_a_file_candidate_with_an_unreferenced_declared_port_is_rejected():
    """Declares port 'z', which no component in the body ever names as a
    node. The converse (a node the body needs but the caller never declared
    as a port) is deliberately NOT checked here - F1 established that
    direction is not structurally decidable (an undeclared-but-needed port
    is indistinguishable from a legitimate internal node), so it is left to
    the gate's simulation stage (an undeclared port becomes a floating node
    and characteristic reproduction fails there)."""
    body = "R1 a b 1k\nR2 b c 2k\n"
    with pytest.raises(ValueError):
        candidate_from_file(body, ports=["a", "c", "z"], assumes_scale=1e-6, topology_id="from_file")


# --- candidate_from_technique (source C factory) -----------------------------


def test_candidate_from_technique_wraps_the_given_body_as_authored():
    """Direct, non-accidental pin for this factory's own field mapping. The
    task-6 review found that author_and_verify_variant's use of it was
    previously checked only by accident (a fixture's base_body happened to
    equal the deck's actual block body, so a base_body/authored-body mixup
    tripped identical_body rather than being caught directly) - this test
    exercises the factory itself, with no other gate in the way."""
    candidate = candidate_from_technique(
        subckt_body="X1 a b MODELX\n", ports=["a", "b"], assumes_scale=2.5e-6, topology_id="cand_x"
    )
    assert candidate.subckt_body == "X1 a b MODELX\n"
    assert candidate.ports == ["a", "b"]
    assert candidate.assumes_scale == 2.5e-6
    assert candidate.topology_id == "cand_x"
    assert candidate.provenance == "authored"


# --- author_and_verify_variant (source C reject-and-retry loop) -------------
#
# This function moved here from agents/variant_author.py after the task-6
# review (I-2): it interleaves the LLM call (agents.variant_author.
# author_variant) with this module's own gates (check_structure,
# reproduce_characteristics), so it belongs beside them rather than in
# agents/ - the same convention orchestrator.py and optimizer.py already
# follow. `author_variant` itself is mocked at "analogcoder.curation.
# author_variant" (the name this module imported it under), not
# "analogcoder.agents.variant_author.run_agent" - one level higher than
# before, so a test's rejection_feedback assertion reads the exact keyword
# argument the retry loop passed, not a rendered prompt string.


def _variant_slot(criteria: list[Criterion]) -> Slot:
    return _slot_with_criteria(criteria)


@pytest.mark.asyncio
async def test_a_structure_rejection_is_fed_back_verbatim_and_retried():
    """First authored body instantiates a model (MODELX) the deck never
    provides - a 'models' rejection from check_structure/compatible_swaps.
    The loop must feed that exact rejection (reason AND detail, not a
    paraphrase) back as the next call's rejection_feedback, and must
    actually make a second attempt rather than giving up after one
    rejection. The expected reason is computed via the real check_structure
    (same convention as this file's other verbatim-rejection test) so this
    pins "carried over exactly", not a hardcoded string."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _variant_slot([])

    bad_candidate = Candidate(
        topology_id="cand_variant",
        subckt_body="X1 a b MODELX\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    expected = check_structure(bad_candidate, slot, netlist_texts)
    assert expected.status == "fail"

    responses = [
        {"subckt_body": "X1 a b MODELX\n", "rationale": "first try"},
        {"subckt_body": "R2 a b 2k\n", "rationale": "fixed, no foreign model"},
    ]
    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(side_effect=responses),
    ) as mock_author:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([{}, {}]),
            backend=object(),
        )

    assert mock_author.call_count == 2
    second_feedback = mock_author.call_args_list[1].kwargs["rejection_feedback"]
    assert expected.detail["reason"] in second_feedback
    assert expected.detail["detail"] in second_feedback
    assert result.verdict == "PASS"
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_the_retry_limit_is_honoured():
    """Every attempt is rejected at the structure stage (same MODELX shape as
    above, every time). The loop must stop calling the backend at exactly
    MAX_VARIANT_AUTHOR_RETRIES attempts - not fewer (giving up early would
    silently shrink the retry budget) and not more (an unbounded loop)."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _variant_slot([])

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(return_value={"subckt_body": "X1 a b MODELX\n", "rationale": "always bad"}),
    ) as mock_author:
        await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([]),
            backend=object(),
        )

    assert mock_author.call_count == MAX_VARIANT_AUTHOR_RETRIES == 3


@pytest.mark.asyncio
async def test_exhausting_retries_is_a_rejection_not_inconclusive():
    """Exhausting the retry budget without ever producing a body that passes
    is a measured fact about the circuit (it tried and failed), not "never
    tried" - so the verdict must be REJECT, never INCONCLUSIVE, and `reason`
    must carry the LAST rejection (not None, not silently dropped)."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _variant_slot([])

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(return_value={"subckt_body": "X1 a b MODELX\n", "rationale": "always bad"}),
    ):
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([]),
            backend=object(),
        )

    assert result.verdict == "REJECT"
    assert result.verdict != "INCONCLUSIVE"
    assert result.candidate is None
    assert result.reason is not None
    assert "models" in result.reason


@pytest.mark.asyncio
async def test_an_agent_execution_error_is_inconclusive():
    """The backend dying (or failing schema validation) says nothing about
    whether the authored circuit is good - it never got a body to judge.
    This must be INCONCLUSIVE, never REJECT, and must not be silently
    retried (a dead backend will typically fail again, so retrying spends
    budget without learning anything new)."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _variant_slot([])

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(side_effect=AgentExecutionError("rate limited")),
    ) as mock_author:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([]),
            backend=object(),
        )

    assert result.verdict == "INCONCLUSIVE"
    assert result.verdict != "REJECT"
    assert result.candidate is None
    assert mock_author.call_count == 1


@pytest.mark.asyncio
async def test_a_reproduce_rejection_is_fed_back_verbatim_and_retried():
    """I-1 (task-6 review): the two stage-2 branches had zero mutation
    coverage. This covers the reproduce-fail retry branch: the first
    authored body passes structure but its simulated deck never produces
    the 'gain_db' measurement the slot's one criterion needs - a stage-2
    ('fail', missing measurement) rejection, not a simulator exception. The
    loop must feed that rejection's `missing` list back verbatim as the next
    attempt's rejection_feedback, and must actually retry."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _variant_slot(criteria)

    attempt1_candidate = Candidate(
        topology_id="cand_variant", subckt_body="R2 a b 2k\n", ports=["a", "b"], assumes_scale=1e-6, provenance="authored"
    )
    expected, _ = reproduce_characteristics(
        attempt1_candidate, slot, netlist_texts, _SequencedBackend([{}, {"gain_db": 50.0}])
    )
    assert expected.status == "fail"
    assert expected.detail["missing"] == ["gain"]

    responses = [
        {"subckt_body": "R2 a b 2k\n", "rationale": "first try - missing measurement"},
        {"subckt_body": "R3 a b 3k\n", "rationale": "fixed"},
    ]
    sim_backend = _SequencedBackend(
        [
            {},  # attempt 1 candidate sim: missing gain_db
            {"gain_db": 50.0},  # attempt 1 baseline sim
            {"gain_db": 42.0},  # attempt 2 candidate sim
            {"gain_db": 50.0},  # attempt 2 baseline sim
        ]
    )

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(side_effect=responses),
    ) as mock_author:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=sim_backend,
            backend=object(),
        )

    assert mock_author.call_count == 2
    second_feedback = mock_author.call_args_list[1].kwargs["rejection_feedback"]
    assert str(expected.detail["missing"]) in second_feedback
    assert result.verdict == "PASS"
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_a_reproduce_stage_simulator_exception_is_inconclusive_not_a_rejection():
    """I-1 (task-6 review): the reproduce-inconclusive branch had zero
    mutation coverage - mutating its 'INCONCLUSIVE' to 'REJECT' left every
    prior test passing. The simulator crashing during stage 2 says nothing
    about whether the authored circuit is good (reproduce_characteristics
    itself already distinguishes this from a genuine rejection - see
    test_a_simulator_exception_is_inconclusive_not_a_rejection above); the
    retry loop must preserve that distinction rather than collapsing a
    simulator crash into REJECT, and must not retry (retrying will not fix a
    crashing simulator)."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _variant_slot(criteria)

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(return_value={"subckt_body": "R2 a b 2k\n", "rationale": "..."}),
    ) as mock_author:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_ExplodingBackend(),
            backend=object(),
        )

    assert result.verdict == "INCONCLUSIVE"
    assert result.verdict != "REJECT"
    assert result.candidate is None
    assert mock_author.call_count == 1


@pytest.mark.asyncio
async def test_the_admitted_candidate_wraps_the_authored_body_not_the_base_body():
    """Minor 3 (task-6 review): base_body is deliberately DIFFERENT from both
    the deck's actual BLOCK content ('R1 a b 1k') and from the authored
    response ('R9 a b 9k'), so identical_body cannot fire either way - the
    only way this test can fail is a genuine mixup between base_body and the
    authored subckt_body when author_and_verify_variant builds its
    Candidate. This replaces an earlier accidental catch where a fixture's
    base_body happened to equal the deck's own block body, so a base_body/
    authored-body swap was only caught via identical_body, not directly."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _variant_slot([])

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(return_value={"subckt_body": "R9 a b 9k\n", "rationale": "..."}),
    ):
        result = await author_and_verify_variant(
            base_body="R5 a b 5k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([{}, {}]),
            backend=object(),
        )

    assert result.verdict == "PASS"
    assert result.candidate.subckt_body == "R9 a b 9k\n"
    assert result.candidate.subckt_body != "R5 a b 5k\n"


# --- I1: the declared-port reference check applies to ALL THREE sources ------
#
# Before this, only source B checked it. Source A copied the `.subckt` header
# verbatim and source C inherited the slot block's ports verbatim, and source
# C's docstring justified the omission with a claim that was false: stage 1
# "already judges port compatibility". It cannot - the ports handed to stage 1
# ARE the block's own, so `set(topology.ports) <= set(block.ports)` is an
# identity, `leftover_ports` is empty, and topology_match's floating-net check
# never runs. The three tests below pin each source, and the fourth pins the
# consequence that made this worth fixing.


# A body whose components never name 'nbias' as a node, inside a block whose
# header declares it. This is exactly the shape `--technique "remove the
# cascode"` produces for ncas/pcas.
DECK_WITH_AN_UNUSED_HEADER_PORT = """* t
.option scale=1.0u
.subckt BLOCK a b nbias
R1 a b 1k
.ends BLOCK
Xu1 n1 n2 nb BLOCK
.end
"""


def test_a_deck_extracted_candidate_with_an_unreferenced_header_port_is_rejected():
    """Source A. The `.subckt` header declares three ports; the body names
    only two. Copying the header verbatim would record ports=[a, b, nbias]
    onto a library entry whose body cannot connect nbias to anything.

    Mutation this catches: deleting the reject_unreferenced_ports call from
    candidate_from_deck (observed: this test then reports
    `ports == ['a', 'b', 'nbias']` instead of raising)."""
    with pytest.raises(ValueError, match="nbias"):
        candidate_from_deck(DECK_WITH_AN_UNUSED_HEADER_PORT, "BLOCK", "extracted_overstated")


def test_an_authored_candidate_with_an_unreferenced_inherited_port_is_rejected():
    """Source C. The ports are inherited from the slot block, not parsed from
    the authored body, so nothing else in this factory can notice that the
    technique removed the only device using one of them.

    Mutation this catches: deleting the reject_unreferenced_ports call from
    candidate_from_technique (observed: the candidate is then constructed
    with provenance='authored' and ports=['a', 'b', 'nbias'])."""
    with pytest.raises(ValueError, match="nbias"):
        candidate_from_technique(
            subckt_body="R1 a b 1k\n",
            ports=["a", "b", "nbias"],
            assumes_scale=1e-6,
            topology_id="authored_overstated",
        )


def test_stage_1_cannot_catch_an_overstated_port_list_which_is_why_this_check_exists():
    """The measured fact that makes source C's old docstring false, pinned
    directly rather than asserted in prose: hand a candidate the block's own
    full port list together with a body that uses only some of them, and
    check_structure PASSES - because leftover_ports is empty, so
    topology_match's floating-net rule never runs at all.

    This test deliberately constructs the Candidate directly (bypassing the
    factories, which now refuse it) - the point is what stage 1 does with
    such an entry once one exists in the library, i.e. forever after a human
    commits it.

    Mutation this catches: none in the production path - it is a
    characterisation test. It breaks if topology_match ever gains a rule that
    would have caught this, at which point the factories' check could be
    reconsidered rather than silently kept as dead weight."""
    overstated = Candidate(
        topology_id="overstated",
        # Deliberately NOT the block's existing body - an identical one is
        # rejected by compatible_swaps' own `identical_body` no-op rule, which
        # would mask the point being made here.
        subckt_body="R1 a b 1k\nR3 a b 3k\n",
        ports=["a", "b", "nbias"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    slot = _dummy_slot("BLOCK")
    result = check_structure(overstated, slot, {"tb1": DECK_WITH_AN_UNUSED_HEADER_PORT})
    assert result.status == "pass", result.detail

    # ... whereas the honest port list - what the body actually uses - is the
    # one the floating-net rule can see, and it rejects, because no other
    # component in Xu1's scope references net 'nb'.
    honest = Candidate(
        topology_id="honest",
        subckt_body="R1 a b 1k\nR3 a b 3k\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    honest_result = check_structure(honest, slot, {"tb1": DECK_WITH_AN_UNUSED_HEADER_PORT})
    assert honest_result.status == "fail"
    assert honest_result.detail["reason"] == "ports"
    assert "float" in honest_result.detail["detail"]


@pytest.mark.asyncio
async def test_an_authored_body_that_drops_a_port_is_retried_not_ended():
    """The rejection is a judged fact about THIS attempt's body, so it must
    return through the retry loop as feedback - the same treatment stage 1
    and stage 2 rejections get - rather than escaping as a ValueError that
    cli_curate's guard would convert into INCONCLUSIVE with the retry budget
    untouched.

    Mutation this catches: removing the `except ValueError` around
    candidate_from_technique in author_and_verify_variant (observed:
    `ValueError: declared port(s) ['nbias'] are not referenced ...`
    propagates out of author_and_verify_variant and the test errors)."""
    netlist_texts = {"tb1": DECK_WITH_AN_UNUSED_HEADER_PORT}
    tb = SimpleNamespace(
        name="tb1", netlist_path="/dev/null", analyses=["op"], control_block=".control\nop\n.endc", criteria=[],
        fragments=None)
    spec = SimpleNamespace(testbenches=[tb], all_criteria=[], canonical=tb)
    slot = Slot(spec=spec, block_path="BLOCK")

    responses = [
        {"subckt_body": "R1 a b 1k\n", "rationale": "dropped the bias device"},
        {"subckt_body": "R1 a b 1k\nR2 nbias b 2k\n", "rationale": "kept nbias"},
    ]
    with patch("analogcoder.curation.author_variant", new=AsyncMock(side_effect=responses)) as mock_author:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\nR2 nbias b 2k\n",
            technique="remove the bias device",
            ports=["a", "b", "nbias"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([{}, {}]),
            backend=object(),
        )

    assert mock_author.call_count == 2
    feedback = mock_author.call_args_list[1].kwargs["rejection_feedback"]
    assert "nbias" in feedback
    assert result.verdict == "PASS"
    assert result.attempts == 2


# --- I2: the exhausted-retries REJECT must carry the stages it reached ------


@pytest.mark.asyncio
async def test_exhausting_retries_still_records_the_last_reached_stage_results():
    """VariantAuthorResult's own docstring promises structure/reproduce hold
    the last reached attempt's StageResult. The exhausted-retries return
    hardcoded both to None, so after 3 LLM calls, 3 structure checks and 3
    simulated stage-2 runs the report's `## Stages` was empty and
    curation.json's `"stages"` was `[]` - this repo's recurring "checked and
    fine is indistinguishable from the check is gone" shape.

    Here every attempt passes structure and fails stage 2 on a missing
    measurement, so BOTH stages were genuinely reached and both must survive
    into the REJECT.

    Mutation this catches: restoring `structure=None, reproduce=None` on the
    exhausted-retries return (observed: `assert result.structure is not None`
    fails with `assert None is not None`)."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _variant_slot(criteria)

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(return_value={"subckt_body": "R2 a b 2k\n", "rationale": "why I did it"}),
    ):
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            # 3 attempts x (candidate deck, baseline deck) = 6 sims, none of
            # which produce gain_db, so stage 2 fails on `missing` every time.
            sim_backend=_SequencedBackend([{}] * 6),
            backend=object(),
        )

    assert result.verdict == "REJECT"
    assert result.attempts == MAX_VARIANT_AUTHOR_RETRIES
    assert result.structure is not None and result.structure.status == "pass"
    assert result.reproduce is not None and result.reproduce.status == "fail"
    assert result.reproduce.detail["missing"] == ["gain"]
    # ... and the rationale of the attempt that produced them is kept too,
    # rather than discarded (I5's "record it rather than throw it away").
    assert result.rationale == "why I did it"


@pytest.mark.asyncio
async def test_a_never_reached_stage_stays_none_on_the_exhausted_retries_reject():
    """The other half of the same distinction, so the fix above cannot be
    'always report something'. Every attempt is rejected at stage 1, so
    stage 2 was never reached and must remain None - "this stage passed" and
    "this stage was never looked at" stay different facts.

    Mutation this catches: setting `reproduce=last_structure` or otherwise
    fabricating a stage-2 result (observed by mutating the return to
    `reproduce=last_structure`: `assert result.reproduce is None` fails)."""
    netlist_texts = {"tb1": DECK_TWO_PORT}
    slot = _variant_slot([])

    with patch(
        "analogcoder.curation.author_variant",
        new=AsyncMock(return_value={"subckt_body": "X1 a b MODELX\n", "rationale": "always bad"}),
    ):
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_SequencedBackend([]),
            backend=object(),
        )

    assert result.verdict == "REJECT"
    assert result.structure is not None and result.structure.status == "fail"
    assert result.reproduce is None


# --- M8: source A must not record a scale it cannot see ---------------------


def test_extracting_from_a_deck_whose_scale_may_live_in_an_include_is_refused():
    """`.option scale` inside `pdk_corner.inc` is invisible to parse_netlist,
    which never follows includes - so recording assumes_scale=1.0 for such a
    deck is off by 1e6, the exact trap CLAUDE.md already documents for the
    area gate. Source A is a new entry point to it, and the entry it would
    write carries the wrong number into the library permanently.

    Refusing is the minimum: the pipeline cannot know the real scale, and
    guessing 1.0 is the failure mode.

    Mutation this catches: dropping the declares_scale/declares_include guard
    from candidate_from_deck (observed: no exception, and
    `candidate.assumes_scale == 1.0`)."""
    deck = '* t\n.include "pdk_corner.inc"\n.subckt BLOCK a b\nR1 a b 1k\n.ends BLOCK\n.end\n'
    with pytest.raises(ValueError, match="scale"):
        candidate_from_deck(deck, "BLOCK", "scale_unknown")


def test_a_deck_with_no_includes_at_all_records_scale_1_as_a_fact():
    """The other side of the same rule: with no includes there is nowhere
    else for a scale to hide, so 1.0 is parsed fact rather than a default
    standing in for an unknown. Without this, the guard above could be
    'widened' into refusing every deck that omits .option scale.

    Mutation this catches: broadening the guard to `if not declares_scale(...)`
    alone (observed: this test raises ValueError instead of returning)."""
    deck = "* t\n.subckt BLOCK a b\nR1 a b 1k\n.ends BLOCK\n.end\n"
    candidate = candidate_from_deck(deck, "BLOCK", "no_includes")
    assert candidate.assumes_scale == 1.0


# --- M2: stage 3 records what narrowing was REQUESTED, not just its effect ---


def test_stage_3_detail_records_every_requested_narrowing_including_zero():
    """`knobs_omitted` alone cannot distinguish "this block has no knobs" from
    "you asked me to sweep zero of them": `--knobs ""` parses to `[]` (a
    distinction _parse_knob_names deliberately preserves) and `--max-knobs 0`
    / `--points 0` are both accepted by the parser. The requested values
    themselves must therefore survive into the recorded scope.

    Mutation this catches: deleting `max_knobs_requested`/`points_requested`
    from scoped_comparison's detail dict (observed: KeyError on both
    assertions below - verified by removing exactly those two lines)."""
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=0.0)]
    slot = _scoped_slot(criteria, DECK_ONE_KNOB)

    result = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        _ConstantBackend({"gain_db": 1.0}),
        candidate_measurements={"gain_db": 5.0},
        incumbent_measurements={"gain_db": 1.0},
        max_knobs=0,
        points=0,
        knob_names=[],
    )

    assert result.detail["max_knobs_requested"] == 0
    assert result.detail["points_requested"] == 0
    assert result.detail["knob_names_requested"] == []
    # ... and with nothing requested, the same keys record that too (None is
    # "no narrowing asked for", which is not 0).
    unnarrowed = scoped_comparison(
        _candidate(),
        slot,
        {"tb1": DECK_ONE_KNOB},
        _ConstantBackend({"gain_db": 1.0}),
        candidate_measurements={"gain_db": 5.0},
        incumbent_measurements={"gain_db": 1.0},
        max_knobs=None,
        points=5,
        knob_names=None,
    )
    assert unnarrowed.detail["max_knobs_requested"] is None
    assert unnarrowed.detail["points_requested"] == 5
    assert unnarrowed.detail["knob_names_requested"] is None


# --- M5: the prompt's model set must not diverge from the gate's ------------


def test_the_prompt_model_set_is_the_intersection_across_every_testbench():
    """`available_models` was derived from the CANONICAL testbench alone while
    `compatible_swaps` requires a model to be present in EVERY testbench (the
    same reason it has a `missing_in_testbench` rejection at all:
    push_netlist_version versions every testbench atomically, so a
    partially-swapped state cannot exist). So the prompt could recommend a
    model the gate would then reject.

    Here `MODEL_ONLY_IN_CANONICAL` exists only in tb1; the prompt must not
    offer it.

    Mutation this catches: using `union` instead of `set.intersection`
    (observed: 'MODEL_ONLY_IN_CANONICAL' appears in `offered` and both
    assertions below fail)."""
    tb1 = "* t\n.subckt BLOCK a b\nX1 a b MODEL_SHARED\nX2 a b MODEL_ONLY_IN_CANONICAL\n.ends BLOCK\n.end\n"
    tb2 = "* t\n.subckt BLOCK a b\nX1 a b MODEL_SHARED\n.ends BLOCK\n.end\n"

    offered, record = prompt_available_models({"tb1": tb1, "tb2": tb2})

    assert offered == {"MODEL_SHARED"}
    assert record["dropped_not_in_every_testbench"] == ["MODEL_ONLY_IN_CANONICAL"]


def test_tokens_that_cannot_be_a_spice_name_are_dropped_from_the_prompt():
    """Measured on this repo's own decks: `all_model_names` treats any
    positional value `parse_spice_value` cannot parse as a model name, so
    `benchmarks/bandgap`'s startup deck yields `'1.8)'` and its settling deck
    yields `'10u)'` (mis-parsed control/expression lines). Those were handed
    to the agent as "models this deck instantiates".

    The filter reads no MEANING out of the name - only whether the string can
    syntactically be a SPICE identifier at all, which `'1.8)'` cannot.

    Mutation this catches: removing the `_VALID_MODEL_NAME` filter (observed:
    `offered == {'MODEL_OK', '1.8)'}` and the first assertion fails)."""
    deck = "* t\n.subckt BLOCK a b\nX1 a b MODEL_OK\nX2 a b 1.8)\n.ends BLOCK\n.end\n"

    offered, record = prompt_available_models({"tb1": deck})

    assert offered == {"MODEL_OK"}
    assert record["dropped_not_a_syntactically_valid_name"] == ["1.8)"]


def test_the_decks_own_block_definitions_are_not_offered_as_device_models():
    """`all_model_names` also returns the deck's own `.subckt` definition
    names (measured on bandgap: BANDGAP, BGR_CORE, BUF_N, BUF_P, ERRAMP,
    TRIMAMP). Those are true - the deck really instantiates them - but this
    agent's job is a LOCAL modification of ONE such block's body, and
    instantiating a sibling block inside it is not a technique.

    This is a parsed fact ("the deck defines this name as a .subckt"), not a
    name heuristic. And it only narrows the PROMPT: the gate keeps its own
    broader set, so no body the gate would accept becomes unreachable.

    Mutation this catches: not subtracting `block_definitions` (observed:
    'BLOCK' and 'OTHER' both appear in `offered`)."""
    deck = (
        "* t\n"
        ".subckt BLOCK a b\nX1 a b sky130_fd_pr__nfet_01v8\n.ends BLOCK\n"
        ".subckt OTHER a b\nR9 a b 5k\n.ends OTHER\n"
        "Xu1 n1 n2 BLOCK\nXu2 n2 n3 OTHER\n.end\n"
    )

    offered, record = prompt_available_models({"tb1": deck})

    assert offered == {"sky130_fd_pr__nfet_01v8"}
    assert record["dropped_block_definitions_of_this_deck"] == ["BLOCK", "OTHER"]


def test_on_the_shipped_bandgap_slot_this_narrowing_changes_only_the_block_names():
    """The measured state of the fix on real decks, pinned so that "this rule
    does nothing today" cannot quietly become "this rule is gone".

    On `benchmarks/bandgap/spec_curate_slot.yaml` the intersection EQUALS the
    canonical set (there is one testbench), and no mis-parsed token appears -
    so of the three narrowings, only the block-definition one actually fires
    here. The prompt/gate divergence was latent, not live, and this test says
    so rather than letting a passing suite imply it was live.

    Mutation this catches: dropping the block-definition subtraction
    (observed: `offered` gains BANDGAP/BGR_CORE/BUF_N/BUF_P/ERRAMP/TRIMAMP and
    the first assertion fails)."""
    import os

    from analogcoder.netlist import resolve_includes
    from analogcoder.spec import load_spec

    spec = load_spec("benchmarks/bandgap/spec_curate_slot.yaml")
    netlist_texts = {
        tb.name: resolve_includes(Path(tb.netlist_path).read_text(), os.path.dirname(tb.netlist_path))
        for tb in spec.testbenches
    }

    offered, record = prompt_available_models(netlist_texts)

    assert offered == {
        "sky130_fd_pr__nfet_01v8",
        "sky130_fd_pr__pfet_01v8",
        "sky130_fd_pr__pnp_05v5_W3p40L3p40",
        "sky130_fd_pr__res_high_po",
    }
    assert record["dropped_block_definitions_of_this_deck"] == [
        "BANDGAP", "BGR_CORE", "BUF_N", "BUF_P", "ERRAMP", "TRIMAMP",
    ]
    # Measured: on THIS slot the other two narrowings fire on nothing.
    assert record["dropped_not_in_every_testbench"] == []
    assert record["dropped_not_a_syntactically_valid_name"] == []
