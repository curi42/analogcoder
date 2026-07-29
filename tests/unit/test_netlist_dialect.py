from analogcoder.netlist import (
    apply_changes,
    check_refdes_resolution,
    declares_include,
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


# ----------------------------------------------------------------- `.lib`

# 독점 PDK 가 코너를 지정하는 실제 파일 내용(사용자 진술, 2026-07-29).
#
# **축 정체성을 여기서 읽지 않는다.** `PROCESS.LIB` 이 process 축이라는 것을
# 알아내는 길은 셋(파일명 / 순서 / 위의 `*` 주석)뿐이고 전부 추측이다 -
# 넷 이름 `vdd` 를 보고 전원 레일이라고 단정하는 것과 같은 부류이며, 사용자가
# 경로는 바뀔 수 있다고 예고했다. 이 픽스처가 고정하는 사실은 하나뿐이다:
# **`.lib` 호출 줄은 파일을 가리킨다.**
COMPANY_CORNER_INC = (
    "*Process\n"
    ".lib '../../corner_library/PROCESS.LIB' SF6_HTTT\n"
    "\n"
    "*Voltage\n"
    ".lib '../../corner_library/VOLTAGE.LIB' MV\n"
    "\n"
    "*Temperature\n"
    ".lib '../../corner_library/TEMP.LIB' RT\n"
)


def test_a_lib_call_is_absolutised_and_keeps_its_section_name():
    out = resolve_includes(COMPANY_CORNER_INC, "/BASE/tb/corner")

    assert ".lib '/BASE/tb/corner/../../corner_library/PROCESS.LIB' SF6_HTTT" in out
    assert ".lib '/BASE/tb/corner/../../corner_library/VOLTAGE.LIB' MV" in out
    assert ".lib '/BASE/tb/corner/../../corner_library/TEMP.LIB' RT" in out
    # 주석 줄은 손대지 않는다. `*Process` 는 자유 텍스트이고 파서가 읽는
    # 대상이 아니다.
    assert "*Process" in out


def test_a_lib_definition_is_not_a_file_reference():
    """`.lib` 은 두 형태이고 **호출만** 파일을 가리킨다.

    호출: `.lib '<파일>' <섹션>`  - 인자 둘.
    정의: `.lib <섹션>` … `.endl` - 인자 하나. 파일이 아니다.

    구별은 **인자 개수**이고 이것은 추측이 아니다 - 생산 덱의 실제 호출 형태가
    `.lib '<경로>' <섹션>` 두 인자임이 확인됐다. 정의 형태를 호출로 오인하면
    섹션 이름이 경로로 절대화되어 존재하지 않는 파일을 가리킨다."""
    deck = "* t\n.lib SF6_HTTT\n.model nch nmos level=54\n.endl SF6_HTTT\n.end\n"

    assert resolve_includes(deck, "/BASE") == deck


def test_a_lib_call_keeps_the_quoting_style_it_arrived_with():
    """따옴표 종류를 바꾸지 않는다. 생산 덱 형태는 홑따옴표이고, 그것을
    큰따옴표로 고쳐 쓰는 것은 HSPICE 가 둘 다 받는다는 **미검증** 주장에
    기대는 것이다. 원문 유지는 어느 쪽이 참이든 옳다."""
    assert resolve_includes(".lib 'm.lib' TT\n", "/b").strip() == ".lib '/b/m.lib' TT"
    assert resolve_includes('.lib "m.lib" TT\n', "/b").strip() == '.lib "/b/m.lib" TT'
    assert resolve_includes(".lib m.lib TT\n", "/b").strip() == ".lib /b/m.lib TT"


def test_an_absolute_lib_path_is_left_alone():
    line = ".lib '/pdk/corner_library/PROCESS.LIB' SF6_HTTT\n"

    assert resolve_includes(line, "/BASE") == line


def test_a_lib_call_counts_as_a_declared_include():
    """`curation.candidate_from_deck` 이 "scale 이 include 안에 있을 수 있다"를
    경고하는 근거가 이 함수다. `.lib` 도 파일을 끌어오므로 같은 근거가 든다."""
    assert declares_include(".lib '/pdk/x.lib' TT\n") is True
    assert declares_include(".lib TT\n.endl TT\n") is False


def test_a_single_quoted_include_path_does_not_keep_its_quotes():
    """회귀: `.include 'models/tt.inc'` 가
    `.include "/BASE/'models/tt.inc'"` 로 다시 쓰였다 - 따옴표가 경로 안에
    박힌 깨진 경로다. 벤치마크 덱 중 홑따옴표 include 를 쓰는 것은 0건이라
    이 수정은 오늘 어떤 덱의 동작도 바꾸지 않는다."""
    out = resolve_includes(".include 'models/tt.inc'\n", "/BASE")

    assert out.strip() == '.include "/BASE/models/tt.inc"'
