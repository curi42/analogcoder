import pytest

from analogcoder.area_limits import (
    _classify_ctype,
    _tier_baseline_value,
    allowed_multiplier_for,
    check_area_growth,
    evaluate_area_growth,
    index_baseline_components,
    tunable_range,
)
from tests.unit.wrapper_decks import (
    CONTESTED_NAME_DECK,
    INCLUDE_ONLY_DECK,
    PARTIAL_REACH_DECK,
    POSITIONAL_VALUE_DECK,
    SIBLING_INSTANCE_DECK,
    SKY130_POLY_RESISTOR_DECK,
    UNRESOLVABLE_M_DECK,
    WRAPPER_DECK,
    ZERO_MULTIPLICITY_DECK,
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
    # Scoped keys for subckt components, plain aliases for uniquely-named ones,
    # and plain keys for top-level components.
    assert set(baseline.keys()) == {"AMP.M6", "AMP.Cc", "M6", "Cc", "Iref", "Rz"}
    # Both plain and scoped keys should resolve to the same component.
    assert baseline["M6"] is baseline["AMP.M6"]
    assert baseline["Cc"] is baseline["AMP.Cc"]
    assert baseline["M6"].params["W"] == "40u"
    assert baseline["Cc"].value == "2p"
    assert baseline["Rz"].value == "500"


NETLIST_WITH_TOP_LEVEL_SUBCKT_COLLISION = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".ends AMP\n"
    "M6 a b c d NMOSG W=10u L=1u\n"
    ".end\n"
)


def test_index_baseline_components_gives_no_plain_key_when_top_level_collides_with_subckt():
    # A refdes present both top-level and inside a subckt must get no plain
    # key from either side, matching apply_changes' ambiguity rule - so the
    # area gate and the editor agree on what an unqualified "M6" means (both
    # "don't know", not "silently pick the top-level one").
    baseline = index_baseline_components(NETLIST_WITH_TOP_LEVEL_SUBCKT_COLLISION)
    assert "AMP.M6" in baseline
    assert "M6" not in baseline


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


SKY130_NETLIST = (
    "* sky130-style test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "X6 vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
    "Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1\n"
    "Xdut vinp vinn vout vdd vss OPAMP2STAGE\n"
    ".ends AMP\n"
    ".end\n"
)


