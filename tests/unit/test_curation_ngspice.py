"""Task 8 proof - real ngspice end-to-end: does the curation gate (F2) reach
the same verdict that a human reached by hand-sweeping in the previous
sub-project?

F1's design doc (docs/superpowers/specs/2026-07-28-topology-applicability-design.md)
records a real measured failure: the textbook "indirect"/Ahuja compensation
for TRIMAMP (Xcc's compensation cap moved from the output node to the NMOS
cascode source node `ns`, XRz removed) was adopted as a brainstormed library
candidate and only THEN measured - and it lost. A single existing knob of the
incumbent topology (TRIMAMP.XRz.l, the nulling resistor's length) dominates
the Ahuja candidate's best point on the axes that comparison actually
tracked (TRIMAMP's own phase margin and unity-gain bandwidth), at the same
compensation cap area. This file proves the curation pipeline (candidate_from_file ->
stage 1 check_structure -> stage 2 reproduce_characteristics -> stage 3
scoped_comparison) reaches that same REJECT verdict automatically, against a
real ngspice run, with no LLM involved (this is a source-B/file candidate,
not source C/authored, so author_and_verify_variant never runs here).

Scope-narrowing note (brief rule 2, knob axis): TRIMAMP has 30 tunable
(refdes, param) knobs; sweeping all of them at even a few points each would
be ~150 simulations. This test deliberately narrows stage 3's sweep to
exactly the one knob the earlier hand-swept measurement identified as the
dominating knob (TRIMAMP.XRz.l), via scoped_comparison's new `knob_names`
parameter (curation.py) - the narrowing itself, not just its result, is the
point: the test's own name says so, and knobs_omitted/knob_names_requested
in the recorded StageResult.detail say so too.

Scope-narrowing note (criteria axis - a SECOND finding this file measured,
not assumed): `benchmarks/bandgap/spec_curate_slot.yaml`'s single testbench
declares 8 criteria spanning all FOUR amps that share this deck's bias
rails (core/trim/buf1/buf0 loop gain + phase margin), because it exists to
exercise this pipeline generally. Running scoped_comparison against that
FULL criteria set does NOT register domination anywhere in [5, 45] - not
because the real trade-off vanished (trim_phase_margin still swings
63->131 deg while trim_loop_gain barely moves), but because two criteria
that are essentially decoupled from TRIMAMP's own compensation network
(core_phase_margin: BGR_CORE's own loop; trim_loop_gain itself, which
barely differs between topologies) sit a few THOUSANDTHS of a unit on the
wrong side of a literal, zero-tolerance '>=' at every swept point - well
inside ordinary SPICE solver precision, not a real design trade-off. That
is measured and pinned below
(test_the_full_multi_loop_criteria_set_blocks_domination_by_near_ties), not
swept under the rug. The HEADLINE test therefore narrows the JUDGED
criteria to the two axes F1's own hand analysis actually compared - phase
margin and unity-gain bandwidth (that design doc's own comparison table's
two columns are literally "위상여유"/"UGBW"; it never tracks loop gain at
all for this comparison) - which is what "TRIMAMP as a slot" meant in that
analysis to begin with, not a convenient trim to force the expected answer.
`trim_loop_gain` is a real criterion in the shipped spec (used for
PASS/FAIL against a threshold elsewhere in this codebase) but was never one
of the two axes this specific hand comparison used, and the module's
"genuine finding" test below keeps it in to show exactly what happens when
it's included.
"""

import math
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from analogcoder.curation import (
    COMPARISON_REL_TOLERANCE,
    Slot,
    candidate_from_deck,
    candidate_from_file,
    check_structure,
    reproduce_characteristics,
    scoped_comparison,
)
from analogcoder.netlist import apply_topology_swap, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.topologies import TOPOLOGY_LIBRARY

BENCH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap"))
SLOT_SPEC_PATH = os.path.join(BENCH, "spec_curate_slot.yaml")

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")

# The exact Ahuja/indirect-compensation variant measured in F1's design doc:
# TRIMAMP's body with XRz deleted and Xcc moved from the output node to the
# NMOS cascode source node `ns` (Saxena-Baker indirect compensation), gate
# driven by vout, L=W=20 - the "ns/20" row of that doc's table, measured
# there at 89.4 deg / 5.45 MHz. This is a hand-authored source-B candidate
# (candidate_from_file), not an extraction and not an LLM-authored variant.
AHUJA_INDIRECT_COMP_BODY = """Xt   tail nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X1   nx   vinn tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
X2   ny   vinp tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
Xp1  nx   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xp2  ny   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc1  np   pcas  nx   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc2  outA pcas  ny   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xn1  np   ncas  nr   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xn2  outA ncas  ns   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm1  nr   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm2  ns   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X6   vout outA  vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
X7   vout nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xcc  ns   vout  ns   ns  sky130_fd_pr__pfet_01v8 L=20 W=20
"""

