from dataclasses import dataclass


@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str  # lines between ".subckt NAME ports" and ".ends NAME"
    addresses: list[str]  # criterion names this is known to help; informational only, used in the tuner prompt
    ports: list[str]  # ports this body requires, in the source block header's order
    assumes_scale: float  # the .option scale (in metres) this body's geometry numbers assume


TOPOLOGY_LIBRARY: dict[str, Topology] = {
    "miller_basic": Topology(
        id="miller_basic",
        description="Standard two-stage Miller-compensated CMOS op-amp (sky130), no nulling resistor.",
        addresses=[],
        ports=["vinp", "vinn", "vout", "vdd", "vss"],
        assumes_scale=1e-6,
        subckt_body="""\
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
""",
    ),
    "miller_nulling_resistor": Topology(
        id="miller_nulling_resistor",
        description=(
            "Two-stage Miller-compensated CMOS op-amp (sky130) with a nulling resistor Rz "
            "(220kOhm, empirically validated - see the design spec's Rz sweep) in series "
            "with Cc, cancelling the right-half-plane zero. On this sizing, improves phase "
            "margin AND unity-gain bandwidth simultaneously relative to no-Rz, rather than "
            "the usual bandwidth-for-phase-margin trade-off."
        ),
        addresses=["phase_margin"],
        ports=["vinp", "vinn", "vout", "vdd", "vss"],
        assumes_scale=1e-6,
        subckt_body="""\
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA cczz sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Rz   cczz vout 220000
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
""",
    ),
    "folded_cascode_nmos_in_cs": Topology(
        id="folded_cascode_nmos_in_cs",
        description=(
            "NMOS-input folded cascode first stage with a PMOS common-source output "
            "stage, Miller-compensated with a nulling resistor. The 9-port bias "
            "interface (nbias/ncas/pbias/pcas) is supplied externally. Use when the "
            "input common mode sits comfortably above an NMOS pair's Vgs."
        ),
        addresses=[],
        ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
        assumes_scale=1e-6,
        subckt_body="""\
Xt   tail nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X1   nx   vinn tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
X2   ny   vinp tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
Xp1  nx   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xp2  ny   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc1  np   pcas  nx   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc2  outA pcas  ny   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xn1  np   ncas  nr   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xn2  outA ncas  ns   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm1  nr   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm2  ns   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X6   vout outA  vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
X7   vout nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xcc  nz   outA  nz   nz  sky130_fd_pr__pfet_01v8 L=40 W=40
XRz  vout nz 0 sky130_fd_pr__res_high_po w=1 l=15
""",
    ),
    "folded_cascode_pmos_in_cs": Topology(
        id="folded_cascode_pmos_in_cs",
        description=(
            "PMOS-input COMPLEMENTARY folded cascode (NMOS folding sinks, NMOS "
            "cascodes, cascoded PMOS mirror on top) with an NMOS common-source "
            "output stage. Same 9-port bias interface as the NMOS-input variant. "
            "Use when the input common mode is too low for an NMOS pair: measured "
            "on this deck, an NMOS-input fold buffering a 0.5V node leaves only "
            "10.1mV across its tail current source, and widening the input pair "
            "cannot recover it (Vgs_n has a Vth floor)."
        ),
        addresses=["buf0_loop_gain"],
        ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
        assumes_scale=1e-6,
        subckt_body="""\
Xt   tail pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=24
X1   nx   vinn tail  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
X2   ny   vinp tail  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
Xn1  nx   nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xn2  ny   nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xc1  np   ncas  nx   vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xc2  outA ncas  ny   vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xp1  np   pcas  nr   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xp2  outA pcas  ns   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xm1  nr   np    vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xm2  ns   np    vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
X6   vout outA  vss  vss sky130_fd_pr__nfet_01v8 L=1 W=20
X7   vout pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=24
Xcc  nz   outA  nz   nz  sky130_fd_pr__pfet_01v8 L=40 W=40
XRz  vout nz 0 sky130_fd_pr__res_high_po w=1 l=15
Xcl  vss  vout  vss  vss sky130_fd_pr__nfet_01v8 L=20 W=20
""",
    ),
}
