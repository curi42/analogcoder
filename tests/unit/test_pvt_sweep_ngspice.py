import os

from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import PVTCorners, load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def test_run_full_pvt_sweep_against_a_small_representative_corner_set():
    # Full 45-corner sweep is exercised manually (see the design spec's
    # Testing section) - this test uses a small, fast, real-ngspice subset
    # (2 corners x 4 testbenches = 8 real simulations) to verify the whole
    # render -> run -> aggregate -> evaluate pipeline end to end.
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec_pvt.yaml"))
    spec.pvt_corners = PVTCorners(process=["tt", "ss"], voltage=[1.8], temperature=[27])

    netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            netlist_texts[tb.name] = f.read()

    result = run_full_pvt_sweep(netlist_texts, spec, NgspiceBackend())

    assert "overall_pass" in result
    assert len(result["criteria"]) == len(spec.all_criteria)
    # phase_margin is one of the criteria this small corner set actually
    # covers (ac_loop_gain testbench) - its worst-case corner must be
    # either tt or ss (the two corners this test swept), never a corner
    # outside the sweep.
    phase_margin_corner = result["worst_case_corners"]["phase_margin"]
    assert phase_margin_corner["process"] in ("tt", "ss")


def test_run_full_pvt_sweep_with_single_point_corner_matches_nominal_baseline():
    # A 1-corner "sweep" (one process, one voltage, one temperature value)
    # is a degenerate but valid case - no special-casing needed in
    # run_full_pvt_sweep. At tt/1.8V/27C, this reduces to the miller_basic
    # topology baseline (see the sky130 PDK migration design spec's
    # Validation section): phase_margin fails by design (34.56° < 60°
    # threshold) to trigger the topology-swap mechanism, so overall_pass
    # must be False here.
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec_pvt.yaml"))
    spec.pvt_corners = PVTCorners(process=["tt"], voltage=[1.8], temperature=[27])

    netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            netlist_texts[tb.name] = f.read()

    result = run_full_pvt_sweep(netlist_texts, spec, NgspiceBackend())

    # miller_basic topology fails phase_margin by design; this is the baseline
    # for the topology-swap mechanism in the orchestrator
    assert result["overall_pass"] is False
    phase_margin_result = next(c for c in result["criteria"] if c["name"] == "phase_margin")
    assert phase_margin_result["pass"] is False
