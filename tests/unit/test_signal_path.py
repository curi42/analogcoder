from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure

CHAIN = (
    "* t\n"
    ".subckt STAGE vin vout vss\n"
    "M1 vout vin vss vss NMOS W=10 L=1\n"
    ".ends STAGE\n"
    "Xs1 na nb 0 STAGE\n"
    "Xs2 nb nc 0 STAGE\n"
    ".end\n"
)


def test_an_instance_maps_definition_ports_to_parent_nets_by_position():
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))
    edge = next(e for e in paths.instances if e.instance_refdes == "Xs1")

    assert edge.definition == "STAGE"
    assert edge.port_nets == {"vin": "na", "vout": "nb", "vss": "0"}
    assert edge.mismatch is None


def test_a_net_reports_the_definition_that_drives_it_and_the_one_that_senses_it():
    # STAGE는 M1의 드레인으로 vout을 구동하고 게이트로 vin을 감지한다.
    # nb는 Xs1의 출력이자 Xs2의 입력이므로 두 역할이 다 붙는다 - 색인 키는
    # 인스턴스가 아니라 정의이고, 이 정의는 실제로 nb를 구동도 감지도 한다.
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))

    assert paths.net_blocks["nb"] == {"STAGE": {"drive", "sense"}}
    assert paths.net_blocks["na"] == {"STAGE": {"sense"}}


