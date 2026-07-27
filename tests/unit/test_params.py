import os

import pytest

from analogcoder.netlist import parse_netlist
from analogcoder.params import build_param_envs, resolve_value
from tests.unit.wrapper_decks import (
    CONTESTED_NAME_DECK,
    POSITIONAL_VALUE_DECK,
    SIBLING_INSTANCE_DECK,
    WRAPPER_DECK,
)

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "hspice_flavoured.cir"
)


def _fixture_text() -> str:
    with open(FIXTURE) as f:
        return f.read()


def test_resolve_value_reads_plain_and_suffixed_literals():
    assert resolve_value("30", {}) == 30.0
    assert resolve_value("4.7u", {}) == 4.7e-6
    assert resolve_value("10k", {}) == 10000.0


def test_resolve_value_evaluates_bounded_arithmetic_over_the_environment():
    env = {"wn": 4.0}
    assert resolve_value("'wn*2'", env) == 8.0
    assert resolve_value("{wn + 1}", env) == 5.0
    assert resolve_value("'(wn - 1) * 3'", env) == 9.0
    assert resolve_value("'-wn'", env) == -4.0
    assert resolve_value("'wn**2'", env) == 16.0


def test_resolve_value_refuses_to_guess_outside_the_bounded_subset():
    env = {"wn": 4.0}
    assert resolve_value("'sqrt(wn)'", env) is None       # 함수
    assert resolve_value("'undefined_name'", env) is None  # 미정의
    assert resolve_value("'2k*wn'", env) is None           # 표현식 속 접미사
    assert resolve_value("'wn > 2 ? 1 : 0'", env) is None  # 조건식


def test_an_unquoted_spaced_expression_is_not_silently_truncated():
    # 회귀 재현: 이전 _ASSIGN_RE는 \S+ 대안 탓에 공백을 만나면 거기서 멈췄다.
    # ".param wp = wn * 2"에서 "wn"만 캡처되고 "* 2"는 버려져 wp가 wn과 같은
    # 값(4.0)으로 조용히 틀리게 풀렸다 - 절대 나오면 안 되는 값이다.
    deck = "* t\n.param wn = 4\n.param wp = wn * 2\n.end\n"

    envs = build_param_envs(deck)

    assert envs[None]["wn"] == 4.0
    assert envs[None]["wp"] != 4.0
    assert envs[None]["wp"] == 8.0


def test_multiple_assignments_on_one_param_line_still_split_correctly():
    # 위 수정이 공백 포함 표현식을 통째로 삼키려다 한 줄에 여러 개 선언된
    # 평범한 경우("wn=4 wp=8")까지 하나로 뭉개면 안 된다.
    deck = "* t\n.param wn=4 wp=8\n.end\n"

    envs = build_param_envs(deck)

    assert envs[None] == {"wn": 4.0, "wp": 8.0}


def test_a_circular_reference_resolves_to_nothing():
    deck = "* t\n.param a='b*2'\n.param b='a*2'\nM1 x y 0 0 nch W=a\n.end\n"

    envs = build_param_envs(deck)

    assert "a" not in envs[None]
    assert "b" not in envs[None]


def test_global_params_resolve_transitively():
    envs = build_param_envs(_fixture_text())

    assert envs[None]["wn"] == 4.0
    assert envs[None]["wp"] == 8.0


def test_a_subckt_default_is_overridden_by_its_instance():
    # CORE는 W=10을 기본값으로 선언하고 Xc1이 W=20으로 오버라이드한다.
    envs = build_param_envs(_fixture_text())

    assert envs["CORE"]["W"] == 20.0


def test_a_subckt_without_an_override_keeps_its_default():
    deck = "* t\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\nX1 p q SUB\n.end\n"

    envs = build_param_envs(deck)

    assert envs["SUB"]["W"] == 10.0


def test_instances_disagreeing_on_a_parameter_make_it_unresolvable():
    # 값이 진짜로 인스턴스마다 다르고, 이 프로젝트는 정의 단위로 주소지정하므로
    # 단일 정답이 없다.
    deck = (
        "* t\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\n"
        "X1 p q SUB W=20\nX2 r s SUB W=40\n.end\n"
    )

    envs = build_param_envs(deck)

    assert "W" not in envs["SUB"]


