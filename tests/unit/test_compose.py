"""조합 프리미티브. 분석 1이 실제 ngspice로 재현한 조용한 실패들이 여기서
전부 **시끄럽게** 실패해야 한다 - 각 항목에 음성 픽스처가 하나씩 있다."""

import pytest

from analogcoder.compose import ComposeError, Fragment, compose


def _frag(name, text):
    return Fragment(name=name, text=text)


NETLIST = """* core
R1 in mid 1k
R2 mid 0 1k
.end
"""


def test_a_clean_composition_joins_the_fragments_in_order():
    deck = compose(
        [_frag("signals", "Vin in 0 DC 1\n"), _frag("core", NETLIST)],
        title="tb1",
    )
    assert "Vin in 0 DC 1" in deck.text
    assert "R1 in mid 1k" in deck.text
    assert deck.text.index("Vin in 0 DC 1") < deck.text.index("R1 in mid 1k")


# --- §1 제목 줄 흡수 -------------------------------------------------------


def test_the_composer_inserts_its_own_title_so_no_fragment_line_is_eaten():
    """SPICE 덱의 첫 줄은 제목이고 회로에서 사라진다. 조각 1의 첫 줄이
    문장이면 그 소자가 조용히 없어진다(실측: gain_db 19.999 -> 100.0,
    경고 0건)."""
    deck = compose([_frag("signals", "Vin in 0 DC 1\n"), _frag("core", NETLIST)], title="tb1")
    first = deck.text.split("\n")[0]
    assert first.startswith("*")
    assert "tb1" in first
    assert deck.records["title_inserted"] == 1


def test_the_title_is_counted_even_though_it_always_applies():
    """세지 않으면 '삽입했다'와 '삽입 코드가 사라졌다'가 같은 로그가 된다."""
    deck = compose([_frag("a", "R1 a 0 1k\n")], title="t")
    assert "title_inserted" in deck.records


# --- §2 `.end` ------------------------------------------------------------


def test_an_end_that_is_not_the_last_code_line_is_refused():
    """ngspice-46은 합치지만 HSPICE는 미확인이고, 그 미확인이 이 검사의
    이유다. 잘린다면 N개 코너가 넷리스트 없는 덱을 돈다."""
    with pytest.raises(ComposeError) as exc:
        compose([_frag("core", NETLIST), _frag("signals", "Vin in 0 DC 1\n")], title="t")
    assert ".end" in str(exc.value)
    assert "core" in str(exc.value)


def test_two_end_lines_are_refused_and_both_fragments_are_named():
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", "R1 a 0 1k\n.end\n"), _frag("b", "R2 b 0 1k\n.end\n")], title="t")
    message = str(exc.value)
    assert "a" in message and "b" in message


def test_a_composition_with_no_end_gets_one_and_says_so():
    deck = compose([_frag("a", "R1 a 0 1k\n")], title="t")
    assert deck.text.rstrip().split("\n")[-1] == ".end"
    assert deck.records["end_appended"] == 1


def test_an_end_already_last_is_kept_and_not_appended_twice():
    deck = compose([_frag("a", "Rx a 0 1k\n"), _frag("core", NETLIST)], title="t")
    assert deck.text.count(".end") == 1
    assert deck.records["end_appended"] == 0


# --- §3 지시자 충돌 --------------------------------------------------------


@pytest.mark.parametrize(
    "left,right,needle",
    [
        (".model nm nmos level=1 kp=100u", ".model nm nmos level=1 kp=400u", "model"),
        (".option scale=1.0u", ".option scale=1.0", "option"),
        (".param vsup=1.62", ".param vsup=1.8", "param"),
        (".temp 27", ".temp 125", "temp"),
    ],
)
def test_a_directive_declared_by_two_fragments_is_refused(left, right, needle):
    """승자 규칙이 지시자마다 다르고(먼저/나중) 전부 침묵한다 - 그래서
    조각을 놓는 안전한 순서가 존재하지 않고, 충돌 자체를 금지해야 한다."""
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", left + "\n"), _frag("b", right + "\n")], title="t")
    message = str(exc.value)
    assert needle in message
    assert "a" in message and "b" in message


def test_a_subckt_defined_by_two_fragments_is_refused():
    body = ".subckt AMP a b\nR1 a b 1k\n.ends AMP\n"
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", body), _frag("b", body)], title="t")
    assert "AMP" in str(exc.value)


def test_the_same_include_pulled_in_by_two_fragments_is_refused():
    """중복 코너 include. ngspice의 유일한 준-시끄러운 신호는
    `Warning: redefinition ... ignored`인데 그것을 읽는 코드가 저장소에 없다."""
    line = '.include "/abs/pdk/corner.inc"\n'
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", line), _frag("b", line)], title="t")
    assert "include" in str(exc.value)


def test_a_param_declared_inside_a_subckt_does_not_collide_with_another_scope():
    """스코프가 다르면 충돌이 아니다 - 최상위 지시자만 본다."""
    a = ".subckt AMP a b\n.param k=1\nR1 a b 1k\n.ends AMP\n"
    b = ".subckt BUF a b\n.param k=2\nR2 a b 1k\n.ends BUF\n"
    deck = compose([_frag("a", a), _frag("b", b)], title="t")
    assert deck.records["directives_checked"] >= 2


