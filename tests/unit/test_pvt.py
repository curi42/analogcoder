from analogcoder.pvt import render_corner_netlist

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
