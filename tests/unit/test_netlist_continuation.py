from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import apply_changes, logical_lines, parse_netlist
from analogcoder.params import build_param_envs

CONT = """* t
.param wn=4
+ wp=8
M1 d g 0 0 nch
+ W=10 L=1
.end
"""


def test_logical_lines_folds_a_continuation_into_its_statement():
    lines = CONT.splitlines()

    folded = logical_lines(lines)

    assert folded == [
        (".param wn=4 wp=8", [1, 2]),
        ("M1 d g 0 0 nch W=10 L=1", [3, 4]),
        (".end", [5]),
    ]


def test_a_continuation_does_not_become_a_bogus_component():
    # 회귀: '+' 줄이 새 문장으로 파싱되어 refdes='+'인 가짜 소자가 생기고,
    # 그게 M1의 W/L을 가져가 M1은 params={}가 됐다 - 즉 에어리어 게이트에
    # 베이스라인이 없었다.
    parsed = parse_netlist(CONT)

    assert [c.refdes for c in parsed.top_components] == ["M1"]
    assert parsed.top_components[0].params == {"W": "10", "L": "1"}
    assert parsed.top_components[0].value == "nch"


def test_a_continued_param_directive_keeps_every_assignment():
    envs = build_param_envs(CONT)

    assert envs[None] == {"wn": 4.0, "wp": 8.0}


def test_a_change_edits_the_continuation_line_that_holds_the_param():
    # 회귀: W=99가 M1의 첫 줄에 덧붙고 진짜 W=10은 연속 줄에 남아, 덱에 W가
    # 두 번 나오는 상태가 됐다. 게이트는 통과시키고 소자는 예측 불가가 된다.
    out = apply_changes(CONT, [{"refdes": "M1", "param": "W", "new_value": "99"}])

    assert "M1 d g 0 0 nch" in out
    assert "+ W=99 L=1" in out
    assert out.count("W=") == 1


def test_a_param_absent_from_the_group_is_appended_to_the_last_physical_line():
    out = apply_changes(CONT, [{"refdes": "M1", "param": "nf", "new_value": "2"}])

    assert "+ W=10 L=1 nf=2" in out


def test_a_positional_change_edits_the_physical_line_holding_the_value():
    out = apply_changes(CONT, [{"refdes": "M1", "param": "value", "new_value": "pch"}])

    assert "M1 d g 0 0 pch" in out
    assert "+ W=10 L=1" in out


def test_the_area_gate_sees_a_continued_devices_baseline():
    deck = "* t\n.option scale=1.0u\nM1 d g 0 0 nch\n+ W=20 L=1\n.end\n"
    indexed = index_baseline_components(deck)

    ok, feedback = check_area_growth(
        indexed, [{"refdes": "M1", "param": "W", "old_value": "20", "new_value": "200"}]
    )

    assert ok is False
    assert "10.00x" in feedback


SUBCKT_PARAM = """* t
.subckt SUB a b
.param wloc=7
M1 a b 0 0 nch W=wloc L=1
.ends
X1 p q SUB
.end
"""


def test_a_param_declared_inside_a_subckt_body_resolves_in_that_scope():
    envs = build_param_envs(SUBCKT_PARAM)

    assert envs["SUB"]["wloc"] == 7.0
    assert index_baseline_components(SUBCKT_PARAM)["SUB.M1"].resolved_params["W"] == 7.0


def test_a_subckt_body_param_does_not_leak_to_the_global_scope():
    envs = build_param_envs(SUBCKT_PARAM)

    assert "wloc" not in envs[None]


def test_a_name_declared_both_as_interface_and_body_param_is_unresolvable():
    # 인터페이스(.subckt 줄 기본값)와 본문 .param이 같은 이름을 선언하면 어느
    # 쪽이 이기는지가 방언마다 다르다. 추측하지 않는다 - 기존 인스턴스 불일치
    # 처리와 같은 규칙이다.
    deck = (
        "* t\n.subckt SUB a b W=10\n.param W=7\nM1 a b 0 0 nch W=W\n.ends\n"
        "X1 p q SUB\n.end\n"
    )

    envs = build_param_envs(deck)

    assert "W" not in envs["SUB"]
