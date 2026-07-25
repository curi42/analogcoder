import pytest

from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist

SIMPLE_NETLIST = """\
* simple RC
Vin in 0 AC 1
Rin in vminus 1k
Rf vminus vout 10k
.end
"""

SUBCKT_NETLIST = """\
.subckt amp in out
R1 in mid 1k
R2 mid out 2k
.ends
Xamp1 a b amp
.end
"""


def test_parse_netlist_top_level_components():
    parsed = parse_netlist(SIMPLE_NETLIST)
    refdes = [c.refdes for c in parsed.top_components]
    assert refdes == ["Vin", "Rin", "Rf"]
    rin = next(c for c in parsed.top_components if c.refdes == "Rin")
    assert rin.nodes == ["in", "vminus"]
    assert rin.value == "1k"


def test_parse_netlist_subckt_block():
    parsed = parse_netlist(SUBCKT_NETLIST)
    assert "amp" in parsed.subckts
    subckt = parsed.subckts["amp"]
    assert subckt.ports == ["in", "out"]
    assert [c.refdes for c in subckt.components] == ["R1", "R2"]
    assert len(parsed.top_components) == 1
    assert parsed.top_components[0].refdes == "Xamp1"


def test_apply_changes_replaces_primary_value():
    updated = apply_changes(SIMPLE_NETLIST, [{"refdes": "Rf", "param": "value", "new_value": "20k"}])
    parsed = parse_netlist(updated)
    rf = next(c for c in parsed.top_components if c.refdes == "Rf")
    assert rf.value == "20k"


def test_apply_changes_sets_named_param():
    netlist = "M1 d g s b nmos W=1u L=0.18u\n.end\n"
    updated = apply_changes(netlist, [{"refdes": "M1", "param": "W", "new_value": "2u"}])
    parsed = parse_netlist(updated)
    m1 = parsed.top_components[0]
    assert m1.params["W"] == "2u"
    assert m1.params["L"] == "0.18u"


def test_apply_topology_swap_replaces_interior_preserving_header_and_footer():
    netlist = (
        "* test netlist\n"
        ".subckt AMP vinp vinn vout vdd vss\n"
        "R1 vinp mid 1k\n"
        "R2 mid vout 2k\n"
        ".ends AMP\n"
        "Xamp1 a b c d e AMP\n"
        ".end\n"
    )
    new_body = "R3 vinp mid 5k\nR4 mid vout 6k\n"

    updated = apply_topology_swap(netlist, "AMP", new_body)

    assert ".subckt AMP vinp vinn vout vdd vss" in updated
    assert ".ends AMP" in updated
    assert "R1 vinp mid 1k" not in updated
    assert "R3 vinp mid 5k" in updated
    assert "R4 mid vout 6k" in updated
    assert "Xamp1 a b c d e AMP" in updated  # lines outside the block are untouched


def test_apply_topology_swap_raises_when_subckt_not_found():
    netlist = "* test\nR1 a b 1k\n.end\n"
    with pytest.raises(ValueError):
        apply_topology_swap(netlist, "AMP", "R1 a b 1k\n")


def test_apply_topology_swap_raises_when_subckt_not_closed():
    netlist = "* test\n.subckt AMP a b\nR1 a b 1k\n.end\n"
    with pytest.raises(ValueError):
        apply_topology_swap(netlist, "AMP", "R1 a b 1k\n")