TRIMAMP_PORTS = ["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"]


def _load_netlist_texts(spec) -> dict[str, str]:
    """Mirrors cli_curate._curate's own loading: resolve_includes against
    each testbench netlist's OWN directory before anything is written to a
    tmp dir. spec_curate_slot.yaml's single testbench (amp_loops) points at
    netlist_loops.cir, whose `.include "pdk_corner.inc"` is a path relative
    to benchmarks/bandgap - resolve_includes rewrites it to an absolute path
    so a later write to any tmp directory still resolves it, exactly the
    established pattern in test_topology_seed_ngspice.py."""
    netlist_texts: dict[str, str] = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            netlist_texts[tb.name] = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
    return netlist_texts


def _full_trimamp_slot() -> tuple[Slot, dict[str, str]]:
    """The slot as spec_curate_slot.yaml declares it verbatim - all 8
    criteria across all 4 amps. Used by the "genuine finding" test below."""
    spec = load_spec(SLOT_SPEC_PATH)
    netlist_texts = _load_netlist_texts(spec)
    slot = Slot(spec=spec, spec_dir=Path(BENCH), block_path="TRIMAMP")
    return slot, netlist_texts


def _trimamp_own_loop_slot() -> tuple[Slot, dict[str, str]]:
    """Same real netlist as _full_trimamp_slot, but the judged criteria are
    narrowed to the TWO axes F1's own hand analysis actually compared for
    this exact candidate: TRIMAMP's own phase margin AND unity-gain
    bandwidth (the design doc's table columns are literally "위상여유" /
    "UGBW" - NOT trim_loop_gain, which that table never tracks at all).
    Measured directly above (see this module's mutation-testing notes / the
    real run): trim_loop_gain sits at a constant ~87.5449 dB across the
    WHOLE XRz.l sweep regardless of value, physically because a
    compensation network (Miller+Rz or Ahuja) reshapes phase/bandwidth, not
    DC loop gain - so it is a real but physically decoupled axis from what
    is actually being traded off here, and including it would block
    domination on a difference smaller than 0.001 dB (see
    test_the_full_multi_loop_criteria_set_blocks_domination_by_near_ties,
    which keeps that axis deliberately to document exactly this).

    spec_curate_slot.yaml's control block computes tmag/tph for the trim
    loop but only measures phase AT the tmag=0 crossing
    (`trim_pm_deg`) - not the crossing FREQUENCY itself (the UGBW). One
    `meas` line is appended right after it
    (`meas ac trim_ugbw_hz WHEN tmag=0`, no FIND clause - ngspice reports
    the sweep variable, i.e. frequency, at the crossing) to expose that
    frequency as a measurement. This is the same control block otherwise
    unchanged - every existing measurement (including trim_gain_db) is
    still computed, just not judged here. Verified against this exact
    candidate: baseline 4.817 MHz, candidate (Ahuja, Cc=20 at `ns`) 5.457
    MHz - matching the design doc's own 4.813 MHz / 5.45 MHz for the same
    two points almost exactly, confirming this measurement is the same
    quantity the design doc calls UGBW."""
    spec = load_spec(SLOT_SPEC_PATH)
    netlist_texts = _load_netlist_texts(spec)
    real_tb = spec.testbenches[0]

    augmented_control_block = real_tb.control_block.replace(
        "meas ac trim_pm_deg  FIND tph WHEN tmag=0",
        "meas ac trim_pm_deg  FIND tph WHEN tmag=0\n      meas ac trim_ugbw_hz WHEN tmag=0",
    )
    assert augmented_control_block != real_tb.control_block, "control block's trim_pm_deg line changed - update this string"

    trim_pm_criterion = next(c for c in real_tb.criteria if c.name == "trim_phase_margin")
    trim_ugbw_criterion = SimpleNamespace(
        name="trim_unity_gain_bandwidth",
        measurement="trim_ugbw_hz",
        operator=">=",
        threshold=0.0,
        unit="Hz",
    )
    trim_criteria = [trim_pm_criterion, trim_ugbw_criterion]

    tb = SimpleNamespace(
        name=real_tb.name,
        netlist_path=real_tb.netlist_path,
        analyses=real_tb.analyses,
        control_block=augmented_control_block,
        criteria=trim_criteria,
    )
    scoped_spec = SimpleNamespace(
        testbenches=[tb],
        all_criteria=trim_criteria,
        canonical=tb,
        circuit_name=spec.circuit_name,
    )
    slot = Slot(spec=scoped_spec, spec_dir=Path(BENCH), block_path="TRIMAMP")
    return slot, netlist_texts


