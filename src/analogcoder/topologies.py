from dataclasses import dataclass


@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str  # lines between ".subckt NAME ports" and ".ends NAME"
    addresses: list[str]  # criterion names this is known to help; informational only, used in the tuner prompt


TOPOLOGY_LIBRARY: dict[str, Topology] = {
    "miller_basic": Topology(
        id="miller_basic",
        description="Standard two-stage Miller-compensated CMOS op-amp (sky130), no nulling resistor.",
        addresses=[],
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
}
