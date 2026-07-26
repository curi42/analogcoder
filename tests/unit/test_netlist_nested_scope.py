import pytest

from analogcoder.area_limits import index_baseline_components
from analogcoder.netlist import (
    apply_topology_swap,
    check_refdes_resolution,
    parse_netlist,
    split_scoped_refdes,
)

NESTED = """* t
.subckt OUTER a b
.subckt INNER c d
M1 c d 0 0 nch W=1 L=1
.ends
Xi a b INNER
M2 a b 0 0 nch W=2 L=1
.ends
.end
"""


def test_a_nested_definition_does_not_reparent_its_enclosing_components():
    # 회귀: 예전에는 OUTER가 빈 채로 파싱되고 M2/Xi가 최상위로 올라갔다.
    parsed = parse_netlist(NESTED)

    assert sorted(parsed.subckts) == ["OUTER", "OUTER.INNER"]
    assert [c.refdes for c in parsed.subckts["OUTER"].components] == ["Xi", "M2"]
    assert [c.refdes for c in parsed.subckts["OUTER.INNER"].components] == ["M1"]
    assert parsed.top_components == []


def test_scope_is_the_full_path():
    parsed = parse_netlist(NESTED)

    assert parsed.subckts["OUTER.INNER"].components[0].scope == "OUTER.INNER"
    assert parsed.subckts["OUTER"].components[0].scope == "OUTER"


def test_split_scoped_refdes_splits_on_the_last_dot():
    assert split_scoped_refdes("OUTER.INNER.M1") == ("OUTER.INNER", "M1")
    assert split_scoped_refdes("BUF_P.X6") == ("BUF_P", "X6")
    assert split_scoped_refdes("Rf") == (None, "Rf")


def test_a_full_path_resolves_and_a_partial_one_is_rejected():
    ok, _ = check_refdes_resolution(NESTED, [{"refdes": "OUTER.INNER.M1", "param": "W"}])
    assert ok is True

    ok, feedback = check_refdes_resolution(NESTED, [{"refdes": "INNER.M1", "param": "W"}])
    assert ok is False
    assert "INNER" in feedback


def test_an_unqualified_refdes_colliding_across_nesting_levels_is_ambiguous():
    deck = NESTED.replace("M2 a b 0 0 nch W=2 L=1", "M1 a b 0 0 nch W=2 L=1")

    ok, feedback = check_refdes_resolution(deck, [{"refdes": "M1", "param": "W"}])

    assert ok is False
    assert "ambiguous" in feedback
    assert "OUTER.INNER" in feedback


def test_subckt_line_parameters_are_defaults_not_ports():
    deck = "* t\n.subckt SUB a b W=10 L=1\nM1 a b 0 0 nch W=1\n.ends\n.end\n"

    subckt = parse_netlist(deck).subckts["SUB"]

    assert subckt.ports == ["a", "b"]
    assert subckt.defaults == {"W": "10", "L": "1"}


def test_the_area_index_keys_nested_components_by_path():
    indexed = index_baseline_components(NESTED)

    assert "OUTER.INNER.M1" in indexed
    assert "OUTER.M2" in indexed


def test_topology_swap_spans_a_nested_subckt_instead_of_stopping_at_its_ends():
    # 회귀: 첫 .ends를 OUTER의 끝으로 보아 본문을 잘라먹고 중첩 서브회로의
    # 꼬리를 고아로 남겼다.
    out = apply_topology_swap(NESTED, "OUTER", "M9 a b 0 0 nch W=9")

    parsed = parse_netlist(out)
    assert [c.refdes for c in parsed.subckts["OUTER"].components] == ["M9"]
    assert "OUTER.INNER" not in parsed.subckts
    assert out.count(".ends") == 1


def test_topology_swap_still_works_on_a_flat_subckt():
    deck = "* t\n.subckt AMP a b\nM1 a b 0 0 nch W=1\n.ends\nX1 p q AMP\n.end\n"

    out = apply_topology_swap(deck, "AMP", "M9 a b 0 0 nch W=9")

    assert [c.refdes for c in parse_netlist(out).subckts["AMP"].components] == ["M9"]
    assert "X1 p q AMP" in out