def test_check_area_growth_classifies_sky130_fet_by_subckt_name():
    baseline = index_baseline_components(SKY130_NETLIST)
    # sky130 netlists write W/L as bare microns with no unit suffix (relying
    # on pdk_corner.inc's ".option scale=1.0u"), so both old_value and
    # new_value here are unitless too, matching what a real tuner proposal
    # against this netlist would emit - mixing in a "u" suffix on only one
    # side would silently corrupt the ratio (30e-6 / 8 instead of 30 / 8).
    # X6's baseline W=8 is far above every TRANSISTOR_TIERS boundary (those
    # are expressed in meters, e.g. 30e-6) once misread as unitless, so it
    # always lands in the strictest (1.5x) tier - 8->30 is 3.75x, which
    # exceeds even that. This only rejects if X6 is correctly classified as
    # a transistor (ctype "M") from its sky130_fd_pr__nfet_01v8 subckt name,
    # not its "X" refdes prefix.
    changes = [{"refdes": "X6", "param": "W", "old_value": "8", "new_value": "30"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False
    assert "X6" in feedback


def test_check_area_growth_classifies_sky130_mim_cap_by_subckt_name():
    baseline = index_baseline_components(SKY130_NETLIST)
    # Xcc's "value" positional token is the subckt name
    # ("sky130_fd_pr__cap_mim_m3_1"), not a numeric literal - area growth
    # must be judged on its w= param instead, the same way a transistor's
    # is judged on W=. w: 12.05->50 is a real ~4.1x growth, which must be
    # rejected once Xcc is correctly classified as a capacitor (ctype "C").
    changes = [{"refdes": "Xcc", "param": "w", "old_value": "12.05", "new_value": "50"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False
    assert "Xcc" in feedback


def test_check_area_growth_still_treats_subckt_instantiation_as_unconstrained():
    # Xdut instantiates a whole op-amp subckt (OPAMP2STAGE), not a sky130
    # PDK primitive - its "value" doesn't contain "fet" or "cap", so it must
    # remain unconstrained, exactly like today's behavior for any X-prefixed
    # non-PDK-primitive instance.
    baseline = index_baseline_components(SKY130_NETLIST)
    changes = [{"refdes": "Xdut", "param": "value", "old_value": "OPAMP2STAGE", "new_value": "OTHERAMP"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


TWO_BUFFERS_NETLIST = (
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    "Xonly n2 vss sky130_fd_pr__nfet_01v8 L=1 W=4\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Cload vout 0 2p\n"
)


def test_index_baseline_components_keys_colliding_refdes_by_subckt():
    indexed = index_baseline_components(TWO_BUFFERS_NETLIST)

    assert indexed["BUF_P.Xcc"].params["W"] == "10"
    assert indexed["BUF_N.Xcc"].params["W"] == "20"
    # Ambiguous plain name gets no alias - it must not silently resolve to one of them.
    assert "Xcc" not in indexed


def test_index_baseline_components_aliases_a_unique_refdes_unqualified():
    # Back-compat: existing single-subckt benchmarks propose unqualified
    # refdes, and without this alias check_area_growth would find no
    # baseline and silently wave the change through.
    indexed = index_baseline_components(TWO_BUFFERS_NETLIST)

    assert indexed["Xonly"] is indexed["BUF_P.Xonly"]
    assert indexed["Cload"].value == "2p"


def test_area_gate_uses_the_scoped_baseline_not_a_colliding_one():
    baseline = index_baseline_components(TWO_BUFFERS_NETLIST)

    # Prove that colliding refdes has no plain alias (would be present pre-change)
    assert "Xcc" not in baseline

    # Prove that scoped keys exist (would not exist pre-change)
    assert "BUF_P.Xcc" in baseline
    assert "BUF_N.Xcc" in baseline

    # 20 -> 30 is 1.5x against BUF_N's own baseline, at the tier limit.
    # Against BUF_P's 10 it would look like 3.0x and be rejected.
    approved, feedback = check_area_growth(
        baseline, [{"refdes": "BUF_N.Xcc", "param": "W", "new_value": "30"}]
    )
    assert approved, feedback

    # Verify that using the WRONG scoped key's baseline would fail,
    # because 20 -> 30 is 3.0x against BUF_P's W=10 baseline, exceeding
    # the 1.5x tier limit. Pre-change would have approved=True for the wrong
    # reason (scoped key not found, so check silently skipped).
    approved_wrong, feedback_wrong = check_area_growth(
        baseline, [{"refdes": "BUF_P.Xcc", "param": "W", "new_value": "30"}]
    )
    assert not approved_wrong, "Should reject 3.0x growth against BUF_P's baseline"
    assert "BUF_P.Xcc" in feedback_wrong


# --- scaled sky130 geometry -------------------------------------------------
# ".option scale=1.0u" plus bare geometry ("W=30" meaning 30um) is how every
# sky130 netlist in this repo is written. Reading those tokens as absolute
# values put every PDK device past the 30e-6/80e-6 tier boundaries and into
# the unbounded 1.5x tier, making the whole tier table inert on exactly the
# benchmarks that use a real PDK.

SCALED_NETLIST = (
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "X7 vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30\n"
    "Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1\n"
    "XRz vout nz 0 sky130_fd_pr__res_high_po w=1 l=15\n"
    "Xcl vss vout vss vss sky130_fd_pr__nfet_01v8 L=20 W=20\n"
    ".ends AMP\n"
)


def test_scale_option_is_applied_to_sky130_geometry():
    components = index_baseline_components(SCALED_NETLIST)

    assert _tier_baseline_value(components["AMP.X7"]) == pytest.approx(30e-6)


def test_sky130_transistor_gets_its_real_tier_not_the_fallback():
    component = index_baseline_components(SCALED_NETLIST)["AMP.X7"]

    allowed = allowed_multiplier_for(
        _classify_ctype(component), _tier_baseline_value(component), is_sky130=True
    )

    assert allowed == 2.0


def test_sky130_mim_cap_is_tiered_by_geometry_not_by_farads():
    components = index_baseline_components(SCALED_NETLIST)

    assert _tier_baseline_value(components["AMP.Xcc"]) == pytest.approx(12.05e-6)


def test_x_prefixed_resistor_is_tiered_by_length_instead_of_falling_through():
    # A sky130 resistor's positional "value" is its subckt NAME, so the old
    # code raised ValueError here and check_area_growth swallowed it, leaving
    # the resistor silently unconstrained.
    components = index_baseline_components(SCALED_NETLIST)

    assert _tier_baseline_value(components["AMP.XRz"]) == pytest.approx(15e-6)


def test_area_gate_rejects_an_oversized_sky130_resistor_growth():
    components = index_baseline_components(SCALED_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "AMP.XRz", "param": "l", "new_value": "90"}]
    )

    assert ok is False
    assert "XRz" in feedback


def test_area_gate_allows_a_within_tier_sky130_resistor_growth():
    components = index_baseline_components(SCALED_NETLIST)

    ok, _ = check_area_growth(
        components, [{"refdes": "AMP.XRz", "param": "l", "new_value": "40"}]
    )

    assert ok is True


def test_area_gate_allows_the_seeded_buf_p_load_cap_growth():
    # benchmarks/bandgap's spec_seed_buf0_droop.yaml is only solvable if
    # BUF_P.Xcl can grow from W=20 to W=50 (2.5x), which needs the 3.0x tier.
    # Read as 20 metres it falls into the unbounded 1.5x tier and is rejected.
    components = index_baseline_components(SCALED_NETLIST)

    ok, _ = check_area_growth(
        components, [{"refdes": "AMP.Xcl", "param": "W", "new_value": "50"}]
    )

    assert ok is True


def test_real_sky130_benchmark_netlist_declares_its_geometry_scale():
    # The scale lives in pdk_corner.inc, which parse_netlist never sees - it
    # only gets the netlist text. Without the netlist declaring it too, every
    # sky130 device reads as tens of metres and lands in the unbounded tier,
    # which is the state this whole task exists to fix. Guarding the real
    # benchmark file, not a fixture, because a fixture cannot catch someone
    # dropping the line from the netlist.
    import os

    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp", "netlist.cir"
    )
    with open(path) as f:
        text = f.read()

    component = index_baseline_components(text)["OPAMP2STAGE.X7"]

    assert _tier_baseline_value(component) == pytest.approx(30e-6)
    assert allowed_multiplier_for("M", _tier_baseline_value(component), is_sky130=True) == 2.0


PNP_NETLIST = (
    ".option scale=1.0u\n"
    ".subckt CORE vbgout vdd vss\n"
    "Xq1 0 0 na 0 sky130_fd_pr__pnp_05v5_W3p40L3p40\n"
    "Xq8 0 0 ne8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8\n"
    ".ends CORE\n"
)


def test_pnp_is_classified_rather_than_falling_through_to_unconstrained():
    components = index_baseline_components(PNP_NETLIST)

    assert _classify_ctype(components["CORE.Xq8"]) == "Q"


def test_pnp_emitter_multiplier_growth_is_bounded():
    # m is an emitter-area count, not a length: m=8 -> m=24 triples the PNP's
    # area. Left unclassified it was completely unconstrained.
    components = index_baseline_components(PNP_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "CORE.Xq8", "param": "m", "new_value": "24"}]
    )

    assert ok is False
    assert "Xq8" in feedback


def test_pnp_emitter_multiplier_small_growth_is_allowed():
    components = index_baseline_components(PNP_NETLIST)

    ok, _ = check_area_growth(
        components, [{"refdes": "CORE.Xq8", "param": "m", "new_value": "12"}]
    )

    assert ok is True


def test_pnp_multiplier_is_a_count_and_is_not_scaled_by_the_deck_scale():
    # geometry_scale must not touch m, or an 8x device would tier as 8e-6.
    components = index_baseline_components(PNP_NETLIST)

    assert _tier_baseline_value(components["CORE.Xq8"]) == pytest.approx(8.0)


def test_a_parameterised_width_is_tiered_like_a_literal_one():
    # 회귀: W=30 -> 300은 차단되는데 W='wn*2' -> 'wn*20'은 같은 10x인데도
    # 승인됐다. 리터럴 덱에서는 드문 예외지만 파라미터화가 기본인 HSPICE
    # 덱에서는 모든 소자에 발동해 게이트가 사실상 사라진다.
    deck = (
        "* t\n.option scale=1.0u\n.param wn=20\n"
        "M1 d g 0 0 nch W='wn*2' L=1\n.end\n"
    )
    indexed = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "'wn*2'", "new_value": "400"}]
    )

    assert ok is False
    assert "10.00x" in feedback


