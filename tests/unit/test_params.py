import os

from analogcoder.netlist import parse_netlist
from analogcoder.params import build_param_envs, resolve_value

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