def test_a_nested_subckt_sees_the_global_environment():
    envs = build_param_envs(_fixture_text())

    assert envs["WRAP.DEEP"]["wn"] == 4.0


def test_a_subckt_default_shadows_a_global_of_the_same_name():
    # W=5 전역과 W=10 서브회로 기본값이 이름 충돌하면 로컬(기본값)이 이겨야
    # 한다 - 전역 < 서브회로 기본값 < 인스턴스 오버라이드 순서.
    deck = (
        "* t\n.param W=5\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\n"
        "X1 p q SUB\n.end\n"
    )

    envs = build_param_envs(deck)

    assert envs["SUB"]["W"] == 10.0


def test_an_instance_override_shadows_a_global_of_the_same_name():
    # 회귀 재현: W=5 전역, W=10 기본값, 인스턴스가 W=99로 오버라이드.
    # 이전 버그는 seed(전역)에 이미 W가 있다는 이유로 기본값도 오버라이드도
    # 아예 시도하지 않아 envs["SUB"]["W"]가 5.0으로 나왔다.
    deck = (
        "* t\n.param W=5\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\n"
        "X1 p q SUB W=99\n.end\n"
    )

    envs = build_param_envs(deck)

    assert envs["SUB"]["W"] == 99.0


def test_a_non_x_component_whose_value_matches_a_subckt_name_is_not_an_instance():
    # 회귀 재현: M1의 모델명이 우연히 서브회로 이름 CORE와 같다. X로 시작하지
    # 않는 줄은 서브회로 인스턴스화가 아니므로 그 트랜지스터의 W=77이
    # CORE의 기본값(W=10)을 밀어내면 안 된다.
    deck = (
        "* t\n.subckt CORE a b W=10\nM0 a b 0 0 nch W=W\n.ends\n"
        "M1 d g s bb CORE W=77 L=1\n.end\n"
    )

    envs = build_param_envs(deck)

    assert envs["CORE"]["W"] == 10.0


def test_same_named_subckts_in_different_scopes_do_not_leak_instance_overrides():
    # 회귀 재현: A.LOAD(기본 R=10)와 B.LOAD(기본 R=20)는 이름만 같은 별개의
    # 정의다. A.LOAD의 인스턴스만 R=55로 오버라이드하고 B.LOAD의 인스턴스는
    # 오버라이드가 없다. 이전 버그는 인스턴스를 짧은 이름(LOAD)만으로
    # 매칭해 A.LOAD의 오버라이드가 B.LOAD로 새어 들어갔다.
    deck = (
        "* t\n"
        ".subckt A p q\n"
        ".subckt LOAD c d R=10\n"
        "M1 c d 0 0 nch W=1\n"
        ".ends\n"
        "X1 c d LOAD R=55\n"
        ".ends\n"
        ".subckt B p q\n"
        ".subckt LOAD c d R=20\n"
        "M2 c d 0 0 nch W=1\n"
        ".ends\n"
        "X2 c d LOAD\n"
        ".ends\n"
        ".end\n"
    )

    envs = build_param_envs(deck)

    assert envs["A.LOAD"]["R"] == 55.0
    assert envs["B.LOAD"]["R"] == 20.0


def test_the_fixture_parses_without_losing_any_block():
    parsed = parse_netlist(_fixture_text())

    assert sorted(parsed.subckts) == ["CORE", "WRAP", "WRAP.DEEP"]
    assert [c.refdes for c in parsed.subckts["WRAP"].components] == ["Xd", "M5"]
    assert [c.refdes for c in parsed.top_components] == ["Xc1", "Xw1"]


def test_a_disagreeing_override_does_not_fall_back_to_a_global_of_the_same_name():
    # 회귀: 불일치로 판정된 이름을 raw에서 제거하면 _resolve_environment가
    # "로컬 선언 없음"으로 보고 시드(전역)의 값을 그대로 비춰줬다. "모른다"고
    # 판정해 놓고 전역값을 내주는 셈이라, 추측하지 않는다는 계약 위반이다.
    deck = (
        "* t\n.param W=5\n.subckt SUB a b W=10\nM1 a b 0 0 nch W=W\n.ends\n"
        "X1 p q SUB W=20\nX2 r s SUB W=40\n.end\n"
    )

    envs = build_param_envs(deck)

    assert envs[None]["W"] == 5.0
    assert "W" not in envs["SUB"]