def test_an_unresolvable_value_still_falls_back_to_not_blocking():
    # 의도된 폴백이다. 값을 확정할 수 없으면 면적 영향을 판단할 수 없고,
    # 판단할 수 없는 것을 막지는 않는다.
    deck = (
        "* t\n.option scale=1.0u\n"
        "M1 d g 0 0 nch W='sqrt(nope)' L=1\n.end\n"
    )
    indexed = index_baseline_components(deck)

    ok, _ = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "x", "new_value": "300"}]
    )

    assert ok is True


def test_the_resolved_tier_baseline_uses_the_parameterised_geometry():
    # 티어 선택도 해소된 값을 봐야 한다. M1은 일반(비-sky130) 소자이므로
    # TRANSISTOR_TIERS를 타고, wn*2 = 40um는 30um 초과 80um 이하 구간이라
    # 2.0x 티어다. 해소가 안 되면 티어 베이스라인 자체가 None이 되어 게이트가
    # 아예 판정하지 않는다.
    deck = (
        "* t\n.option scale=1.0u\n.param wn=20\n"
        "M1 d g 0 0 nch W='wn*2' L=1\n.end\n"
    )
    indexed = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "'wn*2'", "new_value": "100"}]
    )

    assert ok is False
    assert "2.0x" in feedback


# --- 래퍼 셀: 인스턴스 줄에서 크기가 정해지는 덱 -----------------------------
# 래퍼 셀 덱의 모양만 옮긴 합성 픽스처다 (실물은 사용자 IP). 제네릭
# 2-트랜지스터 셀이 기하를 파라미터로 선언하고 실제 숫자는 인스턴스 줄에서
# 온다. 게이트는 파라미터 *이름*(wn/ma1 - 설계자의 명명 규칙)을 해석하지
# 않고, 그 값이 도달하는 본문 토큰(w/l/m/nf - SPICE 표준 문법)을 추적한다.
#
# xin1: ma1 총 폭 = wn(2u) x m(4)  = 8u  -> 3.0x 티어
# xin2: ma1 총 폭 = wn(20u) x m(2) = 40u -> 2.0x 티어
#
# 덱 문자열 자체는 tests/unit/wrapper_decks.py에 있다 - test_params.py가 같은
# 덱으로 추적 결과를 확인하므로, 복제해 두면 한쪽만 고쳐져 조용히 갈라진다.
WRAPPER_NETLIST = WRAPPER_DECK