@pytest.mark.parametrize(
    "left,right",
    [
        (".param rf = 10k", ".param rf=20k"),      # 띄어쓴 쪽이 왼쪽
        (".param rf=10k", ".param rf = 20k"),      # 오른쪽
        (".param rf = 10k", ".param rf = 20k"),    # 양쪽
        (".param rf =10k", ".param rf= 20k"),      # 반쪽씩 붙은 두 표기
    ],
)
def test_a_spaced_param_assignment_still_collides(left, right):
    """`.param rf = 10k`는 ngspice가 받는 표기이고, 토큰 단위로 읽으면 `=`를
    담은 토큰이 `"="` 하나뿐이라 이름이 **빈 문자열**이 된다. 그러면 같은 이름을
    선언한 두 조각이 충돌로 잡히지 않는다 - 실측(ngspice-46)으로 그 덱은 조용히
    돌고 **나중** 것이 이긴다. 게이트가 놓치면서 `directives_checked`는 2를
    적으므로, 기록은 "둘을 보고 통과시켰다"로 읽힌다."""
    with pytest.raises(ComposeError, match="param"):
        compose([_frag("a", left + "\n"), _frag("b", right + "\n")], title="t")


def test_two_spaced_params_with_different_names_do_not_collide():
    """같은 결함의 반대편. 이름이 전부 빈 문자열이 되면 서로 다른 이름을 선언한
    두 조각이 **거짓 충돌**한다."""
    deck = compose(
        [_frag("a", ".param rf = 10k\n"), _frag("b", ".param cc = 2p\n")], title="t"
    )
    assert deck.records["directives_checked"] == 2


def test_a_param_line_that_cannot_be_parsed_is_refused():
    """읽을 수 없는 표기는 빈 키를 내는 대신 거부한다. 빈 키는 조용히 놓치는
    쪽으로 닫히는데, 이 모듈의 실패 방향은 반대여야 한다."""
    with pytest.raises(ComposeError, match="param"):
        compose([_frag("a", ".param rf\n")], title="t")


# --- §4 상대 include (cwd 가리기) ------------------------------------------


def test_a_relative_include_is_refused():
    """cwd가 조합 덱의 디렉터리를 가린다. 실측: 덱 옆에 놓은 ss 코너가
    무시되고 tt 값이 그대로 나왔다(경고 0건)."""
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", '.include "pdk_corner.inc"\n')], title="t")
    assert "absolute" in str(exc.value)


def test_the_inc_abbreviation_is_the_same_statement():
    """`.inc`는 `.include`의 약어이고 `netlist._INCLUDE_RE`가 이미 아는 형태다.
    접두사 문자열로 판정하면 한 글자 차이로 절대경로 게이트를 통째로 우회하고,
    `includes_checked`는 0을 적는다 - 검사가 아무것도 안 했다는 뜻인데 통과로
    읽힌다."""
    with pytest.raises(ComposeError, match="absolute"):
        compose([_frag("a", '.inc "pdk_corner.inc"\n')], title="t")


def test_the_inc_abbreviation_collides_with_the_spelled_out_form():
    with pytest.raises(ComposeError, match="include"):
        compose(
            [_frag("a", '.inc "/abs/pdk/corner.inc"\n'), _frag("b", '.include "/abs/pdk/corner.inc"\n')],
            title="t",
        )


def test_a_lib_section_definition_is_not_a_file_reference():
    """`.lib <섹션>` … `.endl`은 **정의** 형태이고 파일을 가리키지 않는다 -
    파일을 가리키는 것은 인자 둘짜리 **호출** 형태뿐이다. `netlist.py`가 그
    구별을 실제 프로덕션 덱으로 확인해 적어 두고, 복제하면 갈라진다고
    경고까지 해 두었다. `compose.py`는 그것을 손으로 복제해서 갈라졌다."""
    deck = compose([_frag("a", ".lib tt\n.param x=1\n.endl\nR1 a b 1k\n")], title="t")
    assert deck.records["includes_checked"] == 0


def test_a_lib_call_still_needs_an_absolute_path():
    """반대 방향 - 인자 둘짜리 호출 형태는 파일을 가리키므로 계속 잡힌다."""
    with pytest.raises(ComposeError, match="absolute"):
        compose([_frag("a", ".lib 'corners.lib' tt\n")], title="t")


def test_an_absolute_include_passes_and_is_counted():
    deck = compose([_frag("a", '.include "/abs/pdk_corner.inc"\n')], title="t")
    assert deck.records["includes_checked"] == 1


def test_a_relative_lib_is_refused_the_same_way():
    with pytest.raises(ComposeError):
        compose([_frag("a", '.lib "models.lib" tt\n')], title="t")


# --- §5 조각 사이 개행 -----------------------------------------------------


