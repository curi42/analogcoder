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