def test_indirect_compensation_is_rejected_because_a_single_knob_change_dominates_it__scope_narrowed_to_trimamp_xrz_l():
    """F1's real measured rejection, reproduced by the deterministic gate,
    judged on the two criteria (trim_loop_gain, trim_phase_margin) F1's own
    hand analysis actually compared for TRIMAMP's own loop - see the module
    docstring's "criteria axis" note for why, and
    test_the_full_multi_loop_criteria_set_blocks_domination_by_near_ties
    below for what happens on the broader 8-criteria slot instead.

    Stage 1 (check_structure): the Ahuja body declares TRIMAMP's own 9
    ports and uses only models already present in the deck, so it must be
    admitted as a structurally compatible candidate for this slot.

    Stage 2 (reproduce_characteristics): both the candidate-swapped deck and
    the untouched incumbent deck are simulated for real; this must produce
    both judged criteria's measurements (no missing values) so stage 3 has a
    complete candidate_measurements dict to compare against.

    Stage 3 (scoped_comparison), narrowed via knob_names to EXACTLY
    TRIMAMP.XRz.l (see this test's own name and the module docstring for
    why: 30 knobs at even a few points each is far more simulation than this
    one hand-identified knob needs). area_limits.tunable_range gives
    XRz.l's baseline (15, i.e. 15 um under this deck's .option scale=1.0u)
    a 3.0x tier (SKY130_GEOMETRY_TIERS' first boundary is 25 um and 15 < 25),
    so the reachable sweep range is [5, 45] - NOT the 60 the earlier hand
    sweep used (that measurement predates this gate and predates the area
    gate being consulted at all here). The brief is explicit that this must
    be measured, not assumed: at l=45 (this sweep's top of range), the
    incumbent must be at least as good as the Ahuja candidate on BOTH
    judged criteria for the gate to report REJECT with XRz.l named as the
    dominating knob - the same conclusion the design doc reached by hand at
    l=60 (there, 125.4 deg / 24.8 MHz beating the candidate's 89.4 deg /
    5.45 MHz)."""
    slot, netlist_texts = _trimamp_own_loop_slot()
    sim_backend = NgspiceBackend(timeout=120)

    candidate = candidate_from_file(
        AHUJA_INDIRECT_COMP_BODY,
        ports=TRIMAMP_PORTS,
        assumes_scale=1e-6,
        topology_id="ahuja_indirect_comp_test",
    )

    structure_result = check_structure(candidate, slot, netlist_texts)
    assert structure_result.status == "pass", structure_result.detail

    reproduce_result, _addresses = reproduce_characteristics(candidate, slot, netlist_texts, sim_backend)
    assert reproduce_result.status == "pass", reproduce_result.detail
    candidate_measurements = reproduce_result.detail["candidate_measurements"]
    incumbent_measurements = reproduce_result.detail["baseline_measurements"]

    # Sanity check against the design doc's own hand measurement for this
    # EXACT body (Cc=20 at node `ns`, tabulated there as 89.4 deg / trim
    # loop). The doc's number came from a wider/finer sweep
    # (`ac dec 40 1 1g`) than this spec's control block (`ac dec 20 1
    # 100meg`), so this only checks the same ballpark, not an exact match.
    assert candidate_measurements["trim_pm_deg"] == pytest.approx(89.4, abs=15.0)

    comparison_result = scoped_comparison(
        candidate,
        slot,
        netlist_texts,
        sim_backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=5,
        knob_names=[("TRIMAMP.XRz", "l")],
    )

    detail = comparison_result.detail
    # The narrowing is on record, independent of what the sweep found -
    # this is what makes "we bounded the comparison and said so" checkable
    # rather than an unverified claim in a docstring.
    assert detail["knob_names_requested"] == ["TRIMAMP.XRz.l"]
    assert [k["knob"] for k in detail["knobs_swept"]] == ["TRIMAMP.XRz.l"]
    swept_range = detail["knobs_swept"][0]["range"]
    assert swept_range == pytest.approx([5.0, 45.0])

    assert comparison_result.status == "fail", detail
    dominating = detail["dominating_point"]
    assert dominating is not None
    assert dominating["knob"] == "TRIMAMP.XRz.l"

    # The real measured values at the dominating point - reported, not
    # assumed. print() surfaces them under `pytest -s`; they are also
    # reproduced verbatim in task-8-report.md.
    print(
        "\ndominating point: TRIMAMP.XRz.l =",
        dominating["swept_value"],
        "-> trim_pm_deg =",
        dominating["measurements"].get("trim_pm_deg"),
        "trim_ugbw_hz =",
        dominating["measurements"].get("trim_ugbw_hz"),
    )
    print(
        "Ahuja candidate: trim_pm_deg =",
        candidate_measurements.get("trim_pm_deg"),
        "trim_ugbw_hz =",
        candidate_measurements.get("trim_ugbw_hz"),
    )

    # The dominating point must genuinely beat the candidate on BOTH judged
    # axes - not merely "appear in the sweep" (a mutation that reported the
    # first EXCLUDED point, say, would still set dominating_point to
    # something, but that something wouldn't actually beat the candidate on
    # both axes).
    assert dominating["measurements"]["trim_pm_deg"] > candidate_measurements["trim_pm_deg"]
    assert dominating["measurements"]["trim_ugbw_hz"] > candidate_measurements["trim_ugbw_hz"]


