import os
import shutil
import subprocess
import tempfile

import pytest

BENCH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")
)

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")

CORNERS = ["pdk_corner.inc", "pdk_corner_ss.inc", "pdk_corner_ff.inc", "pdk_corner_sf.inc", "pdk_corner_fs.inc"]


def _run(deck: str) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "probe.cir")
        with open(path, "w") as f:
            f.write(deck)
        proc = subprocess.run(
            ["ngspice", "-b", path], capture_output=True, text=True, timeout=120, cwd=BENCH
        )
    values = {}
    for line in (proc.stdout + proc.stderr).splitlines():
        parts = line.strip().split("=")
        if len(parts) == 2:
            try:
                values[parts[0].strip()] = float(parts[1].strip())
            except ValueError:
                pass
    return values


def _deck(corner: str = "pdk_corner.inc", pnp_ratio: str = "m=8") -> str:
    return f"""* device probe
.include "{corner}"
Ie1 0 e1 DC 5u
Xq1 0 0 e1 0 sky130_fd_pr__pnp_05v5_W3p40L3p40
Ie8 0 e8 DC 5u
Xq8 0 0 e8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 {pnp_ratio}
Ir 0 rt DC 1u
Xr1 rt 0 0 sky130_fd_pr__res_high_po w=1 l=10
.control
op
let dvbe_mv = (v(e1)-v(e8))*1e3
let rval = v(rt)/1u
print dvbe_mv rval
.endc
.end
"""


def test_pnp_instance_multiplier_gives_the_ptat_delta_vbe():
    # VT*ln(8) at 27C is 53.78mV; the measured 54.59mV includes the model's
    # emitter resistance and ise non-ideality. The point of the assertion is
    # that "m=8" scales emitter area at all - see the companion test below.
    values = _run(_deck())

    assert values["dvbe_mv"] == pytest.approx(54.59, abs=2.0)


def test_pnp_mult_parameter_does_not_scale_emitter_area():
    # sky130's pnp subckt uses "mult" only inside its mismatch expressions,
    # which are identically zero without Monte Carlo. Writing the emitter
    # ratio as mult=8 therefore yields NO delta-Vbe, and a bandgap core built
    # that way has no PTAT current at all - silently, with a plausible-looking
    # netlist.
    values = _run(_deck(pnp_ratio="mult=8"))

    assert abs(values["dvbe_mv"]) < 0.001


def test_res_high_po_sheet_and_head_resistance():
    # 317.4*(l+0.247)/0.999 + 345.83/1.1548 = 3554 ohm for w=1, l=10. The
    # ~300 ohm head term is not negligible for the core's 10.9k R1.
    values = _run(_deck())

    assert values["rval"] == pytest.approx(3554.0, rel=0.02)


@pytest.mark.parametrize("corner", CORNERS)
def test_every_process_corner_include_loads_and_biases_both_devices(corner):
    values = _run(_deck(corner=corner))

    assert values["dvbe_mv"] == pytest.approx(54.6, abs=6.0)
    assert values["rval"] == pytest.approx(3554.0, rel=0.02)