def test_a_fragment_ending_without_a_newline_does_not_swallow_the_next_line():
    """실측: 앞 조각이 주석 배너로 끝나고 개행이 없으면 뒤 조각의 첫 줄이
    그 주석에 흡수되어 완전히 사라진다(경고 0건)."""
    deck = compose(
        [_frag("a", "R1 a 0 1k\n* end of signal section"), _frag("b", ".temp 125\n")],
        title="t",
    )
    assert "\n.temp 125" in deck.text
    assert deck.records["boundary_newlines_inserted"] == 1


def test_a_fragment_already_newline_terminated_is_not_counted():
    deck = compose([_frag("a", "R1 a 0 1k\n"), _frag("b", "R2 b 0 1k\n")], title="t")
    assert deck.records["boundary_newlines_inserted"] == 0


# --- §6 subckt 균형과 `.ends` 이름 -----------------------------------------


def test_an_unbalanced_fragment_is_refused():
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", ".subckt AMP a b\nR1 a b 1k\n")], title="t")
    assert "a" in str(exc.value)


def test_a_mismatched_ends_name_is_refused():
    """실측: `.subckt AMP`를 `.ends BUF`로 닫으면 ngspice는 아무 말도 하지
    않는다 - 조각 경계가 subckt 본문을 지나는 경우의 유일한 신호."""
    with pytest.raises(ComposeError) as exc:
        compose([_frag("a", ".subckt AMP a b\nR1 a b 1k\n.ends BUF\n")], title="t")
    assert "AMP" in str(exc.value) and "BUF" in str(exc.value)


def test_a_bare_ends_closes_without_a_name_check():
    deck = compose([_frag("a", ".subckt AMP a b\nR1 a b 1k\n.ends\n")], title="t")
    assert ".ends" in deck.text


# --- §7 넷 이름 충돌은 게이트가 아니라 보고 --------------------------------


def test_a_net_shared_by_two_fragments_is_reported_not_refused():
    """SPICE는 이름으로 넷을 잇는다. 의도를 모르면 옳고 그름을 판정할 수
    없으므로 기록 이상은 추측이 된다."""
    deck = compose(
        [_frag("signals", "Vin in 0 DC 1\n"), _frag("core", "R1 in 0 1k\n")], title="t"
    )
    shared = {entry["net"]: entry["fragments"] for entry in deck.report["shared_nets"]}
    assert "in" in shared
    assert set(shared["in"]) == {"signals", "core"}


def test_shared_nets_is_present_and_empty_when_nothing_is_shared():
    """빈 목록과 '보고가 사라졌다'가 구별되어야 한다."""
    deck = compose([_frag("a", "R1 a 0 1k\n"), _frag("b", "R2 b 0 1k\n")], title="t")
    shared = {entry["net"] for entry in deck.report["shared_nets"]}
    assert shared == {"0"}


def test_each_fragment_records_what_it_contributed_at_the_top_level():
    """분할이 DUT를 먹어치운 실행과 '튜닝할 게 없었다'를 구별하는 유일한 기록."""
    deck = compose(
        [_frag("signals", "Vin in 0 DC 1\n"), _frag("core", "R1 in 0 1k\nR2 in 0 2k\n")],
        title="t",
    )
    contributions = deck.report["top_level_contributions"]
    assert contributions["signals"]["components"] == 1
    assert contributions["core"]["components"] == 2


# --- §8 refdes 중복 --------------------------------------------------------


def test_a_refdes_contributed_by_two_fragments_is_refused_with_attribution():
    """ngspice는 시끄럽지만 합쳐진 덱의 줄 번호만 말한다 - 어느 조각이
    충돌을 냈는지는 조합기만 안다."""
    with pytest.raises(ComposeError) as exc:
        compose([_frag("signals", "Vdd vdd 0 DC 1.8\n"), _frag("core", "Vdd vdd 0 DC 1.6\n")], title="t")
    message = str(exc.value)
    assert "Vdd" in message
    assert "signals" in message and "core" in message


def test_a_refdes_inside_a_subckt_does_not_collide_with_a_top_level_one():
    a = ".subckt AMP a b\nR1 a b 1k\n.ends AMP\n"
    deck = compose([_frag("a", a), _frag("b", "R1 x 0 1k\n")], title="t")
    assert deck.records["top_refdes_checked"] == 1


# --- 검사가 아무것도 안 했을 때의 로그 -------------------------------------


def test_every_check_records_a_count_even_when_nothing_fires():
    """'검사했고 괜찮다'와 '검사가 없다'가 구별되어야 한다 - 이 저장소가
    열 번 값을 치른 질문이다."""
    deck = compose([_frag("a", "R1 a 0 1k\n")], title="t")
    for key in (
        "fragments",
        "title_inserted",
        "end_appended",
        "end_lines_found",
        "boundary_newlines_inserted",
        "directives_checked",
        "top_refdes_checked",
        "includes_checked",
    ):
        assert key in deck.records, key


def test_compose_error_is_a_value_error():
    """run_orchestration / run_optimization이 이미 깨끗한 FAIL로 접는다."""
    assert issubclass(ComposeError, ValueError)


def test_zero_fragments_is_refused():
    with pytest.raises(ComposeError):
        compose([], title="t")