def test_wrapper_instance_width_growth_is_bounded():
    # 이 파일이 존재하는 이유: 예전에는 xin1의 value가 "WRAPCELL_A"이라
    # ctype이 X로 남고 X 티어가 없어 어떤 성장도 허용됐다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": "8e-6"}]
    )

    assert ok is False
    assert "xin1" in feedback
    assert "ma1" in feedback
    assert "4.00x" in feedback


def test_wrapper_instance_width_growth_within_tier_is_allowed():
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": "5e-6"}]
    )

    assert ok is True, feedback


def test_tunable_range_agrees_with_evaluate_area_growth_on_a_traced_wrapper_param():
    """xin1.wn reaches ma1.w (and ma1.m=4) by tracing, not by directly
    addressing a device - xin1's own positional value is the subckt name
    WRAPCELL_A, which _direct_target cannot tier at all. tunable_range must
    follow evaluate_area_growth's own branch (`traced = component.
    traced_params.get(param)`, `_traced_targets` when present) rather than
    unconditionally falling back to `_direct_target`, or it reports "no
    tier" for a knob the judging path actually bounds at 3.0x (see
    test_wrapper_instance_width_growth_is_bounded above, which pins that
    same 3.0x figure via check_area_growth on this exact deck/refdes/param)."""
    components = index_baseline_components(WRAPPER_NETLIST)
    component = components["xin1"]

    baseline, allowed = tunable_range(component, "wn")

    assert baseline == pytest.approx(2e-6)
    assert allowed == 3.0

    # Cross-check against the judging path itself, not just a hardcoded
    # number: comfortably inside the tier passes, comfortably outside it is
    # rejected with that same 3.0x figure in the feedback.
    within_ok, _ = check_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": str(2e-6 * (allowed - 0.5))}]
    )
    over_ok, over_feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": str(2e-6 * (allowed + 0.5))}]
    )
    assert within_ok is True
    assert over_ok is False
    assert "3.0x" in over_feedback


def test_wrapper_tier_comes_from_this_instances_own_values():
    # 같은 2.5x 성장인데 xin1(8u 총 폭, 3.0x 티어)은 통과하고 xin2(40u 총 폭,
    # 2.0x 티어)는 막힌다. 정의 단위 환경은 인스턴스가 갈린 wn/ma1을 버리므로
    # 여기서 필요한 숫자를 주지 못한다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xin2", "param": "wn", "new_value": "50e-6"}]
    )

    assert ok is False
    assert "xin2" in feedback


def test_wrapper_count_param_is_capped_flat_at_two():
    # m은 병렬 소자의 개수다 - 길이가 아니므로 크기별 티어가 아니라 평평한 2.0x.
    components = index_baseline_components(WRAPPER_NETLIST)

    rejected, feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "ma1", "new_value": "12"}]
    )
    allowed, _ = check_area_growth(
        components, [{"refdes": "xin1", "param": "ma1", "new_value": "8"}]
    )

    assert rejected is False
    assert "xin1" in feedback
    assert allowed is True


def test_width_and_count_multiply_into_one_total_width():
    # 6x 구멍: w 3x(단독 허용)와 m 2x(단독 허용)가 같은 제안에 있으면 총 폭은
    # 6x가 된다. 소자별로 묶어 곱을 봐야 잡힌다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components,
        [
            {"refdes": "xin1", "param": "wn", "new_value": "6e-6"},
            {"refdes": "xin1", "param": "ma1", "new_value": "8"},
        ],
    )

    assert ok is False
    assert "6.00x" in feedback
    # ma1만 두 변경이 도달한다. 형제 mb1은 wn의 3x만 받고 3.0x 티어라 통과다.
    assert "ma1" in feedback
    assert "mb1" not in feedback


def test_each_of_the_two_changes_alone_would_have_been_allowed():
    # 위 테스트가 진짜로 곱 때문에 막힌 것인지 못박는다.
    components = index_baseline_components(WRAPPER_NETLIST)

    w_only, _ = check_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": "6e-6"}]
    )
    m_only, _ = check_area_growth(
        components, [{"refdes": "xin1", "param": "ma1", "new_value": "8"}]
    )

    assert w_only is True
    assert m_only is True


