import pytest

from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist, parse_spice_value

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


def test_parse_spice_value_no_suffix():
    assert parse_spice_value("500") == pytest.approx(500.0)


def test_parse_spice_value_pico():
    assert parse_spice_value("2p") == pytest.approx(2e-12)


def test_parse_spice_value_nano():
    assert parse_spice_value("40n") == pytest.approx(40e-9)


def test_parse_spice_value_micro():
    assert parse_spice_value("40u") == pytest.approx(40e-6)


def test_parse_spice_value_milli():
    assert parse_spice_value("5m") == pytest.approx(5e-3)


def test_parse_spice_value_kilo():
    assert parse_spice_value("10k") == pytest.approx(10e3)


def test_parse_spice_value_mega_uses_full_meg_suffix():
    assert parse_spice_value("1.5meg") == pytest.approx(1.5e6)


def test_parse_spice_value_bare_m_is_milli_not_mega():
    assert parse_spice_value("2MEG") == pytest.approx(2e6)
    assert parse_spice_value("2m") == pytest.approx(2e-3)


def test_parse_spice_value_giga_and_tera():
    assert parse_spice_value("3g") == pytest.approx(3e9)
    assert parse_spice_value("1t") == pytest.approx(1e12)


def test_parse_spice_value_femto():
    assert parse_spice_value("100f") == pytest.approx(100e-15)


def test_parse_spice_value_negative_number():
    assert parse_spice_value("-5u") == pytest.approx(-5e-6)


def test_parse_spice_value_scientific_notation():
    assert parse_spice_value("2e-3") == pytest.approx(2e-3)


def test_parse_spice_value_ignores_trailing_unit_letters():
    assert parse_spice_value("5pF") == pytest.approx(5e-12)
    assert parse_spice_value("40uOHM") == pytest.approx(40e-6)


def test_parse_spice_value_case_insensitive():
    assert parse_spice_value("2P") == pytest.approx(2e-12)
    assert parse_spice_value("40U") == pytest.approx(40e-6)


def test_parse_spice_value_raises_on_invalid_input():
    with pytest.raises(ValueError):
        parse_spice_value("not-a-number")


def test_parse_netlist_records_each_component_subckt_scope():
    text = (
        ".subckt BUF_P vinp vinn vout vdd vss\n"
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
        ".ends BUF_P\n"
        ".subckt BUF_N vinp vinn vout vdd vss\n"
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
        ".ends BUF_N\n"
        "Cload vout 0 2p\n"
    )

    parsed = parse_netlist(text)

    assert parsed.subckts["BUF_P"].components[0].scope == "BUF_P"
    assert parsed.subckts["BUF_N"].components[0].scope == "BUF_N"
    assert parsed.top_components[0].scope is None


TWO_BUFFERS = (
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Cload vout 0 2p\n"
)


def test_apply_changes_scoped_refdes_edits_only_the_named_subckt():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "BUF_N.Xcc", "param": "W", "new_value": "99"}])

    xcc_lines = [ln for ln in out.splitlines() if ln.startswith("Xcc")]
    assert xcc_lines == [
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10",
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=99",
    ]


def test_apply_changes_raises_on_an_ambiguous_unqualified_refdes():
    # Silently editing the first match is how a tuner's change to one block
    # lands in a different block with no error at all.
    with pytest.raises(ValueError, match="ambiguous"):
        apply_changes(TWO_BUFFERS, [{"refdes": "Xcc", "param": "W", "new_value": "99"}])


def test_apply_changes_still_accepts_an_unqualified_refdes_that_is_unique():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "Cload", "param": "value", "new_value": "5p"}])

    assert "Cload vout 0 5p" in out


def test_apply_changes_scoped_refdes_that_matches_nothing_is_a_no_op():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "BUF_P.Xnope", "param": "W", "new_value": "99"}])

    assert out == TWO_BUFFERS
