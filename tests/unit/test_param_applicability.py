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
