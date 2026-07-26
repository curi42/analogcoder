import os

from analogcoder.simulators.ngspice import NgspiceBackend

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def test_pdk_corner_inc_loads_nfet_pfet_and_mim_cap_cleanly():
    # Written alongside the real benchmark files (not tmp_path) so the
    # relative `.include "pdk_corner.inc"` here, and pdk_corner.inc's own
    # relative "../../third_party/..." includes, resolve exactly the way
    # they do for the real testbench netlists: NgspiceBackend runs ngspice
    # with cwd set to the netlist's own directory (Task 1's fix).
    smoke_path = os.path.join(BENCHMARK_DIR, "_pdk_corner_smoke_test.cir")
    with open(smoke_path, "w") as f:
        f.write(
            "* pdk_corner.inc smoke test - not a real benchmark testbench\n"
            '.include "pdk_corner.inc"\n'
            "Vdd vdd 0 DC 1.8\n"
            "Vss vss 0 DC 0\n"
            "Xn n vdd vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4\n"
            "Xp p vss vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=4\n"
            "Xc n p sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1\n"
            ".end\n"
        )

    try:
        backend = NgspiceBackend()
        # A plain "op" + "print" control block never populates
        # result.measurements (NgspiceBackend only captures lines matching
        # "name = number", and ngspice's "print v(n)" emits "v(n) = ..." -
        # the parentheses aren't \w characters, so the regex never matches,
        # and status would stay "error" even on a fully working include
        # chain). Use a tiny DC sweep + ".meas dc" instead, which does
        # produce a matching "n_val = <number>" line - verified directly
        # against this exact netlist during plan review.
        result = backend.run(
            smoke_path,
            {"control_block": ".control\ndc Vdd 1.7 1.9 0.1\nmeas dc n_val find v(n) at=1.8\n.endc"},
        )
    finally:
        os.remove(smoke_path)

    assert result.status == "success"
    assert "could not find" not in result.raw_log.lower()
    assert "undefined parameter" not in result.raw_log.lower()
