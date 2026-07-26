import os

import pytest

from analogcoder.simulators.ngspice import NgspiceBackend

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


@pytest.mark.parametrize("corner", ["ss", "ff", "sf", "fs"])
def test_corner_specific_pdk_include_loads_cleanly(tmp_path, corner):
    abs_benchmark_dir = os.path.abspath(BENCHMARK_DIR)
    include_path = os.path.join(abs_benchmark_dir, f"pdk_corner_{corner}.inc")

    smoke_path = tmp_path / "_pvt_corner_smoke_test.cir"
    smoke_path.write_text(
        f"* pdk_corner_{corner}.inc smoke test - not a real benchmark testbench\n"
        f'.include "{include_path}"\n'
        "Vdd vdd 0 DC 1.8\n"
        "Vss vss 0 DC 0\n"
        "Xn n vdd vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4\n"
        "Xp p vss vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=4\n"
        "Xc n p sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1\n"
        ".end\n"
    )

    backend = NgspiceBackend()
    result = backend.run(
        str(smoke_path),
        {"control_block": ".control\ndc Vdd 1.7 1.9 0.1\nmeas dc n_val find v(n) at=1.8\n.endc"},
    )

    assert result.status == "success"
    assert "could not find" not in result.raw_log.lower()
    assert "undefined parameter" not in result.raw_log.lower()
