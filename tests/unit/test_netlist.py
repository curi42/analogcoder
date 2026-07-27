import pytest

from analogcoder.netlist import (
    apply_changes,
    apply_topology_swap,
    check_refdes_resolution,
    parse_netlist,
    parse_spice_value,
)

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


NESTED_TOPOLOGY_NETLIST = """\
* t
.subckt OUTER a b
.subckt INNER ai bi
R1 ai bi 1k
.ends INNER
Xi a b INNER
.ends OUTER
.subckt INNER at bt
R9 at bt 9k
.ends
.end
"""
# The nested INNER and the top-level INNER are given distinct port names
# (ai/bi vs at/bt) and distinct .ends spellings (named vs bare) on purpose:
# an assertion like `".subckt INNER a b" in out` or `".ends INNER" in out`
# would be satisfied by the *untouched* copy regardless of what happens to
# the matched one, making the header/footer test vacuous. See the review
# finding that caught this in the original (identical-port) fixture.


def test_a_dotted_path_targets_the_nested_definition_not_the_top_level_one():
    # Mutation this catches: reverting to "first bare-name match wins" would
    # replace the top-level INNER (R9) instead of OUTER.INNER (R1).
    out = apply_topology_swap(NESTED_TOPOLOGY_NETLIST, "OUTER.INNER", "R2 ai bi 2k\n")
    assert "R2 ai bi 2k" in out
    assert "R9 at bt 9k" in out  # top-level same-named definition untouched
    assert "R1 ai bi 1k" not in out


def test_a_bare_name_targets_the_top_level_definition():
    # Mutation this catches: matching on bare name anywhere in the stack (or
    # taking the first occurrence in document order regardless of depth)
    # would hit the nested INNER (R1) instead of the top-level one (R9).
    out = apply_topology_swap(NESTED_TOPOLOGY_NETLIST, "INNER", "R3 at bt 3k\n")
    assert "R1 ai bi 1k" in out  # nested definition left alone
    assert "R9 at bt 9k" not in out
    assert "R3 at bt 3k" in out


def test_a_partial_path_is_rejected_rather_than_guessed_at():
    # Mutation this catches: a suffix/substring match on the path (e.g.
    # matching "INNER" as a trailing component of "OUTER.INNER") would
    # silently pick a definition instead of raising for an unresolvable path.
    with pytest.raises(ValueError):
        apply_topology_swap(NESTED_TOPOLOGY_NETLIST, "INNER.DEEPER", "R4 a b 4k\n")


def test_an_unknown_path_raises():
    with pytest.raises(ValueError):
        apply_topology_swap(NESTED_TOPOLOGY_NETLIST, "NOPE", "R5 a b 5k\n")


def test_the_header_and_footer_lines_are_preserved_verbatim():
    # Mutation this catches: an off-by-one in locating the matched .subckt's
    # own header/footer line indices (as opposed to some ancestor's, or
    # dropping them outright) would drop or duplicate these exact lines.
    # ".subckt INNER ai bi" and the named ".ends INNER" only ever appear on
    # the nested definition's own header/footer in this fixture — the
    # top-level INNER uses different port names and a bare ".ends" — so
    # these assertions can only be satisfied by the matched block itself.
    out = apply_topology_swap(NESTED_TOPOLOGY_NETLIST, "OUTER.INNER", "R2 ai bi 2k\n")
    assert ".subckt INNER ai bi" in out
    assert ".ends INNER" in out


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


def test_apply_changes_ambiguous_error_names_the_actual_candidate_paths():
    # Regression: the message used to say "qualify it as <subckt>.{refdes}",
    # a literal template rather than the real dotted paths - feedback the
    # tuner reads verbatim, so a wrong template teaches it to emit a wrong
    # refdes. Must match what check_refdes_resolution already produces.
    with pytest.raises(ValueError) as exc_info:
        apply_changes(TWO_BUFFERS, [{"refdes": "Xcc", "param": "W", "new_value": "99"}])
    message = str(exc_info.value)
    assert "<subckt>" not in message
    assert "qualify it as one of: BUF_N.Xcc, BUF_P.Xcc" in message


def test_apply_changes_still_accepts_an_unqualified_refdes_that_is_unique():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "Cload", "param": "value", "new_value": "5p"}])

    assert "Cload vout 0 5p" in out


def test_apply_changes_scoped_refdes_that_matches_nothing_is_a_no_op():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "BUF_P.Xnope", "param": "W", "new_value": "99"}])

    assert out == TWO_BUFFERS


# --- check_refdes_resolution: the deterministic pre-apply gate that subsumes
# C1/I1/I2 from the final-branch review. It must classify every proposed
# change's refdes before apply_changes ever sees it, so an unresolvable or
# ambiguous refdes is rejected with retryable feedback instead of either a
# silent no-op or an uncaught ValueError.

MULTI_REFDES_NETLIST = (
    "M1 d g s b nmos W=1u L=0.18u\n"
    "Cc n1 n2 2p\n"
    ".end\n"
)


def test_check_refdes_resolution_approves_a_uniquely_resolving_refdes():
    ok, feedback = check_refdes_resolution(TWO_BUFFERS, [{"refdes": "Cload", "param": "value", "new_value": "5p"}])
    assert ok is True
    assert feedback is None


def test_check_refdes_resolution_approves_a_correctly_scoped_refdes():
    ok, feedback = check_refdes_resolution(
        TWO_BUFFERS, [{"refdes": "BUF_N.Xcc", "param": "W", "new_value": "30"}]
    )
    assert ok is True
    assert feedback is None


def test_check_refdes_resolution_rejects_an_ambiguous_unqualified_refdes():
    ok, feedback = check_refdes_resolution(TWO_BUFFERS, [{"refdes": "Xcc", "param": "W", "new_value": "30"}])
    assert ok is False
    assert "ambiguous" in feedback
    assert "BUF_N.Xcc" in feedback
    assert "BUF_P.Xcc" in feedback


def test_check_refdes_resolution_rejects_a_refdes_that_matches_nothing():
    ok, feedback = check_refdes_resolution(
        TWO_BUFFERS, [{"refdes": "BUF_P.Xnope", "param": "W", "new_value": "99"}]
    )
    assert ok is False
    assert "Xnope" in feedback or "BUF_P.Xnope" in feedback


def test_check_refdes_resolution_rejects_a_dotted_refdes_whose_scope_names_no_subckt():
    # "M1.W" is syntactically valid per TUNER_SCHEMA (the tuner meant to set
    # M1's W param, but wrote it in the refdes field) - split_scoped_refdes
    # parses this as scope="M1", refdes="W". Since no subckt named "M1"
    # exists, this must be rejected here rather than silently no-op'd.
    ok, feedback = check_refdes_resolution(
        MULTI_REFDES_NETLIST, [{"refdes": "M1.W", "param": "value", "new_value": "2u"}]
    )
    assert ok is False
    assert "M1" in feedback


def test_check_refdes_resolution_rejects_cc_dot_kappa_shaped_refdes():
    ok, feedback = check_refdes_resolution(
        MULTI_REFDES_NETLIST, [{"refdes": "Cc.kappa", "param": "value", "new_value": "2p"}]
    )
    assert ok is False
    assert "Cc" in feedback
