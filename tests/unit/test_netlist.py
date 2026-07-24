from analogcoder.netlist import parse_netlist, apply_changes

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