def test_the_full_multi_loop_criteria_set_rejects_because_near_ties_no_longer_block_domination():
    """The shipped slot, judged on the SHIPPED criteria - the end-to-end
    proof this sub-project exists for, with no hand-built criteria set.

    This test previously pinned the OPPOSITE fact, and that inversion is the
    point. With a literal, zero-tolerance Pareto '>=', spec_curate_slot.yaml's
    8 criteria across four amps sharing bias rails let NO point in
    TRIMAMP.XRz.l's [5, 45] sweep dominate the Ahuja candidate - so this
    sub-project's own proof case ended in ADMIT with `dominating: None`.
    Two criteria physically decoupled from TRIMAMP's compensation network sat
    a few thousandths of a unit on the wrong side of the comparison at every
    swept point (measured, and re-measured by this test's own assertions
    below):

    - core_phase_margin (BGR_CORE's own loop, coupled to TRIMAMP only through
      shared bias rails): 66.0791 deg at the dominating point vs the
      candidate's 66.0835 - short by 0.0044 deg, i.e. 6.7e-5 relative.
    - trim_loop_gain: 87.5449 dB vs the candidate's 87.5450 - short by
      0.0001 dB, i.e. 1.1e-6 relative. This resistor barely touches DC gain.

    Both gaps are orders of magnitude below the real trade-off in the same
    run (trim_pm_deg 89.4213 -> 99.9033 at the dominating point, +10.5 deg =
    0.117 relative), and neither moves with XRz.l, so no number of extra
    sweep points could ever have closed them. curation.COMPARISON_REL_TOLERANCE
    (1e-3, ~24x above the largest measured noise and ~100x below the real
    improvement) makes both a tie, and the gate then reaches the verdict F1
    reached by hand.

    Mutation this catches: setting COMPARISON_REL_TOLERANCE to 0 (or dropping
    the tolerance from _at_least_as_good) restores `status == "pass"` /
    `dominating_point is None` - verified by doing exactly that."""
    slot, netlist_texts = _full_trimamp_slot()
    sim_backend = NgspiceBackend(timeout=120)

    candidate = candidate_from_file(
        AHUJA_INDIRECT_COMP_BODY,
        ports=TRIMAMP_PORTS,
        assumes_scale=1e-6,
        topology_id="ahuja_indirect_comp_test_full_criteria",
    )

    reproduce_result, addresses = reproduce_characteristics(candidate, slot, netlist_texts, sim_backend)
    assert reproduce_result.status == "pass", reproduce_result.detail
    candidate_measurements = reproduce_result.detail["candidate_measurements"]
    incumbent_measurements = reproduce_result.detail["baseline_measurements"]

    # The same tolerance in the other direction: with a zero-tolerance
    # comparison this run's `addresses` also carried core_phase_margin
    # (+0.0028 deg) and trim_loop_gain (+0.0001 dB) - unverified claims that
    # agents/tuner.py renders straight into the swap-selection prompt. Only
    # the one real improvement may survive.
    assert addresses == ["trim_phase_margin"], addresses

    comparison_result = scoped_comparison(
        candidate,
        slot,
        netlist_texts,
        sim_backend,
        candidate_measurements,
        incumbent_measurements,
        max_knobs=None,
        points=5,
        knob_names=[("TRIMAMP.XRz", "l")],
    )

    detail = comparison_result.detail
    dominating = detail["dominating_point"]
    print("\nfull-criteria dominating point:", dominating)
    print("full-criteria candidate_measurements:", candidate_measurements)

    assert comparison_result.status == "fail"
    assert dominating is not None
    assert dominating["point"] == "swept"
    assert dominating["knob"] == "TRIMAMP.XRz.l"

    # The two former blockers really are within the tolerance (they are
    # near-ties, not wins for the incumbent) - so this rejection rests on a
    # tie plus one real improvement, not on the noise having flipped sign.
    for measurement in ("core_pm_deg", "trim_gain_db"):
        gap = candidate_measurements[measurement] - dominating["measurements"][measurement]
        relative = abs(gap) / abs(candidate_measurements[measurement])
        assert 0 < relative < COMPARISON_REL_TOLERANCE, (measurement, gap, relative)

    # ... and the axis that actually decides it is a real design difference,
    # two orders of magnitude larger.
    real_gap = dominating["measurements"]["trim_pm_deg"] - candidate_measurements["trim_pm_deg"]
    assert real_gap > 10.0
    assert real_gap / candidate_measurements["trim_pm_deg"] > 100 * COMPARISON_REL_TOLERANCE


