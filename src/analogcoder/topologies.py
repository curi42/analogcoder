from dataclasses import dataclass


@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str  # lines between ".subckt NAME ports" and ".ends NAME"
    addresses: list[str]  # criterion names this is known to help; informational only, used in the tuner prompt
    ports: list[str]  # ports this body requires, in the source block header's order
    assumes_scale: float  # the .option scale (in metres) this body's geometry numbers assume
    # 이 항목이 어디서 왔는가 - "extracted"(실제 통과한 덱에서 뽑음) |
    # "file"(다른 어딘가의 완성된 SPICE 파일에서 그대로 가져옴) |
    # "authored"(사람/에이전트가 직접 작성). 큐레이션 파이프라인(F2)이 항목을
    # 어떤 검증 절차에 태울지 이 필드로 가른다 - 파일에서 값을 추측하지 않는다.
    provenance: str
    # 이 본문이 어느 수준까지 검증됐는가 - "nominal"(한 지점) | "corners"
    # (다지점 PVT 스윕). **네 항목이 전부 "corners"였고, 2026-08-04 에 그중 둘이
    # 실측으로 반증되어 "nominal"로 내려갔다** - `miller_basic` 과
    # `miller_nulling_resistor`. 두 bandgap 항목은 재지 않았으므로 그대로 두었다:
    # 재지 않은 것을 내리는 것도 재지 않은 것을 올리는 것과 같은 종류의 주장이다.
    # 이 필드는 `agents/tuner.py` 가 튜너 프롬프트에 그대로 싣는다.
    verified_at: str


TOPOLOGY_LIBRARY: dict[str, Topology] = {
    "miller_basic": Topology(
        id="miller_basic",
        description="Standard two-stage Miller-compensated CMOS op-amp (sky130), no nulling resistor.",
        addresses=[],
        ports=["vinp", "vinn", "vout", "vdd", "vss"],
        assumes_scale=1e-6,
        provenance="extracted",
        # 2026-08-04: "corners" -> "nominal". 이 본문은 `two_stage_opamp` 자신의
        # 본문이고, 그 덱은 `spec_pvt.yaml` 의 45 코너에서 **0/45** 로 전체 통과한
        # 적이 없다 - 즉 "corners" 는 이 항목이 출하된 이래 참인 적이 없었다.
        # 이 필드는 `agents/tuner.py` 가 튜너에게 그대로 보여주는 주장이므로
        # 재지 않은 것을 크게 적어 둘 자리가 아니다. 근거:
        # docs/superpowers/specs/2026-08-04-tso-bias-fix-results.md.
        verified_at="nominal",
        subckt_body="""\
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rbias vdd nbias 1Meg

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
            "(160kOhm, re-derived by sweep on 2026-08-04 after the bias change) in series "
            "with Cc, cancelling the right-half-plane zero. On this sizing, improves phase "
            "margin AND unity-gain bandwidth simultaneously relative to no-Rz, rather than "
            "the usual bandwidth-for-phase-margin trade-off."
        ),
        addresses=["phase_margin"],
        ports=["vinp", "vinn", "vout", "vdd", "vss"],
        assumes_scale=1e-6,
        provenance="extracted",
        # 2026-08-04: "corners" -> "nominal", 그리고 이것은 이번 변경이 깨뜨린 것이
        # **아니다**. 변경 전(옛 바이어스 + Rz=220k)에도 45 코너 중 `phase_margin
        # >= 62` 는 12 뿐이고 **NaN 이 7 개**, 범위가 3.98-132.12 도였다(132 도는
        # 래치 상태에서 증폭기가 추종기가 된 것이라 좋은 코너가 아니다). 변경 후
        # (Rz=160k)는 6/45 로 수는 줄지만 **NaN 0 개**, 범위 46.17-72.11 도로 모든
        # 코너에서 일관된 증폭기다 - 숫자는 낮아지고 의미가 생겼다.
        # 근거: docs/superpowers/specs/2026-08-04-tso-bias-fix-results.md.
        verified_at="nominal",
        subckt_body="""\
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rbias vdd nbias 1Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA cczz sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Rz   cczz vout 160000
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
        provenance="extracted",
        verified_at="corners",
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
        provenance="extracted",
        verified_at="corners",
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
