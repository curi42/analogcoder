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
    # nb는 Xs1의 출력이자 Xs2의 입력이므로 둘 다 붙는다.
    paths = build_signal_paths(derive_structure(CHAIN, "demo"))

    assert paths.net_blocks["nb"] == {"STAGE": "drive"}
    assert paths.net_blocks["na"] == {"STAGE": "sense"}


def test_drive_wins_when_one_definition_both_drives_and_senses_a_net():
    deck = (
        "* t\n"
        ".subckt SELF a vss\n"
        "M1 a a vss vss NMOS W=10 L=1\n"
        ".ends SELF\n"
        "Xs na 0 SELF\n"
        ".end\n"
    )

    paths = build_signal_paths(derive_structure(deck, "demo"))

    assert paths.net_blocks["na"] == {"SELF": "drive"}


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
    assert paths.net_blocks["a"] == {"R2": "drive"}
    assert paths.net_blocks["b"] == {"R2": "drive"}