def test_finger_count_is_area_neutral_and_excluded_from_the_product():
    # nf는 손가락 개수다: w를 나눌 뿐 총 폭도 면적도 바꾸지 않는다. 곱에
    # 들어가면 안 된다 ("판단 불가"가 아니라 "판단할 것이 없다").
    components = index_baseline_components(WRAPPER_NETLIST)

    alone, feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "nf_n", "new_value": "4"}]
    )
    with_width, feedback2 = check_area_growth(
        components,
        [
            {"refdes": "xin1", "param": "wn", "new_value": "6e-6"},
            {"refdes": "xin1", "param": "nf_n", "new_value": "2"},
        ],
    )

    assert alone is True, feedback
    # nf가 곱에 들어갔다면 6x가 되어 막혔을 것이다.
    assert with_width is True, feedback2


def test_a_rejection_says_the_finger_count_was_not_counted():
    # 면적 중립이라 빼놓았다는 사실이 피드백에 남아야 한다 - 그러지 않으면
    # 튜너도, 나중에 읽는 사람도 nf가 그냥 잊힌 것으로 오해한다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components,
        [
            {"refdes": "xin1", "param": "wn", "new_value": "8e-6"},
            {"refdes": "xin1", "param": "nf_n", "new_value": "2"},
        ],
    )

    assert ok is False
    assert "area-neutral" in feedback


def test_a_non_integral_count_is_rejected():
    # m/nf는 개수다. 스키마는 숫자 문자열만 요구하고 게이트는 비율만 보므로
    # m=6.5가 통과할 수 있었다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok_m, feedback_m = check_area_growth(
        components, [{"refdes": "xin1", "param": "ma1", "new_value": "6.5"}]
    )
    ok_nf, feedback_nf = check_area_growth(
        components, [{"refdes": "xin1", "param": "nf_n", "new_value": "1.5"}]
    )

    assert ok_m is False
    assert "whole number" in feedback_m
    assert ok_nf is False
    assert "whole number" in feedback_nf


def test_a_shrinking_non_integral_count_is_still_rejected():
    # 줄어드는 변경은 면적 검사를 건너뛰지만, 정수 조건은 면적과 무관하다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "ma1", "new_value": "2.5"}]
    )

    assert ok is False
    assert "whole number" in feedback


def test_a_traced_non_size_param_stays_unconstrained():
    # geomod는 크기가 아니다. 티어도 없고 곱에도 들어가지 않는다.
    components = index_baseline_components(WRAPPER_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xin1", "param": "geomod", "new_value": "100"}]
    )

    assert ok is True, feedback


def test_an_untraceable_instance_param_falls_back_to_not_blocking():
    # "무엇을 키우는지 알아내지 못했다"는 "아무것도 키우지 않는다"와 다르다.
    # 전자는 기존 철학대로 막지 않는다.
    deck = (
        "* untraceable\n"
        ".subckt CELL a b\n"
        "R1 a b 1k\n"
        ".ends CELL\n"
        "xc1 p q CELL rval=1k\n"
        ".end\n"
    )
    components = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xc1", "param": "rval", "new_value": "100k"}]
    )

    assert ok is True, feedback


def test_a_wrapper_around_a_pdk_primitive_is_tiered_on_the_scaled_geometry():
    # .option scale은 계속 존중된다 - sky130 덱이 게이트를 통과하는 이유다.
    deck = (
        "* pdk-backed wrapper\n.option scale=1.0u\n"
        ".subckt CELL d g s b\n"
        "Xm1 d g s b sky130_fd_pr__nfet_01v8 W=wn L=ln m=mm\n"
        ".ends CELL\n"
        "xc1 vd vg vs vb CELL wn=10 ln=1 mm=3\n"
        ".end\n"
    )
    components = index_baseline_components(deck)

    # 총 폭 = 10 x 3 x 1u = 30u -> sky130 기하 티어 2.0x. 10 -> 30은 3.0x.
    ok, feedback = check_area_growth(
        components, [{"refdes": "xc1", "param": "wn", "new_value": "30"}]
    )

    assert ok is False
    assert "xc1" in feedback


# --- C1: 하나의 파라미터가 형제 인스턴스 둘에 도달하는 경우 -----------------


def test_one_param_reaching_two_sibling_instances_does_not_square_its_ratio():
    # 회귀 재현: 그룹 키가 *정의* 컴포넌트(scope+refdes)만으로 만들어져 있어
    # xl1과 xl2를 통한 두 도달점이 같은 소자로 묶였고, 한 변경의 2.5x가
    # 6.25x로 곱해졌다. 티어는 소자 하나의 성장 **비율** 한도이므로, 같은
    # 비율이 두 소자에 각각 일어난 것은 여전히 소자당 2.5x다.
    components = index_baseline_components(SIBLING_INSTANCE_DECK)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xtop", "param": "wtop", "new_value": "5e-6"}]
    )

    assert ok is True, feedback


def test_two_sibling_instances_are_each_bounded_on_their_own():
    # 위 수정이 "형제가 있으면 그냥 통과"로 무너지지 않았는지 못박는다.
    # 소자당 4x는 3.0x 티어를 넘으므로 두 소자 모두 위반으로 보고돼야 한다.
    components = index_baseline_components(SIBLING_INSTANCE_DECK)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xtop", "param": "wtop", "new_value": "8e-6"}]
    )

    assert ok is False
    assert "4.00x" in feedback
    assert "6.25x" not in feedback
    assert "xl1" in feedback
    assert "xl2" in feedback


