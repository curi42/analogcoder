from analogcoder.netlist import resolve_change_scopes

NETLIST = (
    "* t\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M1 vout vinp vss vss NMOS W=10 L=1\n"
    ".ends AMP\n"
    ".subckt BIAS vdd vss iref\n"
    "Rb vdd iref 1k\n"
    ".ends BIAS\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    "Xbias1 vdd vss iref BIAS\n"
    "Rf vminus vout 10k\n"
    ".end\n"
)


def test_a_qualified_refdes_resolves_to_its_named_scope():
    assert resolve_change_scopes(NETLIST, [{"refdes": "AMP.M1", "param": "W"}]) == {"AMP"}


def test_an_unqualified_refdes_resolves_to_the_subckt_it_actually_lives_in():
    # Not by string-splitting a dotted prefix - there is none here at all -
    # but by actually finding the Rb line and asking which scope owns it.
    # This is the exact gap that let an unqualified refdes resolving into an
    # out-of-focus subckt go unnoticed by the old focus_misses implementation.
    assert resolve_change_scopes(NETLIST, [{"refdes": "Rb", "param": "value"}]) == {"BIAS"}


def test_a_top_level_refdes_resolves_to_no_scope():
    # Top level is always focused by convention, so callers shouldn't have to
    # special-case an empty result here.
    assert resolve_change_scopes(NETLIST, [{"refdes": "Rf", "param": "value"}]) == set()


def test_a_qualified_refdes_naming_a_nonexistent_subckt_resolves_to_nothing():
    # check_refdes_resolution is meant to have already rejected a change like
    # this before resolve_change_scopes is ever called; this just documents
    # that the helper doesn't crash or fabricate a scope for it.
    assert resolve_change_scopes(NETLIST, [{"refdes": "GHOST.M1", "param": "W"}]) == set()


def test_multiple_changes_merge_into_the_union_of_their_scopes():
    changes = [{"refdes": "AMP.M1", "param": "W"}, {"refdes": "Rb", "param": "value"}]
    assert resolve_change_scopes(NETLIST, changes) == {"AMP", "BIAS"}
