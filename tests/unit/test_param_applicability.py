from analogcoder.netlist import check_param_applicability

PDK = (
    "* t\n"
    ".option scale=1.0u\n"
    ".subckt A vdd vss\n"
    "X6 d g vss vss sky130_fd_pr__pfet_01v8 L=1 W=20\n"
    ".ends A\n"
    "Xq1 0 0 na 0 sky130_fd_pr__pnp_05v5_W3p40L3p40\n"
    "Xq8 0 0 ne8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8\n"
    "Rf a b 10k\n"
    "Xa vdd 0 A\n"
    ".end\n"
)


def test_a_param_present_on_the_component_line_is_applicable():
    assert check_param_applicability(PDK, [{"refdes": "A.X6", "param": "W"}]) == (True, None)


def test_a_param_that_exists_nowhere_is_rejected_with_the_names_that_do_exist():
    # 재현된 결함: param="width"는 조용히 width=55를 덧붙이고 소자는 그대로다.
    ok, feedback = check_param_applicability(PDK, [{"refdes": "A.X6", "param": "width"}])

    assert ok is False
    assert "width" in feedback and "W" in feedback and "L" in feedback


def test_a_param_a_peer_instance_uses_is_applicable_even_when_absent_here():
    # Xq8의 m=8이 Xq1.m을 정당화한다. 이것을 막으면 bandgap의 이미터 면적비가
    # 프로젝트 전체에서 도달 불가능해진다.
    assert check_param_applicability(PDK, [{"refdes": "Xq1", "param": "m"}]) == (True, None)


def test_value_is_applicable_when_the_positional_token_is_numeric():
    assert check_param_applicability(PDK, [{"refdes": "Rf", "param": "value"}]) == (True, None)


def test_value_is_rejected_when_the_positional_token_is_a_model_name():
    # param="value"로 덮어쓰면 sky130_fd_pr__pfet_01v8이 숫자가 되어 덱이 깨진다.
    ok, feedback = check_param_applicability(PDK, [{"refdes": "A.X6", "param": "value"}])

    assert ok is False
    assert "sky130_fd_pr__pfet_01v8" in feedback


def test_value_is_rejected_for_a_subckt_instance():
    ok, _ = check_param_applicability(PDK, [{"refdes": "Xa", "param": "value"}])

    assert ok is False


def test_every_violation_is_reported_not_just_the_first():
    ok, feedback = check_param_applicability(
        PDK, [{"refdes": "A.X6", "param": "width"}, {"refdes": "Xa", "param": "value"}]
    )

    assert ok is False
    assert "width" in feedback and "Xa" in feedback


def test_a_refdes_that_resolves_to_nothing_is_silently_passed():
    # check_refdes_resolution의 몫이다 - 이 게이트가 같은 결함을 다른 말로
    # 또 보고하면 안 된다. (Important 리뷰 항목: 이 무언 의무를 지키는
    # 회귀를 잡을 테스트가 없었다.)
    assert check_param_applicability(PDK, [{"refdes": "NoSuchThing", "param": "x"}]) == (True, None)


def test_peer_rule_credits_two_generic_resistors_of_different_values():
    # Critical 리뷰 재현: 동료 판정 키가 리터럴 값이면 R1(10k)과 R2(5k)는
    # 서로 다른 그룹으로 갈라져 R2.tc가 부당하게 거부된다 - Xq1.m이
    # 도달 불가능해지는 실패 양상을 제네릭 소자에서 그대로 재현한 것.
    deck = "* t\nR1 a b 10k tc=1\nR2 c d 5k\n.end\n"

    assert check_param_applicability(deck, [{"refdes": "R2", "param": "tc"}]) == (True, None)


def test_peer_rule_does_not_credit_a_resistor_and_capacitor_sharing_a_literal_value():
    # Critical 리뷰 재현: R과 C가 우연히 같은 값 문자열("10k")을 쓰면
    # 리터럴 값 키는 이걸 동료로 착각해 커패시터 전용 param을 저항에
    # 허용해버린다.
    deck = "* t\nRf a b 10k\nC1 x y 10k ic=0.1\n.end\n"

    ok, _ = check_param_applicability(deck, [{"refdes": "Rf", "param": "ic"}])

    assert ok is False