# --- I1: 정의가 .include로만 들어오는 덱 -------------------------------------


def test_an_include_only_wrapper_is_reported_as_blind():
    # parse_netlist는 include를 따라가지 않으므로 이 덱에서는 추적이 원리적으로
    # 불가능하다. 게이트는 여전히 막지 않지만(기존 철학), "판단할 것이 없다"나
    # "값을 못 읽었다"와 구별되는 별개의 사실로 기록해야 한다 - 게이트가 조용히
    # 무력해진 전례가 이 저장소에 이미 두 번 있다.
    components = index_baseline_components(INCLUDE_ONLY_DECK)

    result = evaluate_area_growth(
        components, [{"refdes": "xwrap1", "param": "wn", "new_value": "2e-3"}]
    )

    assert result.approved is True
    assert result.states == {"xwrap1.wn": "blind"}


def test_a_pdk_primitive_is_not_blind_even_though_its_model_is_in_an_include():
    # sky130 프리미티브도 덱 안에 정의가 없지만 모델명으로 분류돼 기하 티어를
    # 받는다 - 이것은 blind가 아니라 bounded다.
    deck = (
        "* pdk primitive\n.option scale=1.0u\n"
        "Xm1 d g s b sky130_fd_pr__nfet_01v8 W=10 L=1\n"
        ".end\n"
    )
    components = index_baseline_components(deck)

    result = evaluate_area_growth(
        components, [{"refdes": "Xm1", "param": "W", "new_value": "20"}]
    )

    assert result.states == {"Xm1.W": "bounded"}


def test_the_three_non_bounded_states_are_told_apart():
    components = index_baseline_components(WRAPPER_NETLIST)

    result = evaluate_area_growth(
        components,
        [
            {"refdes": "xin1", "param": "wn", "new_value": "3e-6"},      # 티어가 있다
            {"refdes": "xin1", "param": "nf_n", "new_value": "2"},       # 볼 것이 없다
            {"refdes": "xin1", "param": "geomod", "new_value": "2"},     # 판단 불가
        ],
    )

    assert result.states == {
        "xin1.wn": "bounded",
        "xin1.nf_n": "neutral",
        "xin1.geomod": "unjudged",
    }


# --- I2: 위치 인자 값이 크기 노브인 경우 -------------------------------------


def test_a_wrapped_resistor_is_bounded_like_the_bare_one():
    # 회귀 재현: _trace가 device.params만 보았기 때문에 위치 인자 값으로
    # 크기가 정해지는 R/C는 래퍼 안에 있으면 영원히 무제약이었다. 똑같은
    # 1000x 성장이 감싸는지 여부로 정반대 판정을 받았다.
    components = index_baseline_components(POSITIONAL_VALUE_DECK)

    wrapped, wrapped_feedback = check_area_growth(
        components, [{"refdes": "xr1", "param": "rv", "new_value": "1meg"}]
    )
    bare, bare_feedback = check_area_growth(
        components, [{"refdes": "R2", "param": "value", "new_value": "1meg"}]
    )

    assert bare is False
    assert wrapped is False
    assert "1000.00x" in wrapped_feedback
    assert "2.0x" in wrapped_feedback
    assert "R1" in wrapped_feedback
    assert "1000.00x" in bare_feedback


def test_a_wrapped_resistor_growing_within_its_tier_is_allowed():
    components = index_baseline_components(POSITIONAL_VALUE_DECK)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xr1", "param": "rv", "new_value": "1.5k"}]
    )

    assert ok is True, feedback


# --- I3: 본문 .param과 .subckt 줄 기본값이 같은 이름을 두고 충돌 -------------


def test_a_contested_name_is_not_resolved_for_tiering_either():
    # 회귀 재현: _instance_env가 build_param_envs와 달리 섀도잉을 적용하지
    # 않아 .subckt 줄 기본값(10u)을 답으로 골랐다. 그러면 3.0x 티어가 잡혀
    # 2.8x 성장이 통과했지만, 본문 .param(60u)을 읽으면 1.5x 티어라 막힌다.
    # 한 덱에 두 개의 답이 있고 게이트가 추측한 쪽으로 움직이는 상태였다.
    components = index_baseline_components(CONTESTED_NAME_DECK)

    result = evaluate_area_growth(
        components, [{"refdes": "xc1", "param": "ln", "new_value": "2.8e-6"}]
    )

    assert result.states == {"xc1.ln": "unjudged"}


# --- M3: m은 MOS만이 아니라 어떤 소자든 면적을 곱한다 -------------------------


