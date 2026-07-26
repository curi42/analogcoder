from analogcoder.netlist import (
    apply_changes,
    check_refdes_resolution,
    parse_netlist,
    resolve_includes,
    strip_inline_comment,
)


def test_strip_inline_comment_splits_code_from_comment():
    assert strip_inline_comment("M1 d g 0 0 nch W=1") == ("M1 d g 0 0 nch W=1", "")
    assert strip_inline_comment("M1 d g 0 0 nch $ note") == ("M1 d g 0 0 nch", "$ note")
    assert strip_inline_comment("Rf a b 10k ; note") == ("Rf a b 10k", "; note")


def test_a_dollar_comment_does_not_swallow_the_model_name():
    # 회귀: 예전에는 nodes가 ['d','g','0','0','nch','$','hspice','comment']가
    # 되고 value가 'comment'가 되어 디바이스 종류가 통째로 사라졌다.
    deck = "* t\nM1 d g 0 0 nch W=1 L=1 $ hspice comment\n.end\n"

    component = parse_netlist(deck).top_components[0]

    assert component.nodes == ["d", "g", "0", "0"]
    assert component.value == "nch"
    assert component.params == {"W": "1", "L": "1"}


def test_a_semicolon_comment_is_stripped_the_same_way():
    deck = "* t\nRf a b 10k ; ngspice comment\n.end\n"

    component = parse_netlist(deck).top_components[0]

    assert component.nodes == ["a", "b"]
    assert component.value == "10k"


def test_applying_a_value_change_edits_the_device_not_the_comment():
    # param="value"는 마지막 위치 토큰을 바꾼다. 주석이 남아 있으면 그게
    # 마지막 위치 토큰이 되어버린다.
    deck = "* t\nRf a b 10k $ feedback resistor\n.end\n"

    out = apply_changes(deck, [{"refdes": "Rf", "param": "value", "new_value": "15k"}])

    assert "Rf a b 15k" in out
    assert "$ feedback resistor" in out


def test_applying_a_named_param_change_edits_the_device_not_the_comment():
    # 위 테스트는 param="value"(마지막 위치 토큰) 경로만 확인한다. name=value
    # 토큰을 찾아 교체하는 분기도 재조립 시 주석을 되붙이는지 별도로 확인한다.
    deck = "* t\nM1 d g 0 0 nch W=1 L=1 $ feedback resistor\n.end\n"

    out = apply_changes(deck, [{"refdes": "M1", "param": "W", "new_value": "2"}])

    assert "W=2" in out
    assert "$ feedback resistor" in out


def test_macro_and_eom_are_accepted_as_subckt_and_ends():
    deck = "* t\n.macro AMP a b\nM1 a b 0 0 nch W=1\n.eom\n.end\n"

    parsed = parse_netlist(deck)

    assert list(parsed.subckts) == ["AMP"]
    assert parsed.subckts["AMP"].ports == ["a", "b"]
    assert [c.refdes for c in parsed.subckts["AMP"].components] == ["M1"]
    assert parsed.top_components == []


def test_a_macro_scoped_refdes_resolves_through_check_refdes_resolution():
    # 회귀: parse_netlist는 .macro/.eom을 .subckt/.ends 별칭으로 인식했지만
    # _line_scopes는 여전히 문자 그대로 ".subckt"/".ends"만 찾고 있어서,
    # AMP.M1이 parsed.subckts엔 있는데도 "매칭 없음"으로 거부됐었다.
    deck = "* t\n.macro AMP a b\nM1 a b 0 0 nch W=1\n.eom\n.end\n"

    ok, feedback = check_refdes_resolution(deck, [{"refdes": "AMP.M1", "param": "W"}])

    assert ok is True
    assert feedback is None


def test_inc_is_accepted_as_an_alias_for_include(tmp_path):
    out = resolve_includes('.inc "models.lib"\n', "/base/dir")

    assert out.strip() == '.inc "/base/dir/models.lib"'


def test_split_tokens_keeps_a_quoted_expression_whole():
    from analogcoder.netlist import split_tokens

    assert split_tokens("M1 d g 0 0 nch W=1") == ["M1", "d", "g", "0", "0", "nch", "W=1"]
    assert split_tokens("M1 d g nch W='wn * 2'") == ["M1", "d", "g", "nch", "W='wn * 2'"]
    assert split_tokens("M1 d g nch W={wn * 2}") == ["M1", "d", "g", "nch", "W={wn * 2}"]
    assert split_tokens("Cc a b 'cv * 2'") == ["Cc", "a", "b", "'cv * 2'"]


def test_a_spaced_quoted_expression_does_not_swallow_the_model_name():
    # 회귀: 예전에는 nodes가 ['d','g','0','0','nch','*']가 되고 value가 "2'"가
    # 되어, $ 주석 버그와 정확히 같은 모양으로 모델명이 사라졌다. 공백이 없는
    # W='wn*2'는 원래도 정상이었으므로 경계는 따옴표 안의 공백이다.
    deck = "* t\nM1 d g 0 0 nch W='wn * 2' L=1\n.end\n"

    component = parse_netlist(deck).top_components[0]

    assert component.nodes == ["d", "g", "0", "0"]
    assert component.value == "nch"
    assert component.params == {"W": "'wn * 2'", "L": "1"}


def test_a_spaced_quoted_positional_value_stays_one_token():
    deck = "* t\nCc a b 'cv * 2'\n.end\n"

    component = parse_netlist(deck).top_components[0]

    assert component.nodes == ["a", "b"]
    assert component.value == "'cv * 2'"


def test_applying_a_change_does_not_corrupt_a_spaced_quoted_expression():
    # 회귀: W='wn * 2'를 W=50으로 바꾸면 "W=50 * 2'"가 남았다. 게이트 셋이
    # 전부 통과시키므로 망가진 덱이 그대로 ngspice까지 갔다.
    deck = "* t\nM1 d g 0 0 nch W='wn * 2' L=1\n.end\n"

    out = apply_changes(deck, [{"refdes": "M1", "param": "W", "new_value": "50"}])

    assert "M1 d g 0 0 nch W=50 L=1" in out
    assert "*" not in out.splitlines()[1]


def test_applying_a_positional_change_replaces_the_whole_quoted_value():
    deck = "* t\nCc a b 'cv * 2'\n.end\n"

    out = apply_changes(deck, [{"refdes": "Cc", "param": "value", "new_value": "5p"}])

    assert "Cc a b 5p" in out


def test_a_subckt_line_default_may_be_a_spaced_quoted_expression():
    deck = "* t\n.subckt SUB a b W='wn * 2'\nM1 a b 0 0 nch W=W\n.ends\n.end\n"

    subckt = parse_netlist(deck).subckts["SUB"]

    assert subckt.ports == ["a", "b"]
    assert subckt.defaults == {"W": "'wn * 2'"}
