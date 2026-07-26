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