def test_a_mim_cap_is_tiered_on_its_multiplicity_too():
    # 회귀 재현: _tier_baseline_value가 ctype "M"에만 m을 곱해, m=4인 MiM 캡이
    # 단위 소자 크기(10u)로 티어링돼 가장 느슨한 3.0x 티어를 받았다. m은
    # 병렬 소자의 개수라 면적을 똑같이 곱한다.
    deck = (
        "* mim cap with multiplicity\n.option scale=1.0u\n"
        "Xc1 a b sky130_fd_pr__cap_mim_m3_1 w=10 l=10 m=4\n"
        ".end\n"
    )
    components = index_baseline_components(deck)

    assert _tier_baseline_value(components["Xc1"]) == pytest.approx(40e-6)

    ok, feedback = check_area_growth(
        components, [{"refdes": "Xc1", "param": "w", "new_value": "25"}]
    )

    # 10u로 티어링하면 3.0x 티어라 2.5x 성장이 통과했다. 40u면 2.0x 티어다.
    assert ok is False
    assert "2.0x" in feedback


# --- 시야 상태는 도달점 전체에 대한 주장이다 ---------------------------------


def test_a_half_judged_change_is_not_reported_as_bounded():
    # N1 회귀: wn이 ma1(총 폭 8u, 티어 있음)과 mb1(총 폭 확정 불가, 실제로
    # 무제약)에 동시에 도달한다. "티어가 하나라도 붙었으면 bounded"라고 하면
    # 로그가 절반의 무제약을 감춘다 - 로그로 무력화를 감사할 수 있게 하는 것이
    # 이 상태의 유일한 존재 이유다.
    components = index_baseline_components(PARTIAL_REACH_DECK)

    result = evaluate_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": "5e-6"}]
    )

    assert result.approved is True  # 판정 자체는 그대로 - 막지 않는다
    assert result.states == {"xin1.wn": "unjudged"}


def test_a_fully_judged_change_is_still_reported_as_bounded():
    # 위 수정이 "도달점이 둘이면 무조건 unjudged"로 무너지지 않았는지 못박는다.
    components = index_baseline_components(WRAPPER_NETLIST)

    result = evaluate_area_growth(
        components, [{"refdes": "xin1", "param": "wn", "new_value": "3e-6"}]
    )

    assert result.states == {"xin1.wn": "bounded"}


# --- 해소되지 않는 m은 1이 아니다 --------------------------------------------


def test_an_unresolvable_m_does_not_silently_become_one():
    # N2 회귀: `1.0 if m is None else m`은 "m 토큰이 없다"와 "m 토큰은 있는데
    # 모른다"를 구별하지 않아, 8배짜리 소자를 1배로 티어링했다 (3.0x 티어 →
    # 2.5x 성장 통과). 이 추측은 **항상** 티어를 느슨한 쪽으로 틀린다.
    components = index_baseline_components(UNRESOLVABLE_M_DECK)

    result = evaluate_area_growth(
        components, [{"refdes": "xc1", "param": "wn", "new_value": "25e-6"}]
    )

    assert result.states == {"xc1.wn": "unjudged"}


def test_the_same_deck_without_the_contest_honours_m_and_blocks():
    # 위 테스트가 진짜로 "m을 못 풀어서"인지 못박는다. 경합만 없애면 m=8이
    # 반영돼 총 폭 80u → 1.5x 티어 → 같은 2.5x 제안이 거부된다.
    deck = UNRESOLVABLE_M_DECK.replace(".param mm=8\n", "")
    components = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        components, [{"refdes": "xc1", "param": "wn", "new_value": "25e-6"}]
    )

    assert ok is False
    assert "1.5x" in feedback


def test_a_directly_addressed_device_with_an_unresolvable_m_is_not_tiered_either():
    # 같은 원칙의 나머지 반쪽: 직접 주소지정 경로(_multiplicity)도 모르는 m을
    # 1로 가정했다. 한쪽만 고치면 같은 구멍이 반쪽 남는다.
    deck = "* direct unresolvable m\nM1 d g s b NMOS W=10u L=1u m=unknown_name\n.end\n"
    components = index_baseline_components(deck)

    assert _tier_baseline_value(components["M1"]) is None

    result = evaluate_area_growth(
        components, [{"refdes": "M1", "param": "W", "new_value": "25u"}]
    )

    assert result.states == {"M1.W": "unjudged"}


def test_a_device_with_no_m_token_at_all_still_tiers_at_multiplicity_one():
    # "토큰이 없다"는 여전히 1이다 - 위 수정이 m 없는 소자를 통째로 무제약으로
    # 만들지 않았는지 못박는다.
    deck = "* no m token\nM1 d g s b NMOS W=10u L=1u\n.end\n"
    components = index_baseline_components(deck)

    assert _tier_baseline_value(components["M1"]) == pytest.approx(10e-6)


# --- 인용은 표기법이지 표현식이 아니다 ---------------------------------------