def test_a_definition_that_both_drives_and_senses_a_net_reports_both():
    # 예전에는 drive가 sense를 이겨 역할 하나만 남았다. 다이오드 연결
    # 소자에는 맞는 요약이지만 피드백 증폭기 - 이 도메인의 지배적 구조 -
    # 에는 틀리다: 자기 출력을 되받는 블록이 "senses -"로 나와 피드백
    # 루프의 존재를 적극적으로 부정한다. 둘 다 참이면 둘 다 낸다.
    deck = (
        "* t\n"
        ".subckt SELF a vss\n"
        "M1 a a vss vss NMOS W=10 L=1\n"
        ".ends SELF\n"
        "Xs na 0 SELF\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert paths.net_blocks["na"] == {"SELF": {"drive", "sense"}}


def test_a_unity_gain_feedback_buffer_does_not_deny_its_own_loop():
    # bandgap에서 실측된 모양: BUF_P의 vinn과 vout이 둘 다 최상위 vbg0에
    # 붙는다(단위 이득 복사). _own_port_roles는 {'vinn':'sense',
    # 'vout':'drive'}로 정확히 내는데, 두 포트가 같은 넷으로 접히면서
    # drive가 sense를 지워 net_blocks가 {"BUF_P": "drive"}가 됐다.
    deck = (
        "* t\n"
        ".subckt BUF vinn vout vss\n"
        "M1 vout vinn vss vss NMOS W=10 L=1\n"
        ".ends BUF\n"
        "Xb nfb nfb 0 BUF\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert paths.net_blocks["nfb"] == {"BUF": {"drive", "sense"}}


def test_a_top_level_source_net_is_reported_as_supply_or_stimulus():
    # 최상위 독립 소스가 무는 넷은 구조상 전원/자극이다. 이름(vdd/vss/0)으로
    # 알아보는 것은 이 모듈이 금지하는 추측이지만, "최상위 V/I가 이 넷에
    # 붙어 있다"는 것은 파서가 아는 사실이다.
    deck = (
        "* t\n"
        ".subckt STAGE vin vout vss\n"
        "M1 vout vin vss vss NMOS W=10 L=1\n"
        ".ends STAGE\n"
        "Xs1 na nb 0 STAGE\n"
        "Vdd vdd 0 DC 1.8\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert paths.supply_nets == {"vdd", "0"}


def test_a_port_count_mismatch_is_reported_as_a_fact_not_silently_dropped():
    # 노드 수가 포트 수와 다른 것은 넷리스트 버그다. 감추면 사용자가
    # 시뮬레이션 실패의 원인을 영원히 못 찾는다.
    deck = (
        "* t\n"
        ".subckt STAGE vin vout vss\n"
        "M1 vout vin vss vss NMOS W=10 L=1\n"
        ".ends STAGE\n"
        "Xs1 na nb STAGE\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))
    edge = next(e for e in paths.instances if e.instance_refdes == "Xs1")

    assert edge.port_nets == {}
    assert "2" in edge.mismatch and "3" in edge.mismatch


def test_a_bulk_terminal_does_not_make_a_block_a_driver_of_ground():
    # bulk를 drive로 묶으면 모든 블록이 0을 구동하게 되어 초점이 무의미해진다.
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))

    assert "STAGE" not in paths.net_blocks.get("0", {})


def test_a_primitive_whose_model_name_collides_with_a_subckt_is_not_treated_as_an_instance():
    # M1의 모델 토큰이 우연히 어떤 subckt 이름과 같아도(RMOD) 그건 이름 충돌일
    # 뿐이다 - MOSFET은 ctype이 X가 아니므로 애초에 서브회로를 부를 수 없다.
    # 포트 수(2)와 노드 수(4)가 다르다는 이유로 가짜 mismatch를 만들면 안 된다.
    deck = (
        "* t\n"
        ".subckt RMOD p n\n"
        "R1 p n 1k\n"
        ".ends RMOD\n"
        "M1 vout vin vss vss RMOD W=10 L=1\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert not any(e.instance_refdes == "M1" for e in paths.instances)


def test_a_primitive_whose_model_name_and_node_count_coincide_with_a_subckt_is_not_fabricated_as_two_drivers():
    # 더 위험한 경우: R2의 값 토큰이 우연히 subckt PMOD와 이름도 포트 수(2)도
    # 같다. ctype 검사 없이는 mismatch조차 없이 조용히 잘못된 InstanceEdge를
    # 만들고, net_blocks에 R2(실제 소자)와 PMOD(가짜 인스턴스) 둘 다를 같은
    # 넷의 드라이버로 기록해 물리적으로 하나뿐인 소자에 두 개의 정체성을
    # 지어낸다.
    deck = (
        "* t\n"
        ".subckt PMOD p n\n"
        "R1 p n 1k\n"
        ".ends PMOD\n"
        "R2 a b PMOD\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert not any(e.instance_refdes == "R2" for e in paths.instances)
    assert paths.net_blocks["a"] == {"R2": {"drive"}}
    assert paths.net_blocks["b"] == {"R2": {"drive"}}


# TOP -> MID -> LEAF. walk()는 깊이마다 좌표계를 부모 넷 이름으로 갈아
# 끼우며 재귀하는데, 이 저장소의 모든 테스트와 모든 벤치마크 덱이 깊이 2
# 이하라 그 재귀가 한 번도 실행된 적이 없었다 - 중첩 블록의 역할이 최상위
# 넷에 닿는 유일한 경로인데도.
#
#   LEAF: lin을 감지, lout을 구동
#   MID:  min을 감지(M2의 게이트), nin을 구동(M2의 드레인, MID 내부 전용)
#         Xl은 LEAF의 lin을 nin에, lout을 mout에 건다
#   TOP:  Xm은 MID의 min을 tin에, mout을 tout에 건다
#
# 따라서 LEAF의 lout 구동은 mout을 거쳐 tout까지 두 번 좌표를 바꿔 올라오고,
# LEAF의 lin 감지는 nin에서 멈춘다(nin은 MID의 포트가 아니다).
THREE_DEEP = (
    "* t\n"
    ".subckt LEAF lin lout lvss\n"
    "M1 lout lin lvss lvss NMOS W=10 L=1\n"
    ".ends LEAF\n"
    ".subckt MID min mout mvss\n"
    "Xl nin mout mvss LEAF\n"
    "M2 nin min mvss mvss NMOS W=10 L=1\n"
    ".ends MID\n"
    "Xm tin tout 0 MID\n"
    "Rn nin 0 1k\n"
    ".end\n"
)


def test_a_leaf_two_levels_down_surfaces_on_the_correct_top_level_net():
    paths = build_signal_paths(derive_structure(THREE_DEEP, "demo"))

    assert paths.net_blocks["tout"] == {"LEAF": {"drive"}}
    assert paths.net_blocks["tin"] == {"MID": {"sense"}}


def test_a_net_that_stops_being_a_port_at_the_middle_level_is_dropped_not_misattributed():
    # nin은 MID 안에서만 쓰이는 넷이라 밖으로 밀어올릴 좌표가 없다. 로컬
    # 이름 그대로 최상위에 기록하면, 우연히 같은 이름의 최상위 넷이 있을 때
    # LEAF가 전혀 무관한 넷을 감지한다고 보고하게 된다 - 이 덱의 최상위
    # nin(Rn이 무는 넷)이 정확히 그 함정이다.
    paths = build_signal_paths(derive_structure(THREE_DEEP, "demo"))

    assert paths.net_blocks["nin"] == {"Rn": {"drive"}}
