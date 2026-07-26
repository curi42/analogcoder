import os
import shutil

import pytest

from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")
)

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")


def _simulate_all(spec, tmp_path):
    measurements = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            text = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
        path = tmp_path / os.path.basename(tb.netlist_path)
        path.write_text(text)
        result = NgspiceBackend(timeout=180).run(str(path), {"control_block": tb.control_block})
        assert result.status == "success", (tb.name, result.raw_log[-2000:])
        measurements.update(result.measurements)
    return measurements


def _failed(spec, tmp_path):
    verdict = evaluate_criteria(_simulate_all(spec, tmp_path), spec.all_criteria)
    return verdict, {c["name"] for c in verdict["criteria"] if not c["pass"]}


def test_baseline_spec_passes_at_nominal(tmp_path):
    spec = load_spec(os.path.join(BENCH, "spec.yaml"))

    verdict, failed = _failed(spec, tmp_path)

    assert verdict["overall_pass"] is True, sorted(failed)


def test_tc_seed_fails_at_nominal(tmp_path):
    # The coupled seed: Rp/R1 sets TC but also moves vbgout and vbg1, so the
    # tuner cannot fix this one in isolation. Measured: l=324.74 -> 36.30ppm
    # (fails), l=321.3 -> 29.30ppm (passes) with vbgout 1.2389 -> 1.2334.
    spec = load_spec(os.path.join(BENCH, "spec_seed_tc.yaml"))

    verdict, failed = _failed(spec, tmp_path)

    assert verdict["overall_pass"] is False
    assert failed == {"temperature_coefficient"}


def test_trim_pm_seed_fails_at_nominal(tmp_path):
    # Exactly one correct move: raise TRIMAMP.XRz's length. Measured l=15 ->
    # 81.1 deg (fails), l=25 -> 98.2 (passes), l=90 -> 88.3 (overshoot loses
    # margin again). Every other criterion already passes, so a proposal aimed
    # at another block is a targeting miss.
    spec = load_spec(os.path.join(BENCH, "spec_seed_trim_pm.yaml"))

    verdict, failed = _failed(spec, tmp_path)

    assert verdict["overall_pass"] is False
    assert failed == {"trim_phase_margin"}


def test_buf0_droop_seed_fails_at_nominal_and_is_local_to_buf_p(tmp_path):
    # Measured: 19.93mV droop at nominal, failing <=15. Reachable within the
    # area gate via BUF_P.X6.W 20 -> 55 (14.79mV, confirmed by a full run) or
    # BUF_P.Xt.W 24 -> 72 (2.34mV); BUF_P.Xcl saturates at 16.79mV at its own
    # 3.0x limit and cannot get there. Growing BUF_N.Xcl instead moves only
    # vbg1's droop (24.12 -> 23.97mV) and leaves vbg0 untouched, so this
    # criterion really does localise to one block.
    spec = load_spec(os.path.join(BENCH, "spec_seed_buf0_droop.yaml"))

    verdict, failed = _failed(spec, tmp_path)

    assert verdict["overall_pass"] is False
    assert failed == {"vbg0_droop"}


def test_pvt_spec_declares_the_full_supply_axis():
    spec = load_spec(os.path.join(BENCH, "spec_pvt.yaml"))

    assert spec.pvt_corners.voltage == [1.62, 1.8, 1.98]
    assert spec.pvt_corners.process == ["tt", "ss", "ff", "sf", "fs"]
    assert spec.pvt_corners.temperature == [-40, 27, 125]


def test_every_seed_differs_from_the_baseline_in_exactly_one_threshold():
    # A seed that moved two thresholds would stop being a single-block target,
    # which is the whole measurement this benchmark exists to make.
    base = load_spec(os.path.join(BENCH, "spec.yaml"))
    base_thresholds = {c.name: c.threshold for c in base.all_criteria}

    for name in ("spec_seed_tc.yaml", "spec_seed_trim_pm.yaml", "spec_seed_buf0_droop.yaml"):
        seed = load_spec(os.path.join(BENCH, name))
        seed_thresholds = {c.name: c.threshold for c in seed.all_criteria}

        assert set(seed_thresholds) == set(base_thresholds), name
        differing = [k for k in base_thresholds if base_thresholds[k] != seed_thresholds[k]]
        assert len(differing) == 1, (name, differing)
