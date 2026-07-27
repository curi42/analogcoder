from analogcoder.structure import derive_structure

TWO_BLOCK = (
    "* t\n"
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "X1 nx vinn tail vss sky130_fd_pr__nfet_01v8 W=48 L=1\n"
    "X2 ny vinp tail vss sky130_fd_pr__nfet_01v8 W=48 L=1\n"
    "Cc nx vout 1p\n"
    ".ends AMP\n"
    "Xa1 inp inn out vdd 0 AMP\n"
    "Xa2 inp inn out2 vdd 0 AMP\n"
    "Rf inn out 10k\n"
    ".end\n"
)


def test_blocks_are_keyed_by_definition_path_with_none_for_top_level():
    s = derive_structure(TWO_BLOCK, "demo")

    assert s.circuit_name == "demo"
    assert set(s.blocks) == {None, "AMP"}
    assert s.blocks["AMP"].ports == ["vinp", "vinn", "vout", "vdd", "vss"]
    assert [c.refdes for c in s.blocks[None].components] == ["Xa1", "Xa2", "Rf"]


def test_a_definition_reports_how_many_times_it_is_instantiated():
    # 정의를 튜닝하면 모든 인스턴스가 바뀐다. 그 사실이 보여야 한다.
    s = derive_structure(TWO_BLOCK, "demo")

    assert s.blocks["AMP"].instance_count == 2
    assert s.blocks[None].instance_count == 1


def test_component_refdes_is_scope_qualified():
    s = derive_structure(TWO_BLOCK, "demo")

    assert [c.refdes for c in s.blocks["AMP"].components] == ["AMP.X1", "AMP.X2", "AMP.Cc"]


def test_a_recognised_model_name_yields_terminal_roles():
    s = derive_structure(TWO_BLOCK, "demo")
    x1 = s.blocks["AMP"].components[0]

    assert x1.device_class == "nfet"
    assert [(t.name, t.role) for t in x1.terminals] == [
        ("d", "drive"), ("g", "sense"), ("s", "drive"), ("b", "bulk"),
    ]


def test_an_unrecognised_x_instance_reports_no_terminals_rather_than_guessing():
    # Xa1은 서브회로 인스턴스다. 단자 의미를 모르므로 침묵한다 -
    # 추측한 역할은 초점 선정을 조용히 틀리게 만든다.
    s = derive_structure(TWO_BLOCK, "demo")
    xa1 = s.blocks[None].components[0]

    assert xa1.device_class is None
    assert xa1.terminals == []


def test_an_m_prefix_is_a_mosfet_even_when_the_model_name_says_nothing():
    # ctype 자체가 SPICE의 보장이다. 모델 이름 표에 없다고 침묵하면
    # generic level-1 덱 전체가 단자 역할을 잃는다.
    deck = "* t\nM6 vout outA vss vss NMOSG W=40u L=1u\n.end\n"

    m6 = derive_structure(deck, "demo").blocks[None].components[0]

    assert [(t.name, t.role) for t in m6.terminals] == [
        ("d", "drive"), ("g", "sense"), ("s", "drive"), ("b", "bulk"),
    ]


def test_the_tunable_index_covers_both_named_params_and_positional_values():
    s = derive_structure(TWO_BLOCK, "demo")
    entries = {(e.refdes, e.param) for e in s.tunable}

    assert ("AMP.X1", "W") in entries
    assert ("AMP.X1", "L") in entries
    assert ("AMP.Cc", "value") in entries
    assert ("Rf", "value") in entries
    # 모델명은 튜닝 대상이 아니다 - 숫자로 덮어쓰면 덱이 깨진다.
    assert ("AMP.X1", "value") not in entries


def test_the_structure_exposes_no_field_that_nothing_consumes():
    # net_terminals는 계산되고 스냅샷되고 아무도 안 썼다. 게다가 키 공간이
    # 스코프 한정("AMP.tail")이라 signal_path.net_blocks의 최상위 넷 이름과
    # 달랐다 - 나중에 누가 둘을 같은 것으로 알고 조인하면 조용히 빈 결과를
    # 낸다. 죽은 필드에 서로 다른 키 규약까지 붙으면 그건 함정이다.
    s = derive_structure(TWO_BLOCK, "demo")

    assert not hasattr(s, "net_terminals")


def test_derivation_is_deterministic():
    # analyzer는 같은 넷리스트에 대해 roles를 93/26/1개로 냈다. 이 테스트가
    # 그것과 대비되는 지점이다.
    assert derive_structure(TWO_BLOCK, "demo") == derive_structure(TWO_BLOCK, "demo")
