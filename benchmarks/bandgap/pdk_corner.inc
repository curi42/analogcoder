* Single swap point for PVT corner / PDK model data used by every bandgap
* testbench netlist. All five testbench files .include this ONE file, so
* swapping corner (or PDK) means editing only this file.
*
* Currently: SkyWater sky130 tt (typical) corner - nfet_01v8, pfet_01v8,
* pnp_05v5 and res_high_po - vendored via the git submodule at
* third_party/skywater-pdk-libs-sky130_fd_pr.
*
* nonfet.spice carries dkispp5x/dkbfpp5x, the PNP's process-corner knobs (bf
* varies 0.46x to 1.79x across corners, a real stress on the core), plus the
* junction-cap multipliers the pnp model references. The tt corner's nonfet
* data lives under models/corners/wafer/, not models/corners/tt/.
.option scale=1.0u
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/lod.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/corners/wafer/nonfet.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"

* Vertical substrate PNP. The three *_slope params select nominal (no Monte
* Carlo); without them the model's mismatch expressions are undefined. Note
* the 1:8 emitter ratio a bandgap needs is the INSTANCE multiplier m=8 - this
* subckt's own "mult" parameter scales only the mismatch terms and yields
* zero delta-Vbe.
.param sky130_fd_pr__pnp_05v5_W3p40L3p40__bf_slope = 0.0
.param sky130_fd_pr__pnp_05v5_W3p40L3p40__is_slope = 0.0
.param sky130_fd_pr__pnp_05v5_W3p40L3p40__xti_slope = 0.0
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pnp_05v5/sky130_fd_pr__pnp_05v5_W3p40L3p40.model.spice"

* High-sheet poly resistor (rsheet 317.4 ohm/sq, tc1 -4.3e-4, tc2 12e-6).
* These seven scalars are extracted rather than pulled in via the official
* chain (models/sky130_fd_pr__model__linear.model.spice), which .includes the
* unrelated res_xhigh_po cell family that is not in the sparse-checkout - the
* same hand-extraction precedent the MiM cap already sets in
* benchmarks/two_stage_opamp/pdk_corner.inc. The resistor process corner is
* held at typical for every FET corner: Rp and R1 are the same material, so
* the ratio that sets TC cancels it to first order.
.param sky130_fd_pr__res_high_po__slope_spectre = 0.0
.param sky130_fd_pr__res_high_po__con_slope_spectre = 0.0
.param sky130_fd_pr__res_high_po__var = 0.0
.param tc1rpolybody = 0.514e-3
.param tc2rpolybody = 0.122e-5
.param crpf_precision = 1.06e-04
.param crpfsw_precision_1_1 = 5.04e-11
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parasitics/sky130_fd_pr__model__parasitic__res_po.model.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/res_high_po/sky130_fd_pr__res_high_po.model.spice"
