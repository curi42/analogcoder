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
