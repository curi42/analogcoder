* Corner-specific variant of pdk_corner.inc for the "sf" (slow-fast) process
* corner - part of the PVT corner sweep (see
* docs/superpowers/specs/2026-07-26-pvt-corner-verification-design.md).
* Byte-identical to pdk_corner.inc except the nfet_01v8/pfet_01v8 corner
* include lines below - the mismatch/lod includes are corner-independent
* (confirmed via sky130's own models/sky130.lib.spice, which references the
* identically-named mismatch file from every corner's .lib section).
.option scale=1.0u
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/lod.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__sf.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__sf.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"

* MiM capacitor (Cc/Ca compensation caps) - minimal hand-extracted param
* recipe, not the official include chain. The official chain
* (models/r+c/res_typical__cap_typical.spice ->
* models/sky130_fd_pr__model__r+c.model.spice) pulls in an unrelated
* resistor-cell family not needed here and not in the sparse-checkout;
* these ~10 scalar params were extracted directly from that chain instead.
* Capacitance: C = camimc*w*l + cpmimc*2*(w+l) (verified to ~0.5% accuracy
* against a real ngspice .op run). Fixed at "typical" for every process
* corner - MiM RC corner sweeping is out of scope for this design.
.param tol_m3=0.0
.param rm3=0.047 rcvia3=3.41
.param tc1rm3=3.424e-3 tc2rm3=-7.739e-7
.param tc1rvia3=2.366e-3 tc2rvia3=-1.025e-5
.param m3_dw=-0.025u
.param camimc=2.00e-15 cpmimc=0.19e-15
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_1.model.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_2.model.spice"
