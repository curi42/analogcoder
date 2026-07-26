* Single swap point for PVT corner / PDK model data used by every
* two_stage_opamp testbench netlist. All four testbench files (.cir) and
* both topologies.py subckt bodies .include this ONE file rather than
* each vendoring their own copy of the PDK include chain - swapping to a
* different corner (or a different PDK entirely, e.g. a company-internal
* corner.inc delivered as an HSPICE .inc file) means editing only this
* file, not every netlist.
*
* Currently: SkyWater sky130 nfet_01v8/pfet_01v8, tt (typical) corner,
* vendored via git submodule at third_party/skywater-pdk-libs-sky130_fd_pr
* (sparse-checked-out to models/, cells/nfet_01v8/, cells/pfet_01v8/,
* cells/cap_mim_m3/, tech/ only - see
* docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md).
.option scale=1.0u
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/lod.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"

* MiM capacitor (Cc/Ca compensation caps) - minimal hand-extracted param
* recipe, not the official include chain. The official chain
* (models/r+c/res_typical__cap_typical.spice ->
* models/sky130_fd_pr__model__r+c.model.spice) pulls in an unrelated
* resistor-cell family not needed here and not in the sparse-checkout;
* these ~10 scalar params were extracted directly from that chain instead.
* Capacitance: C = camimc*w*l + cpmimc*2*(w+l) (verified to ~0.5% accuracy
* against a real ngspice .op run).
.param tol_m3=0.0
.param rm3=0.047 rcvia3=3.41
.param tc1rm3=3.424e-3 tc2rm3=-7.739e-7
.param tc1rvia3=2.366e-3 tc2rvia3=-1.025e-5
.param m3_dw=-0.025u
.param camimc=2.00e-15 cpmimc=0.19e-15
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_1.model.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_2.model.spice"
