from analogcoder.pvt import CornerPoint, all_corners, render_corner_netlist, worst_case_measurements
from analogcoder.spec import Criterion, PVTCorners

NETLIST = """\
* Two-stage CMOS op-amp
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
X1 n1 vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8
Vss vss 0 DC 0
.end
"""

NETLIST_WITH_AC_STIMULUS_ON_VDD = """\
* PSR+ testbench
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
X1 n1 vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8 AC 1
Vss vss 0 DC 0
.end
"""


def test_render_corner_netlist_uses_pdk_corner_inc_unchanged_for_tt():
    rendered = render_corner_netlist(NETLIST, "tt", 1.8, 27, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner.inc"' in rendered


def test_render_corner_netlist_swaps_process_corner_include():
    rendered = render_corner_netlist(NETLIST, "ss", 1.8, 27, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner_ss.inc"' in rendered
    assert "pdk_corner.inc" not in rendered


def test_render_corner_netlist_swaps_corner_include_that_is_already_absolute():
    # cli.py runs every netlist through resolve_includes at load time, so by
    # the time a netlist text reaches the PVT sweep its include is already an
    # absolute path - and the FINAL sweep gets exactly such a text back out of
    # RunState. Matching only the bare relative form would silently leave every
    # corner running the tt models, turning a 45-corner sweep into 45 identical
    # nominal runs that all "pass".
    absolute = NETLIST.replace(
        '.include "pdk_corner.inc"', '.include "/benchmarks/two_stage_opamp/pdk_corner.inc"'
    )

    rendered = render_corner_netlist(absolute, "ss", 1.8, 27, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner_ss.inc"' in rendered
    assert "pdk_corner.inc" not in rendered


def test_render_corner_netlist_injects_temp_directive():
    rendered = render_corner_netlist(NETLIST, "tt", 1.8, -40, "/benchmarks/two_stage_opamp")

    assert ".temp -40" in rendered


def test_render_corner_netlist_sets_vdd_dc_value():
    rendered = render_corner_netlist(NETLIST, "tt", 1.62, 27, "/benchmarks/two_stage_opamp")

    vdd_lines = [line for line in rendered.splitlines() if line.startswith("Vdd")]
    assert vdd_lines == ["Vdd vdd 0 DC 1.62"]


def test_render_corner_netlist_preserves_trailing_ac_clause_on_vdd():
    # netlist_psr_plus.cir's Vdd line has a trailing "AC 1" - the voltage
    # substitution must only touch the DC value token, not the AC magnitude.
    rendered = render_corner_netlist(NETLIST_WITH_AC_STIMULUS_ON_VDD, "tt", 1.98, 27, "/benchmarks/two_stage_opamp")

    vdd_lines = [line for line in rendered.splitlines() if line.startswith("Vdd")]
    assert vdd_lines == ["Vdd vdd 0 DC 1.98 AC 1"]


def test_all_corners_produces_full_cross_product():
    pvt = PVTCorners(process=["tt", "ss"], voltage=[1.62, 1.98], temperature=[-40, 125])

    corners = all_corners(pvt)

    assert len(corners) == 8  # 2 * 2 * 2
    assert CornerPoint(process="tt", voltage=1.62, temperature=-40) in corners
    assert CornerPoint(process="ss", voltage=1.98, temperature=125) in corners


def test_worst_case_measurements_picks_minimum_for_gte_criterion():
    corners = [
        CornerPoint(process="tt", voltage=1.8, temperature=27),
        CornerPoint(process="ss", voltage=1.62, temperature=-40),
    ]
    per_corner_measurements = [{"phase_margin_deg": 62.88}, {"phase_margin_deg": 37.12}]
    criteria = [Criterion(name="phase_margin", measurement="phase_margin_deg", operator=">=", threshold=60.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert measurements == {"phase_margin_deg": 37.12}
    assert worst_corners["phase_margin"]["process"] == "ss"
    assert worst_corners["phase_margin"]["value"] == 37.12


def test_worst_case_measurements_picks_maximum_for_lte_criterion():
    corners = [
        CornerPoint(process="tt", voltage=1.8, temperature=27),
        CornerPoint(process="ff", voltage=1.98, temperature=125),
    ]
    per_corner_measurements = [{"psr_minus_db": -1.43}, {"psr_minus_db": 0.5}]
    criteria = [Criterion(name="psr_minus", measurement="psr_minus_db", operator="<=", threshold=0.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert measurements == {"psr_minus_db": 0.5}
    assert worst_corners["psr_minus"]["process"] == "ff"


def test_worst_case_measurements_skips_criterion_missing_from_all_corners():
    corners = [CornerPoint(process="tt", voltage=1.8, temperature=27)]
    per_corner_measurements = [{"gain_db": 71.09}]
    criteria = [Criterion(name="phase_margin", measurement="phase_margin_deg", operator=">=", threshold=60.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert measurements == {}
    assert worst_corners == {}


def test_worst_case_measurements_fails_criterion_when_any_corner_is_missing_the_measurement():
    # A corner that fails to produce a measurement (e.g. an AC response that
    # never crosses 0dB, so a WHEN-conditioned .meas line finds nothing) is
    # itself evidence the circuit doesn't function correctly at that corner -
    # even if OTHER corners did produce a passing-looking value, the
    # criterion must not silently pass on the subset that happened to
    # measure. Two of three corners produce phase_margin_deg; the third
    # (the "ss" corner) produced nothing.
    corners = [
        CornerPoint(process="tt", voltage=1.8, temperature=27),
        CornerPoint(process="ff", voltage=1.98, temperature=125),
        CornerPoint(process="ss", voltage=1.62, temperature=-40),
    ]
    per_corner_measurements = [{"phase_margin_deg": 62.88}, {"phase_margin_deg": 113.0}, {}]
    criteria = [Criterion(name="phase_margin", measurement="phase_margin_deg", operator=">=", threshold=60.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert "phase_margin_deg" not in measurements
    assert worst_corners["phase_margin"]["process"] == "ss"
    assert worst_corners["phase_margin"]["value"] is None


def test_two_sided_window_keeps_both_worst_cases_separate():
    # A min/max window is two criteria over ONE measurement with opposite
    # operators. Keying the worst-case pool by measurement name lets the
    # second criterion's value overwrite the first's, so one side of the
    # window is evaluated against the wrong corner - and a violation on that
    # side becomes invisible. benchmarks/bandgap is the first spec here with
    # two-sided windows, which is how this surfaced.
    corners = [
        CornerPoint(process="tt", voltage=1.8, temperature=27),
        CornerPoint(process="ff", voltage=1.98, temperature=27),
        CornerPoint(process="ss", voltage=1.62, temperature=27),
    ]
    per_corner_measurements = [{"vbgout_v": 1.24}, {"vbgout_v": 1.10}, {"vbgout_v": 1.30}]
    criteria = [
        Criterion(name="vbgout_min", measurement="vbgout_v", operator=">=", threshold=1.20),
        Criterion(name="vbgout_max", measurement="vbgout_v", operator="<=", threshold=1.28),
    ]

    _, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert worst_corners["vbgout_min"]["value"] == 1.10
    assert worst_corners["vbgout_min"]["process"] == "ff"
    assert worst_corners["vbgout_max"]["value"] == 1.30
    assert worst_corners["vbgout_max"]["process"] == "ss"


def test_full_sweep_verdict_fails_the_low_side_of_a_two_sided_window():
    class _StubBackend:
        def __init__(self, values):
            self.values = list(values)

        def run(self, netlist_path, testbench_config):
            from analogcoder.simulators.base import RawSimResult

            return RawSimResult(
                status="success",
                measurements={"vbgout_v": self.values.pop(0)},
                raw_log="",
                warnings=[],
            )

    from types import SimpleNamespace

    from analogcoder.pvt import run_full_pvt_sweep

    criteria = [
        Criterion(name="vbgout_min", measurement="vbgout_v", operator=">=", threshold=1.20),
        Criterion(name="vbgout_max", measurement="vbgout_v", operator="<=", threshold=1.28),
    ]
    tb = SimpleNamespace(
        name="dc", netlist_path="/tmp/x.cir", control_block=".control\nop\n.endc", criteria=criteria
    )
    spec = SimpleNamespace(
        testbenches=[tb],
        canonical=tb,
        all_criteria=criteria,
        pvt_corners=PVTCorners(process=["tt", "ff"], voltage=[1.8], temperature=[27]),
    )
    # ff dips to 1.10, which violates the >=1.20 side while the <=1.28 side is
    # satisfied by both corners.
    backend = _StubBackend([1.24, 1.10])

    result = run_full_pvt_sweep({"dc": "* netlist\n.end\n"}, spec, backend)

    failed = {c["name"] for c in result["criteria"] if not c["pass"]}
    assert failed == {"vbgout_min"}
