"""래퍼 셀 스타일 덱의 합성 픽스처. **모양만** 옮긴 것이고 실제 생산 넷리스트는
이 저장소에 들어오지 않는다.

test_params.py(추적이 무엇을 찾아내는가)와 test_area_limits.py(그 추적으로
게이트가 무엇을 판정하는가)가 같은 덱을 봐야 두 층의 결론을 맞대볼 수 있다.
예전에는 같은 문자열이 두 파일에 그대로 복제돼 있어 한쪽만 고치면 조용히
갈라졌다."""

# 인스턴스 줄에서 크기가 정해지는 기본형: 한 서브회로 정의를 서로 다른
# 파라미터로 두 번 인스턴스화한다.
WRAPPER_DECK = (
    "* synthetic wrapper-cell deck (shape only)\n"
    ".subckt WRAPCELL_A b1 b2 d1 d2 g1 g2 s1 s2\n"
    "ma1 d1 g1 s1 b1 UNITDEV_N_LVT w=wn l=ln m=ma1 nf=nf_n geomod=geomod\n"
    "mb1 d2 g2 s2 b2 UNITDEV_N_LVT w=wn l=ln m=mb1 nf=nf_n geomod=geomod\n"
    ".ends WRAPCELL_A\n"
    "xin1 vss vss dl dr gl gr com com WRAPCELL_A wn=2e-6 ln=3e-6 ma1=4 mb1=4 nf_n=1 geomod=1\n"
    "xin2 vss vss d2l d2r g2l g2r com2 com2 WRAPCELL_A wn=20e-6 ln=3e-6 ma1=2 mb1=2 nf_n=1 geomod=1\n"
    ".end\n"
)

# 같은 단위 셀을 한 래퍼 안에서 두 번 인스턴스화한다 - 단위 셀로 만든 차동
# 쌍의 평범한 모양이다. 하나의 인스턴스 파라미터(wtop)가 **물리적으로 다른 두
# 소자**에 도달하며, 정의 컴포넌트는 하나뿐이다.
SIBLING_INSTANCE_DECK = (
    "* synthetic sibling-instance deck (shape only)\n"
    ".subckt LEAF d g s b\n"
    "ma1 d g s b UNITDEV_N w=wl l=1e-6 m=2\n"
    ".ends LEAF\n"
    ".subckt PAIR d1 d2 g s b\n"
    "xl1 d1 g s b LEAF wl=wtop\n"
    "xl2 d2 g s b LEAF wl=wtop\n"
    ".ends PAIR\n"
    "xtop a b c d e PAIR wtop=2e-6\n"
    ".end\n"
)

# 래퍼 셀 정의가 .include 로만 들어오는 덱. parse_netlist는 include를 따라가지
# 않으므로 이 덱에서는 추적이 원리적으로 불가능하다.
INCLUDE_ONLY_DECK = (
    "* synthetic include-only deck (shape only)\n"
    ".include 'inhouse_cells.inc'\n"
    "xwrap1 d g s b WRAPCELL_A_LVT wn=2e-6 ln=3e-6 ma1=4 nf_n=1\n"
    ".end\n"
)

# R/C의 크기 노브는 위치 인자 값이다. 같은 소자를 하나는 래퍼로 감싸고 하나는
# 벌거벗은 채로 둔다.
POSITIONAL_VALUE_DECK = (
    "* synthetic positional-value deck (shape only)\n"
    ".subckt RCELL a b\n"
    "R1 a b rv\n"
    ".ends RCELL\n"
    "xr1 p q RCELL rv=1k\n"
    "R2 p q 1k\n"
    ".end\n"
)

# 같은 이름을 본문 .param과 .subckt 줄 기본값이 동시에 선언한다 - 어느 쪽이
# 이기는지가 방언마다 달라 이 프로젝트는 "해소 불가"로 둔다.
CONTESTED_NAME_DECK = (
    "* synthetic contested-name deck (shape only)\n"
    ".subckt CELL d g s b wn=10e-6\n"
    ".param wn=60e-6\n"
    "ma1 d g s b UNITDEV_N w=wn l=ln\n"
    ".ends CELL\n"
    "xc1 a b c d CELL ln=1e-6\n"
    ".end\n"
)