def test_extracting_buf_p_reproduces_the_shipped_library_entry_under_real_simulation(tmp_path):
    """Brief rule 3 - the other direction of proof. test_curation.py's
    existing test_extracting_from_a_deck_reproduces_the_shipped_library_entry
    already pins that candidate_from_deck's extraction of BUF_P from
    netlist_loops.cir is BYTE-IDENTICAL to
    TOPOLOGY_LIBRARY['folded_cascode_pmos_in_cs'].subckt_body - but that is a
    string comparison, not a simulation. This test proves the same fact
    under real ngspice: swapping the freshly-extracted body back into its
    own slot (BUF_P) and simulating it must reproduce the SAME measured
    value, for every one of the deck's own measurements, as simulating the
    untouched deck - because apply_topology_swap on a byte-identical body
    can only ever regenerate the original text.

    This test drives apply_topology_swap and NgspiceBackend directly rather
    than through check_structure/reproduce_characteristics: check_structure
    correctly REJECTS this exact swap with reason 'identical_body' (see
    topology_match.compatible_swaps - offering to swap a body for one
    byte-identical to itself would not change the circuit, and stage 1 is
    right to refuse a no-op "candidate"). That rejection is orthogonal to
    what this test proves, so it is bypassed here rather than worked
    around; a mutation that corrupted extraction (wrong line range, wrong
    whitespace handling, wrong scope resolution) would still surface here
    as a numerically different simulation."""
    spec = load_spec(SLOT_SPEC_PATH)
    netlist_texts = _load_netlist_texts(spec)
    tb = spec.testbenches[0]
    original_text = netlist_texts[tb.name]

    candidate = candidate_from_deck(original_text, "BUF_P", "buf_p_extract_test")

    shipped = TOPOLOGY_LIBRARY["folded_cascode_pmos_in_cs"]
    assert candidate.subckt_body == shipped.subckt_body
    assert set(candidate.ports) == set(shipped.ports)
    assert candidate.assumes_scale == shipped.assumes_scale

    swapped_text = apply_topology_swap(original_text, "BUF_P", candidate.subckt_body)

    sim_backend = NgspiceBackend(timeout=120)
    original_path = tmp_path / "original.cir"
    swapped_path = tmp_path / "swapped.cir"
    original_path.write_text(original_text)
    swapped_path.write_text(swapped_text)

    original_measurements = sim_backend.run(str(original_path), {"control_block": tb.control_block}).measurements
    swapped_measurements = sim_backend.run(str(swapped_path), {"control_block": tb.control_block}).measurements

    assert original_measurements.keys() == swapped_measurements.keys()
    assert original_measurements  # not vacuously empty
    for name, value in original_measurements.items():
        assert not math.isnan(value)
        assert swapped_measurements[name] == pytest.approx(value, rel=1e-9, abs=1e-9), name
