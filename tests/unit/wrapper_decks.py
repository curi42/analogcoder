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
    ".subckt WRAP_PAIR_TN33 b1 b2 d1 d2 g1 g2 s1 s2\n"
    "ma1 d1 g1 s1 b1 TN33_LVT w=wn l=ln m=ma1 nf=nf_n geomod=geomod\n"
    "mb1 d2 g2 s2 b2 TN33_LVT w=wn l=ln m=mb1 nf=nf_n geomod=geomod\n"
    ".ends WRAP_PAIR_TN33\n"
    "xin1 vss vss dl dr gl gr com com WRAP_PAIR_TN33 wn=2e-6 ln=3e-6 ma1=4 mb1=4 nf_n=1 geomod=1\n"
    "xin2 vss vss d2l d2r g2l g2r com2 com2 WRAP_PAIR_TN33 wn=20e-6 ln=3e-6 ma1=2 mb1=2 nf_n=1 geomod=1\n"
    ".end\n"
)

# 같은 단위 셀을 한 래퍼 안에서 두 번 인스턴스화한다 - 단위 셀로 만든 차동
# 쌍의 평범한 모양이다. 하나의 인스턴스 파라미터(wtop)가 **물리적으로 다른 두
# 소자**에 도달하며, 정의 컴포넌트는 하나뿐이다.
SIBLING_INSTANCE_DECK = (
    "* synthetic sibling-instance deck (shape only)\n"
    ".subckt LEAF d g s b\n"
    "ma1 d g s b TN33 w=wl l=1e-6 m=2\n"
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
    "xwrap1 d g s b WRAP_PAIR_TN33_LVT wn=2e-6 ln=3e-6 ma1=4 nf_n=1\n"
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
    "ma1 d g s b TN33 w=wn l=ln\n"
    ".ends CELL\n"
    "xc1 a b c d CELL ln=1e-6\n"
    ".end\n"
)