@pytest.mark.parametrize("written", ["rv", "'rv'", "{rv}"])
def test_a_quoted_bare_identifier_is_still_a_bare_identifier(written):
    # N3 회귀: 맨 식별자 판정이 원본 토큰에 대고 이뤄져 `'rv'`와 `{rv}`가
    # 표현식으로 오인됐다. 그 둘은 이 모듈 자신의 인용 관례일 뿐이므로
    # ("비율이 같다고 가정할 수 없다"는 근거가 닿지 않는다) I2가 없애려던
    # 모양 - 같은 성장이 표기법 하나로 정반대 판정 - 이 그대로 남아 있었다.
    deck = POSITIONAL_VALUE_DECK.replace("R1 a b rv\n", f"R1 a b {written}\n")
    components = index_baseline_components(deck)

    result = evaluate_area_growth(
        components, [{"refdes": "xr1", "param": "rv", "new_value": "1meg"}]
    )

    assert result.approved is False
    assert result.states == {"xr1.rv": "bounded"}


def test_a_genuine_expression_positional_value_is_still_refused():
    # 인용을 벗기는 것이 표현식까지 받아들이는 것으로 번지지 않았는지 못박는다.
    deck = POSITIONAL_VALUE_DECK.replace("R1 a b rv\n", "R1 a b 'rv*2'\n")
    components = index_baseline_components(deck)

    result = evaluate_area_growth(
        components, [{"refdes": "xr1", "param": "rv", "new_value": "1meg"}]
    )

    assert result.approved is True
    assert result.states == {"xr1.rv": "unjudged"}


# --- 티어 기준 치수는 감쌌는지와 무관하다 ------------------------------------


def test_a_poly_resistor_is_tiered_on_its_length_whether_or_not_it_is_wrapped():
    # 티어 기준값을 정하는 규칙이 두 벌 있었다: 직접 경로는 X-접두 저항을
    # **l**로 티어링하고(길이가 저항값도 면적도 정한다), 추적 경로는 도달한
    # 토큰과 무관하게 언제나 **w**(총 폭)를 넘겼다. 그래서 같은 324.74 길이의
    # 같은 저항이 감쌌는지 여부만으로 1.5x와 3.0x라는 정반대 한도를 받았고,
    # 로그에는 둘 다 'bounded'로 남아 그 갈라짐이 보이지 않았다.
    components = index_baseline_components(SKY130_POLY_RESISTOR_DECK)

    direct = tunable_range(components["LADDER.XRpa"], "l")
    wrapped = tunable_range(components["LADDER.xr"], "rl")

    assert direct == wrapped
    assert direct == (pytest.approx(324.74), 1.5)


def test_a_wrapped_poly_resistor_gets_the_same_verdict_as_the_bare_one():
    components = index_baseline_components(SKY130_POLY_RESISTOR_DECK)

    verdicts = {}
    for label, refdes, param in (
        ("direct", "LADDER.XRpa", "l"),
        ("wrapped", "LADDER.xr", "rl"),
    ):
        result = evaluate_area_growth(
            components, [{"refdes": refdes, "param": param, "new_value": "649.48"}]
        )
        verdicts[label] = (result.approved, list(result.states.values()))

    assert verdicts["direct"] == (False, ["bounded"])
    assert verdicts["wrapped"] == (False, ["bounded"])


def test_a_nonpositive_multiplicity_is_clamped_the_same_way_on_both_paths():
    # m<=0은 개수로서 말이 되지 않는다. 직접 경로는 1.0으로 잡았지만 추적
    # 경로는 그대로 곱해 총 폭 0을 만들었고, 0은 가장 느슨한 티어를 받는다.
    components = index_baseline_components(ZERO_MULTIPLICITY_DECK)

    direct = tunable_range(components["HOLD.Xmd"], "W")
    wrapped = tunable_range(components["HOLD.xm2"], "wn")

    assert direct == wrapped
    assert direct == (pytest.approx(30.0), 2.0)


# --- 한 제안 안의 같은 (refdes, param) 중복 ----------------------------------


def test_the_same_refdes_and_param_twice_in_one_proposal_is_not_counted_twice():
    # apply_changes는 같은 줄을 순서대로 다시 써서 **마지막 값만** 덱에
    # 남긴다. 게이트가 두 항목을 곱하면 어느 덱에도 존재하지 않는 성장률을
    # 계산해 거짓 거부하고, states 키가 "<refdes>.<param>" 하나뿐이라 두 번
    # 셌다는 사실은 로그에 남지도 않는다.
    components = index_baseline_components(NETLIST_WITH_SUBCKT)
    change = {"refdes": "AMP.M6", "param": "W", "new_value": "72u"}

    once = evaluate_area_growth(components, [change])
    twice = evaluate_area_growth(components, [dict(change), dict(change)])

    assert once.approved is True
    assert twice.approved is True
    assert twice.feedback is None
    assert twice.states == once.states


def test_a_duplicated_change_is_judged_on_the_value_that_survives_in_the_deck():
    # 값이 다른 중복이면 마지막 것만 덱에 남으므로, 게이트도 마지막 것만 본다.
    components = index_baseline_components(NETLIST_WITH_SUBCKT)

    result = evaluate_area_growth(
        components,
        [
            {"refdes": "AMP.M6", "param": "W", "new_value": "72u"},
            {"refdes": "AMP.M6", "param": "W", "new_value": "76u"},
        ],
    )

    assert result.approved is True
    assert result.feedback is None
