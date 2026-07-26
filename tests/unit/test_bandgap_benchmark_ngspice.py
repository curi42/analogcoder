import os
import shutil

import pytest

from analogcoder.netlist import parse_netlist, resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend

BENCH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")
)

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")

TESTBENCHES = [
    "netlist.cir",
    "netlist_startup.cir",
    "netlist_psrr.cir",
    "netlist_settling.cir",
    "netlist_loops.cir",
]

DC_TC_CONTROL = """.control
dc temp -40 125 1
meas dc vmax MAX v(vbgout)
meas dc vmin MIN v(vbgout)
meas dc vbgout_v FIND v(vbgout) AT=27
meas dc vbg0_v FIND v(vbg0) AT=27
meas dc vbg1_v FIND v(vbg1) AT=27
meas dc idd FIND i(Vdd) AT=27
let tc_ppm_per_c = (vmax-vmin)/(vbgout_v*165)*1e6
let iq_ua = -1e6*idd
print vbgout_v vbg0_v vbg1_v iq_ua tc_ppm_per_c
.endc"""


def _run(name, control, tmp_path):
    with open(os.path.join(BENCH, name)) as f:
        text = resolve_includes(f.read(), BENCH)
    path = tmp_path / name
    path.write_text(text)
    return NgspiceBackend(timeout=180).run(str(path), {"control_block": control})


def _blocks(name):
    with open(os.path.join(BENCH, name)) as f:
        lines = f.read().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.lower().startswith(".subckt"))
    end = max(i for i, ln in enumerate(lines) if ln.lower().startswith(".ends"))
    return "\n".join(lines[start : end + 1])


def test_dc_tc_testbench_reproduces_the_measured_nominal_operating_point(tmp_path):
    result = _run("netlist.cir", DC_TC_CONTROL, tmp_path)

    assert result.status == "success", result.raw_log[-2000:]
    m = result.measurements
    assert m["vbgout_v"] == pytest.approx(1.2399, abs=0.010)
    assert m["vbg1_v"] == pytest.approx(1.2009, abs=0.010)
    assert m["vbg0_v"] == pytest.approx(0.5003, abs=0.005)
    assert m["tc_ppm_per_c"] == pytest.approx(36.3, abs=5.0)
    assert m["iq_ua"] == pytest.approx(213.1, abs=20.0)


def test_every_testbench_defines_the_same_blocks():
    # orchestrator._apply_to_all pushes one tuning proposal into every
    # testbench netlist, and apply_changes silently skips a refdes it cannot
    # find - so a definition that drifts between files diverges in silence.
    reference = _blocks("netlist.cir")

    for name in TESTBENCHES[1:]:
        assert _blocks(name) == reference, name


def test_all_six_blocks_are_addressable_by_scoped_refdes():
    with open(os.path.join(BENCH, "netlist.cir")) as f:
        parsed = parse_netlist(f.read())

    assert set(parsed.subckts) == {
        "ERRAMP",
        "TRIMAMP",
        "BUF_N",
        "BUF_P",
        "BGR_CORE",
        "BANDGAP",
    }
    # All four amps declare Xt/X1/X2/Xcc/XRz - the whole point of scoping.
    for amp in ("ERRAMP", "TRIMAMP", "BUF_N", "BUF_P"):
        refdes = {c.refdes for c in parsed.subckts[amp].components}
        assert {"Xt", "X1", "X2", "Xcc", "XRz"} <= refdes, amp


def test_an_unqualified_amp_refdes_is_ambiguous_and_a_scoped_one_is_not():
    from analogcoder.netlist import check_refdes_resolution

    with open(os.path.join(BENCH, "netlist.cir")) as f:
        text = f.read()

    ok, feedback = check_refdes_resolution(text, [{"refdes": "XRz", "param": "l", "new_value": "25"}])
    assert ok is False
    assert "ambiguous" in feedback

    ok, _ = check_refdes_resolution(
        text, [{"refdes": "TRIMAMP.XRz", "param": "l", "new_value": "25"}]
    )
    assert ok is True