# --- 인스턴스 파라미터 추적 ------------------------------------------------
# 픽스처는 tests/unit/wrapper_decks.py에 모여 있다 - test_area_limits.py가
# 같은 덱으로 게이트 판정을 확인하므로 두 파일이 같은 문자열을 봐야 한다.


def _traced(deck, refdes):
    from analogcoder.netlist import parse_netlist
    from analogcoder.params import annotate_traced_params, build_param_envs

    parsed = parse_netlist(deck)
    envs = build_param_envs(deck)
    annotate_traced_params(deck, parsed, envs)
    for component in parsed.top_components:
        if component.refdes == refdes:
            return component
    for subckt in parsed.subckts.values():
        for component in subckt.components:
            if component.refdes == refdes:
                return component
    raise AssertionError(f"{refdes} not found")


def test_build_param_envs_drops_a_name_the_instances_disagree_on():
    # 왜 인스턴스별 해소가 따로 필요한지 못박는 테스트. 정의 단위 환경은
    # 인스턴스마다 값이 갈린 이름을 (정당하게) 버리므로, 래퍼 셀 덱에서는
    # 정확히 필요할 때 None을 준다.
    envs = build_param_envs(WRAPPER_DECK)

    assert "wn" not in envs["WRAPCELL_A"]
    assert "ma1" not in envs["WRAPCELL_A"]


def test_trace_lands_an_instance_param_on_the_body_token():
    xin1 = _traced(WRAPPER_DECK, "xin1")

    assert [(t.device.refdes, t.token) for t in xin1.traced_params["wn"]] == [
        ("ma1", "w"),
        ("mb1", "w"),
    ]
    assert [(t.device.refdes, t.token) for t in xin1.traced_params["ln"]] == [
        ("ma1", "l"),
        ("mb1", "l"),
    ]
    assert [(t.device.refdes, t.token) for t in xin1.traced_params["ma1"]] == [("ma1", "m")]
    assert [(t.device.refdes, t.token) for t in xin1.traced_params["nf_n"]] == [
        ("ma1", "nf"),
        ("mb1", "nf"),
    ]
    assert [(t.device.refdes, t.token) for t in xin1.traced_params["geomod"]] == [
        ("ma1", "geomod"),
        ("mb1", "geomod"),
    ]


def test_total_width_is_resolved_per_instance_not_per_definition():
    # 총 폭은 w × m이고, 두 인스턴스가 그 둘 모두에 서로 다른 값을 준다.
    xin1 = _traced(WRAPPER_DECK, "xin1")
    xin2 = _traced(WRAPPER_DECK, "xin2")

    assert xin1.traced_params["wn"][0].total_width == pytest.approx(8e-6)  # 2u x 4
    assert xin2.traced_params["wn"][0].total_width == pytest.approx(40e-6)  # 20u x 2


def test_trace_follows_a_nested_instance():
    deck = (
        "* nested wrapper\n"
        ".subckt WRAP_PAIR b1 d1 g1 s1\n"
        "ma1 d1 g1 s1 b1 UNITDEV_N w=wn l=ln m=mm\n"
        ".ends WRAP_PAIR\n"
        ".subckt PAIRWRAP b d g s\n"
        "xdp b d g s WRAP_PAIR wn=wtop ln=ltop mm=mtop\n"
        ".ends PAIRWRAP\n"
        "xtop vb vd vg vs PAIRWRAP wtop=2e-6 ltop=3e-6 mtop=4\n"
        ".end\n"
    )

    xtop = _traced(deck, "xtop")

    assert [(t.device.refdes, t.token) for t in xtop.traced_params["wtop"]] == [("ma1", "w")]
    assert xtop.traced_params["wtop"][0].total_width == pytest.approx(8e-6)