# 한 인스턴스 파라미터가 두 소자에 도달하지만 한쪽의 총 폭만 확정된다
# (mb1의 w가 덱 어디에도 없는 kfac를 참조한다). 절반만 판정된 변경이다.
PARTIAL_REACH_DECK = (
    "* synthetic partial-reach deck (shape only)\n"
    ".subckt WRAP_PAIR b1 b2 d1 d2 g1 g2 s1 s2\n"
    "ma1 d1 g1 s1 b1 UNITDEV_N w=wn l=ln m=4\n"
    "mb1 d2 g2 s2 b2 UNITDEV_N w='wn*kfac' l=ln m=4\n"
    ".ends WRAP_PAIR\n"
    "xin1 vss vss dl dr gl gr com com WRAP_PAIR wn=2e-6 ln=3e-6\n"
    ".end\n"
)

# m 토큰이 소자 줄에 **적혀 있는데** 그 이름이 경합해서 해소되지 않는다.
# `.param mm=8` 줄을 빼면 경합이 사라져 m=8이 정상적으로 반영된다.
UNRESOLVABLE_M_DECK = (
    "* synthetic unresolvable-m deck (shape only)\n"
    ".subckt CELL d g s b mm=8\n"
    ".param mm=8\n"
    "ma1 d g s b UNITDEV_N w=wn m=mm\n"
    ".ends CELL\n"
    "xc1 a b c d CELL wn=10e-6\n"
    ".end\n"
)

# 같은 sky130 폴리 저항을 하나는 직접(XRpa), 하나는 래퍼로 감싸(xr -> RCELL_PO.XRp)
# 둔다. 폴리 저항의 티어 기준 치수는 **길이 l**이고(저항값도 면적도 l이 정한다),
# 폭 w는 이 덱에서 두 소자 모두 1로 같다 - 두 경로가 서로 다른 차원을 읽으면
# 같은 성장이 감쌌는지 여부만으로 정반대 판정을 받는다.
SKY130_POLY_RESISTOR_DECK = (
    "* synthetic sky130 poly-resistor deck (shape only)\n"
    ".option scale=1.0u\n"
    ".subckt RCELL_PO a b\n"
    "XRp a b 0 sky130_fd_pr__res_high_po w=1 l=rl\n"
    ".ends RCELL_PO\n"
    ".subckt LADDER a b c d\n"
    "XRpa a b 0 sky130_fd_pr__res_high_po w=1 l=324.74\n"
    "xr c d RCELL_PO rl=324.74\n"
    ".ends LADDER\n"
    "Xl n1 n2 n3 n4 LADDER\n"
    "V1 n1 0 1.8\n"
    ".end\n"
)

# m이 0인 소자를 직접·래퍼 두 벌로 둔다. m<=0은 개수로서 말이 되지 않으므로
# 직접 경로(area_limits.multiplicity)는 1.0으로 잡아 왔다. 추적 경로가 그
# 클램프를 안 하면 총 폭이 0이 되어 **가장 느슨한 티어**를 받는다 - 모르는
# 값을 추측할 때 항상 느슨한 쪽으로 틀린다는 이 저장소의 반복된 실패 모양이다.
ZERO_MULTIPLICITY_DECK = (
    "* synthetic zero-multiplicity deck (shape only)\n"
    ".option scale=1.0u\n"
    ".subckt MCELL d g s b\n"
    "Xm d g s b sky130_fd_pr__nfet_01v8 W=wn L=1 m=0\n"
    ".ends MCELL\n"
    ".subckt HOLD d g s b\n"
    "Xmd d g s b sky130_fd_pr__nfet_01v8 W=30 L=1 m=0\n"
    "xm2 d g s b MCELL wn=30\n"
    ".ends HOLD\n"
    "Xh n1 n2 n3 n4 HOLD\n"
    "V1 n1 0 1.8\n"
    ".end\n"
)
