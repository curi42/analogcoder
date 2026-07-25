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
        description="Standard two-stage Miller-compensated CMOS op-amp, no nulling resistor.",
        addresses=[],
        subckt_body="""\
Iref nb1 vdd 100u
M9 nb1 nb1 vdd vdd PMOSG W=20u L=1u

M1 n1   vinn tail vdd PMOSG W=40u L=1u
M2 outA vinp tail vdd PMOSG W=40u L=1u

M3 n1   n1   vss vss NMOSG W=20u L=1u
M4 outA n1   vss vss NMOSG W=20u L=1u

M5 tail nb1 vdd vdd PMOSG W=40u L=1u

M6 vout outA vss vss NMOSG W=40u L=1u
M7 vout nb1  vdd vdd PMOSG W=60u L=1u

Cc outA vout 2p
Ca outA 0 0.3p
""",
    ),
    "miller_nulling_resistor": Topology(
        id="miller_nulling_resistor",
        description=(
            "Two-stage Miller-compensated CMOS op-amp with a nulling resistor Rz "
            "in series with Cc, cancelling the right-half-plane zero. Improves "
            "phase margin substantially without the unity-gain-bandwidth loss "
            "that increasing Cc alone causes."
        ),
        addresses=["phase_margin"],
        subckt_body="""\
Iref nb1 vdd 100u
M9 nb1 nb1 vdd vdd PMOSG W=20u L=1u

M1 n1   vinn tail vdd PMOSG W=40u L=1u
M2 outA vinp tail vdd PMOSG W=40u L=1u

M3 n1   n1   vss vss NMOSG W=20u L=1u
M4 outA n1   vss vss NMOSG W=20u L=1u

M5 tail nb1 vdd vdd PMOSG W=40u L=1u

M6 vout outA vss vss NMOSG W=40u L=1u
M7 vout nb1  vdd vdd PMOSG W=60u L=1u

Cc outA vnull 2p
Rz vnull vout 500
Ca outA 0 0.3p
""",
    ),
}
