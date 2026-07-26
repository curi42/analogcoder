from analogcoder.netlist import (
    apply_changes,
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


def test_macro_and_eom_are_accepted_as_subckt_and_ends():
    deck = "* t\n.macro AMP a b\nM1 a b 0 0 nch W=1\n.eom\n.end\n"

    parsed = parse_netlist(deck)

    assert list(parsed.subckts) == ["AMP"]
    assert parsed.subckts["AMP"].ports == ["a", "b"]
    assert [c.refdes for c in parsed.subckts["AMP"].components] == ["M1"]
    assert parsed.top_components == []


def test_inc_is_accepted_as_an_alias_for_include(tmp_path):
    out = resolve_includes('.inc "models.lib"\n', "/base/dir")

    assert out.strip() == '.inc "/base/dir/models.lib"'
