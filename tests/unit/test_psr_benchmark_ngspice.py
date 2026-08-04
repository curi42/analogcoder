import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def _load_two_stage_opamp_spec():
    return load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))


def test_spec_declares_four_testbenches_with_expected_criteria():
    spec = _load_two_stage_opamp_spec()

    assert [tb.name for tb in spec.testbenches] == [
        "ac_loop_gain", "psr_plus", "psr_minus", "settling_time",
    ]

    psr_plus = next(tb for tb in spec.testbenches if tb.name == "psr_plus")
    assert psr_plus.criteria[0].measurement == "psr_plus_db"
    assert psr_plus.criteria[0].operator == "<="
    assert psr_plus.criteria[0].threshold == -10.0

    psr_minus = next(tb for tb in spec.testbenches if tb.name == "psr_minus")
    assert psr_minus.criteria[0].measurement == "psr_minus_db"
    assert psr_minus.criteria[0].operator == "<="
    assert psr_minus.criteria[0].threshold == 0.0


def test_baseline_netlist_matches_validated_psr_measurements():
    # Real ngspice measurements re-taken on 2026-08-04, after the bias chain
    # was changed from a self-biased beta-multiplier to a resistor+diode
    # reference - see docs/superpowers/specs/2026-08-04-tso-bias-fix-results.md.
    # The earlier numbers (-15.40 / -1.43) came from the sky130 PDK migration's
    # Validation section and no longer describe this deck: they were measured
    # on a circuit with three DC solutions, so which one they described was
    # never established. This test exists to catch unintentional drift in the
    # committed .cir files - not to re-derive the thresholds.
    spec = _load_two_stage_opamp_spec()
    backend = NgspiceBackend()

    psr_plus = next(tb for tb in spec.testbenches if tb.name == "psr_plus")
    result = backend.run(psr_plus.netlist_path, {"control_block": psr_plus.control_block})
    assert result.status == "success"
    assert -12.9 <= result.measurements["psr_plus_db"] <= -12.6

    psr_minus = next(tb for tb in spec.testbenches if tb.name == "psr_minus")
    result = backend.run(psr_minus.netlist_path, {"control_block": psr_minus.control_block})
    assert result.status == "success"
    assert -1.1 <= result.measurements["psr_minus_db"] <= -0.85


def test_psr_plus_and_psr_minus_subckt_bodies_match_main_testbench():
    # Enforces the invariant this whole feature depends on: tuning changes
    # applied independently to each testbench file only stay consistent if
    # the OPAMP2STAGE subckt text is byte-identical across all four.
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
