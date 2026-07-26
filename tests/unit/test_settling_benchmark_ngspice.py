import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def _load_two_stage_opamp_spec():
    return load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))


def test_spec_declares_four_testbenches_including_settling_time():
    spec = _load_two_stage_opamp_spec()

    assert [tb.name for tb in spec.testbenches] == [
        "ac_loop_gain", "psr_plus", "psr_minus", "settling_time",
    ]

    settling = next(tb for tb in spec.testbenches if tb.name == "settling_time")
    assert {c.name: (c.measurement, c.operator, c.threshold) for c in settling.criteria} == {
        "settling_time_hi": ("t_hi_last", "<=", 0.0000028),
        "settling_time_lo": ("t_lo_last", "<=", 0.0000028),
    }


def test_baseline_netlist_matches_validated_settling_measurements():
    # These are the real ngspice measurements recorded in
    # docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md's
    # Validation section for the sky130 miller_basic subckt (Cc MiM cap,
    # w=12.05/l=12.05). This test exists to catch unintentional drift in
    # the committed .cir file - not to re-derive the thresholds.
    spec = _load_two_stage_opamp_spec()
    settling = next(tb for tb in spec.testbenches if tb.name == "settling_time")
    backend = NgspiceBackend()

    result = backend.run(settling.netlist_path, {"control_block": settling.control_block})

    assert result.status == "success"
    assert 2.4e-6 <= result.measurements["t_hi_last"] <= 2.55e-6
    assert 2.2e-6 <= result.measurements["t_lo_last"] <= 2.32e-6


def test_settling_subckt_body_matches_other_three_testbenches():
    # Enforces the invariant this whole multi-testbench feature depends on:
    # tuning changes applied independently to each testbench file only stay
    # consistent if the OPAMP2STAGE subckt text is byte-identical across all
    # four two_stage_opamp testbenches.
    spec = _load_two_stage_opamp_spec()
    bodies = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            text = f.read()
        start = text.index(".subckt OPAMP2STAGE")
        end = text.index(".ends OPAMP2STAGE") + len(".ends OPAMP2STAGE")
        bodies[tb.name] = text[start:end]

    assert (
        bodies["ac_loop_gain"]
        == bodies["psr_plus"]
        == bodies["psr_minus"]
        == bodies["settling_time"]
    )
