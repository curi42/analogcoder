from analogcoder.area_limits import (
    allowed_multiplier_for,
    check_area_growth,
    index_baseline_components,
)

NETLIST_WITH_SUBCKT = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    "Cc outA vout 2p\n"
    ".ends AMP\n"
    "Iref nb1 vdd 100u\n"
    "Rz vnull vout 500\n"
    ".end\n"
)


def test_index_baseline_components_finds_top_level_and_subckt_components():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    assert set(baseline.keys()) == {"M6", "Cc", "Iref", "Rz"}
    assert baseline["M6"].params["W"] == "40u"
    assert baseline["Cc"].value == "2p"
    assert baseline["Rz"].value == "500"


def test_allowed_multiplier_for_transistor_tiers():
    assert allowed_multiplier_for("M", 20e-6) == 3.0
    assert allowed_multiplier_for("M", 50e-6) == 2.0
    assert allowed_multiplier_for("M", 100e-6) == 1.5


def test_allowed_multiplier_for_capacitor_tiers():
    assert allowed_multiplier_for("C", 1e-12) == 3.0
    assert allowed_multiplier_for("C", 5e-12) == 2.0
    assert allowed_multiplier_for("C", 15e-12) == 1.5


def test_allowed_multiplier_for_resistor_tiers():
    assert allowed_multiplier_for("R", 500) == 3.0
    assert allowed_multiplier_for("R", 5000) == 2.0
    assert allowed_multiplier_for("R", 50000) == 1.5


def test_allowed_multiplier_for_unconstrained_ctype_returns_none():
    assert allowed_multiplier_for("I", 100e-6) is None


def test_check_area_growth_passes_when_within_tier_limit():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    # M6 W baseline 40u is in the medium tier (2.0x allowed); 40u->70u is 1.75x
    changes = [{"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "70u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_rejects_when_exceeding_tier_limit():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    # 40u->100u is 2.5x, exceeds the 2.0x medium-tier limit for a 40u baseline
    changes = [{"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False
    assert "M6" in feedback
    assert "2.0" in feedback


def test_check_area_growth_always_passes_shrinkage():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "10u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_combines_w_and_l_for_same_refdes():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    # W 40u->60u (1.5x) * L 1u->2u (2x) = 3.0x combined; baseline W=40u -> medium
    # tier only allows 2.0x, so this is rejected even though neither single
    # dimension alone would be.
    changes = [
        {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "60u"},
        {"refdes": "M6", "param": "L", "old_value": "1u", "new_value": "2u"},
    ]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False


def test_check_area_growth_ignores_unconstrained_ctype():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "Iref", "param": "value", "old_value": "100u", "new_value": "10m"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_skips_unknown_refdes():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "NotInBaseline", "param": "value", "old_value": "1k", "new_value": "100k"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_skips_unparseable_baseline_value_on_transistor():
    # A weak model can incorrectly use param="value" on a transistor line,
    # where the "value" position is actually the model name (e.g. "NMOSG"),
    # not a numeric literal. check_area_growth must not crash on this - it
    # should simply treat the change as unconstrained, same as a missing
    # baseline value.
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "M6", "param": "value", "old_value": "NMOSG", "new_value": "50u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_skips_unparseable_baseline_value_on_subckt_instance():
    # A proposal can target a subckt instantiation line (e.g. "Xdut ... OPAMP2STAGE"),
    # whose .value is the subckt name, not a numeric literal.
    netlist = (
        "* test\n"
        ".subckt OPAMP2STAGE vinp vinn vout vdd vss\n"
        "M6 vout outA vss vss NMOSG W=40u L=1u\n"
        ".ends OPAMP2STAGE\n"
        "Xdut vinp vinn vout vdd vss OPAMP2STAGE\n"
        ".end\n"
    )
    baseline = index_baseline_components(netlist)
    changes = [{"refdes": "Xdut", "param": "value", "old_value": "OPAMP2STAGE", "new_value": "OTHERAMP"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None