def test_trace_stops_at_a_subckt_the_deck_does_not_define():
    # PDK 프리미티브는 덱 안에 정의가 없다 (parse_netlist는 include를 따라가지
    # 않는다). 그 지점이 잎이며, 그 소자 자신의 w/m으로 총 폭을 읽는다.
    deck = (
        "* pdk leaf\n.option scale=1.0u\n"
        ".subckt CELL d g s b\n"
        "Xm1 d g s b sky130_fd_pr__nfet_01v8 W=wn L=ln m=mm\n"
        ".ends CELL\n"
        "xc1 vd vg vs vb CELL wn=10 ln=1 mm=3\n"
        ".end\n"
    )

    xc1 = _traced(deck, "xc1")

    assert [(t.device.refdes, t.token) for t in xc1.traced_params["wn"]] == [("Xm1", "W")]
    assert xc1.traced_params["wn"][0].total_width == pytest.approx(30e-6)


def test_a_param_that_reaches_no_body_token_is_not_traced():
    # 본문의 어떤 name=value 토큰에도 도달하지 않는 파라미터는 "무엇을
    # 키우는지 알아내지 못했다"이며, 추측하지 않는다.
    deck = (
        "* unused\n"
        ".subckt CELL a b\n"
        "R1 a b 1k\n"
        ".ends CELL\n"
        "xc1 p q CELL rval=1k\n"
        ".end\n"
    )

    xc1 = _traced(deck, "xc1")

    assert "rval" not in xc1.traced_params


def test_trace_distinguishes_two_sibling_instances_of_one_definition():
    # C1. 한 래퍼가 같은 단위 셀을 두 번 인스턴스화하면 하나의 파라미터가
    # **물리적으로 다른 두 소자**에 도달한다. 그런데 두 경로가 돌려주는
    # device는 같은 정의 컴포넌트 객체 하나뿐이라, 경로를 구분하는 것은
    # 중간 인스턴스 refdes(chain)뿐이다. 이것이 없으면 하류(면적 게이트)가
    # 두 도달점을 같은 소자로 묶어 한 변경의 비율을 제곱한다.
    xtop = _traced(SIBLING_INSTANCE_DECK, "xtop")

    targets = xtop.traced_params["wtop"]
    assert [(t.chain, t.device.scope, t.device.refdes, t.token) for t in targets] == [
        (("xl1",), "LEAF", "ma1", "w"),
        (("xl2",), "LEAF", "ma1", "w"),
    ]


def test_trace_records_an_empty_chain_for_a_direct_body_device():
    xin1 = _traced(WRAPPER_DECK, "xin1")

    assert [t.chain for t in xin1.traced_params["wn"]] == [(), ()]


def test_trace_follows_a_positional_value_that_is_a_bare_identifier():
    # I2. R/C의 크기 노브는 name=value 토큰이 아니라 위치 인자 값이다.
    # params만 들여다보면 래퍼로 감싼 저항의 크기 노브는 영원히 추적되지
    # 않는다.
    xr1 = _traced(POSITIONAL_VALUE_DECK, "xr1")

    targets = xr1.traced_params["rv"]
    assert [(t.device.refdes, t.token) for t in targets] == [("R1", "value")]
    assert targets[0].positional_value == pytest.approx(1e3)


def test_a_positional_value_that_is_a_literal_is_not_traced():
    # 값이 리터럴이면 어떤 파라미터도 거기 도달하지 않는다 - 추적이 아니라
    # 추측이 될 자리가 없다는 것을 못박는다.
    deck = (
        "* literal positional\n"
        ".subckt RCELL a b\n"
        "R1 a b 1k\n"
        ".ends RCELL\n"
        "xr1 p q RCELL rv=1k\n"
        ".end\n"
    )

    xr1 = _traced(deck, "xr1")

    assert "rv" not in xr1.traced_params


def test_instance_env_drops_a_name_the_body_and_the_subckt_line_contest():
    # I3. build_param_envs는 이 이름을 (정당하게) 버린다. 인스턴스 단위
    # 해소기가 같은 덱에 대해 다른 답을 내면 게이트는 추측된 숫자로
    # 티어를 고르게 된다.
    assert "wn" not in build_param_envs(CONTESTED_NAME_DECK)["CELL"]

    xc1 = _traced(CONTESTED_NAME_DECK, "xc1")

    assert xc1.traced_params["ln"][0].total_width is None
