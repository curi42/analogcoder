import pytest

from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import apply_changes

TWO_BUFFERS = (
    "* two buffers whose compensation caps share a refdes\n"
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Xb0 a b c vdd vss BUF_P\n"
    "Xb1 d e f vdd vss BUF_N\n"
    ".end\n"
)


def test_a_scoped_proposal_is_gated_and_applied_against_the_right_block():
    baseline = index_baseline_components(TWO_BUFFERS)

    # Assert scoped keys exist and colliding plain key does not.
    assert "BUF_P.Xcc" in baseline
    assert "BUF_N.Xcc" in baseline
    assert "Xcc" not in baseline

    # BUF_N.Xcc: W=20 -> W=30 is 1.5x, within tier limit (allowed).
    change = {"refdes": "BUF_N.Xcc", "param": "W", "new_value": "30"}
    approved, feedback = check_area_growth(baseline, [change])
    assert approved, feedback

    out = apply_changes(TWO_BUFFERS, [change])

    assert "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10" in out
    assert "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=30" in out
    assert "W=20" not in out

    # BUF_P.Xcc: W=10 -> W=31 is 3.1x, exceeding the tier limit (rejected).
    # This asserts the area gate uses the RIGHT scope's baseline and refuses
    # oversized changes, not the wrong scope's.
    change_wrong_scope = {"refdes": "BUF_P.Xcc", "param": "W", "new_value": "31"}
    approved_wrong, feedback_wrong = check_area_growth(baseline, [change_wrong_scope])
    assert not approved_wrong, "BUF_P.Xcc: 10->31 is 3.1x and should exceed the tier limit"
    assert "exceeding" in feedback_wrong


def test_an_unqualified_colliding_proposal_is_refused_rather_than_misapplied():
    with pytest.raises(ValueError, match="ambiguous"):
        apply_changes(TWO_BUFFERS, [{"refdes": "Xcc", "param": "W", "new_value": "30"}])
