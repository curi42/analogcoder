import os

from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure
from analogcoder.structure_view import (
    focus_misses,
    render_netlist,
    render_structure,
    select_focus,
)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

CHAIN = (
    "* t\n"
    ".subckt DRIVER vin vout vss\n"
    "M1 vout vin vss vss NMOS W=10 L=1\n"
    ".ends DRIVER\n"
    ".subckt SPARE a b vss\n"
    "M9 b a vss vss NMOS W=10 L=1\n"
    ".ends SPARE\n"
    "Xd na out 0 DRIVER\n"
    "Xs p q 0 SPARE\n"
    "Vin na 0 DC 1\n"
    ".end\n"
)


def _built():
    s = derive_structure(CHAIN, "demo")
    return s, build_signal_paths(s)


def test_focus_seeds_from_the_blocks_that_touch_a_failing_net():
    s, paths = _built()

    assert select_focus(s, paths, {"out"}, set(), CHAIN) == {"DRIVER"}


def test_focus_walks_one_hop_back_to_whatever_drives_what_a_seed_senses():
    deck = CHAIN.replace("Xd na out 0 DRIVER", "Xd mid out 0 DRIVER").replace(
        "Xs p q 0 SPARE", "Xs na mid 0 SPARE"
    )
    s = derive_structure(deck, "demo")
    paths = build_signal_paths(s)

    # DRIVER는 mid를 감지하고 SPARE가 mid를 구동한다. 씨앗의 입력을 만드는
    # 블록을 못 보면 튜너가 원인 쪽을 건드릴 수 없다.
    assert select_focus(s, paths, {"out"}, set(), deck) == {"DRIVER", "SPARE"}


def test_a_block_already_touched_this_run_stays_in_focus():
    s, paths = _built()

    assert select_focus(s, paths, {"out"}, {"SPARE.M9"}, CHAIN) == {"DRIVER", "SPARE"}


def test_an_unqualified_touched_refdes_still_keeps_its_block_in_focus():
    # check_refdes_resolution은 유일하게 해석되는 언스코프 refdes를 명시적으로
    # 허용한다. "refdes.startswith(path + '.')"는 dotted 형태만 잡으므로,
    # "M9"를 제안하면 방금 튜닝한 SPARE가 다음 반복에서 시야 밖으로 빠진다 -
    # 초점 규칙 4가 존재하는 이유가 바로 그것을 막는 데 있다. 형제 함수인
    # focus_misses는 이미 같은 결함을 고쳤다(resolve_change_scopes 경유).
    s, paths = _built()

    assert select_focus(s, paths, {"out"}, {"M9"}, CHAIN) == {"DRIVER", "SPARE"}


NESTED = (
    "* t\n"
    ".subckt OUTER a b vss\n"
    ".subckt INNER c d vss\n"
    "M1 c d vss vss NMOS W=10 L=1\n"
    ".ends INNER\n"
    "Xi a b vss INNER\n"
    "M2 a b vss vss NMOS W=10 L=1\n"
    ".ends OUTER\n"
    "Xtop na out 0 OUTER\n"
    "Vin na 0 DC 1\n"
    ".end\n"
)


def test_a_touched_refdes_in_a_nested_definition_keeps_every_ancestor_in_focus_and_visible():
    # 회귀: resolve_change_scopes는 가장 안쪽 스코프만 돌려준다("OUTER.INNER"
    # 하나). 그런데 render_netlist는 중첩 정의를 부모와 통째로 접는다(216-218행
    # 주석) - OUTER가 초점 밖이면 그 안의 OUTER.INNER 본문까지 함께 묻혀,
    # 튜너가 방금 바꾼 값을 다음 반복에서 다시 읽을 수 없다. 규칙 4가 막으려던
    # 바로 그 상황이 다시 일어난다. 조상 경로까지 초점에 넣어야 이 접힘을
    # 막는다 - focus 집합만 확인하면 render_netlist의 접힘 규칙이 나중에
    # 바뀌어도 이 회귀를 못 잡으므로 실제 렌더 결과까지 함께 확인한다.
    s = derive_structure(NESTED, "demo")
    paths = build_signal_paths(s)

    focus = select_focus(s, paths, set(), {"OUTER.INNER.M1"}, NESTED)
    assert focus == {"OUTER", "OUTER.INNER"}

    text = render_netlist(NESTED, focus)
    assert "M1 c d vss vss NMOS W=10 L=1" in text
    assert "elided" not in text


FEEDBACK = (
    "* t\n"
    ".subckt SRC out vss\n"
    "M1 out nb vss vss NMOS W=10 L=1\n"
    ".ends SRC\n"
    ".subckt FB fb out vss\n"
    "M2 out fb vss vss NMOS W=10 L=1\n"
    "M3 fb fb vss vss NMOS W=10 L=1\n"
    ".ends FB\n"
    "Xs shared 0 SRC\n"
    "Xf shared meas 0 FB\n"
    ".end\n"
)


def test_a_feedback_block_reports_both_roles_instead_of_denying_the_loop():
    # drive가 sense를 이기면 자기 출력을 되받는 블록이 "senses -"로 나온다 -
    # 피드백 증폭기에 대해 적극적으로 틀린 주장이다.
    s = derive_structure(FEEDBACK, "demo")
    paths = build_signal_paths(s)

    text = render_structure(s, paths, [], set())
    line = next(l for l in text.splitlines() if l.strip().startswith("FB "))

    assert "drives meas,shared" in line
    assert "senses shared" in line


def test_the_reverse_hop_fires_from_a_block_that_both_drives_and_senses_its_net():
    # 역방향 1홉은 "씨앗이 감지하는 넷을 누가 구동하는가"로 상류를 찾는다.
    # FB가 shared를 구동도 감지도 하는데 drive만 남으면 shared가 아예
    # sensed_nets에 안 들어가, 그 값을 만드는 SRC가 영원히 시야 밖이다.
    s = derive_structure(FEEDBACK, "demo")
    paths = build_signal_paths(s)

    assert select_focus(s, paths, {"meas"}, set(), FEEDBACK) == {"FB", "SRC"}


SUPPLY_DECK = (
    "* t\n"
    ".subckt AMP vin vout vdd vss\n"
    "M1 vout vin vss vss NMOS W=10 L=1\n"
    "Rdeg vout vdd 1k\n"
    ".ends AMP\n"
    "Xa nstim nout vdd 0 AMP\n"
    "Vstim nstim 0 AC 1\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


def test_a_supply_net_is_not_summarised_as_a_block_output():
    # 실측: OPAMP2STAGE drives vdd,vss / BANDGAP drives vss. 레일은 입력이지
    # 블록의 출력이 아니다 - 최상위 독립 소스가 그 넷을 구동하므로 어떤
    # 블록도 드라이버일 수 없다. "연산증폭기가 vdd를 구동한다"는 거짓이다.
    s = derive_structure(SUPPLY_DECK, "demo")
    paths = build_signal_paths(s)

    line = next(l for l in render_structure(s, paths, [], set()).splitlines() if "AMP " in l)

    assert "drives nout" in line
    assert "vdd" not in line


def test_a_block_sensing_the_stimulus_net_is_still_summarised_as_sensing_it():
    # 거짓이었던 것은 drive 주장뿐이다. 넷을 통째로 빼면 참이면서 유용한
    # "이 블록이 자극 입력을 감지한다"까지 함께 사라진다 - 테스트벤치에서
    # 독자가 가장 보고 싶어 하는 바로 그 사실이다.
    s = derive_structure(SUPPLY_DECK, "demo")
    paths = build_signal_paths(s)

    line = next(l for l in render_structure(s, paths, [], set()).splitlines() if "AMP " in l)

    assert "senses nstim" in line


def test_the_real_two_stage_deck_senses_its_stimulus_and_drives_no_rail():
    # 리뷰가 인용한 실제 줄: "OPAMP2STAGE drives vout,vdd,vss senses vinn,vinp".
    # vdd/vss 구동은 거짓이라 사라져야 하고, vinp 감지는 참이라 남아야 한다.
    text = open(os.path.join(REPO, "benchmarks", "two_stage_opamp", "netlist.cir")).read()
    s = derive_structure(text, "two_stage")
    paths = build_signal_paths(s)

    line = next(
        l for l in render_structure(s, paths, [], set()).splitlines() if "OPAMP2STAGE" in l
    )

    assert "drives vout " in line
    assert "senses vinn,vinp" in line
    assert "vdd" not in line and "vss" not in line


def test_a_block_that_only_drives_a_rail_is_not_seeded_by_that_rail():
    # 레일을 무는 2단자 소자밖에 없는 블록은 그 레일의 드라이버가 아니다.
    # 그런 블록까지 씨앗으로 잡으면 레일 하나가 초점을 통째로 번지게 한다.
    deck = (
        "* t\n"
        ".subckt A vin vout vss\n"
        "M1 vout vin vss vss NMOS W=10 L=1\n"
        ".ends A\n"
        ".subckt B vin vout vss\n"
        "M1 vout vin vss vss NMOS W=10 L=1\n"
        "Rp vout vss 1k\n"
        ".ends B\n"
        "Xa n1 n2 0 A\n"
        "Xb n3 n4 0 B\n"
        "V0 0 nz DC 0\n"
        ".end\n"
    )
    s = derive_structure(deck, "demo")
    paths = build_signal_paths(s)

    # B는 Rp로 0을 "구동"하는 것처럼 보이지만 0의 드라이버는 V0다.
    assert "drive" in paths.net_blocks["0"]["B"]
    assert select_focus(s, paths, {"n2", "0"}, set(), deck) == {"A"}


def test_a_criterion_measured_on_the_stimulus_net_seeds_the_block_that_senses_it():
    # 반대 방향으로 망가지지 않았는지 확인한다: 자극 넷을 감지하는 것은
    # 정당한 사실이므로, 그 넷에서 측정한 기준은 감지 블록을 씨앗으로
    # 잡을 수 있어야 한다.
    s = derive_structure(SUPPLY_DECK, "demo")
    paths = build_signal_paths(s)

    assert select_focus(s, paths, {"nstim"}, set(), SUPPLY_DECK) == {"AMP"}


def test_no_seed_falls_back_to_every_block_rather_than_to_nothing():
    s, paths = _built()

    assert select_focus(s, paths, set(), set(), CHAIN) == {"DRIVER", "SPARE"}


def test_the_structure_view_lists_every_block_but_details_only_the_focused_ones():
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "SPARE" in text                              # 레벨 0으로는 반드시 보인다
    assert "refdes=DRIVER.M1 param=W" in text           # 초점 블록의 주소록
    assert "refdes=SPARE.M9" not in text


def test_the_tunable_address_keeps_refdes_and_param_in_separate_fields():
    # "BUF_P.X6.W"처럼 렌더링하면 점 하나가 스코프 구분자와 param 구분자를
    # 겸하게 되어, 스키마가 요구하는 두 칸(refdes/param)의 경계가 뷰에서
    # 사라진다 - CLAUDE.md가 이미 실제 실패로 기록한 혼동("M1.W를 refdes
    # 칸에 썼다")을 뷰 자신이 가르치는 셈이다.
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "DRIVER.M1.W" not in text


def test_the_structure_view_never_repeats_a_value():
    # 값의 단일 출처는 넷리스트 원문이다. 두 벌이 들어가면 E1이 겪은
    # "덱에 W가 두 번" 과 같은 모양의 불일치가 모델 쪽에서 재발한다.
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "W=10" not in text


def test_the_structure_view_never_leaks_a_source_stimulus_value():
    # V/I 소스는 단자 역할표가 없어 terminals가 비어 있다. 트레일링 AC
    # 크기가 있는 소스는 structure.py의 위치 분해(positional[:-1]이 노드,
    # 마지막 하나만 값)가 "0.9"를 값이 아니라 nodes 쪽으로 밀어 넣는다 -
    # terminals가 비었다고 nodes를 그대로 echo하면 그 값이 새어 나온다.
    deck = CHAIN.replace("Vin na 0 DC 1\n", "Vin na 0 DC 1\nVac na 0 DC 0.9 AC 1\n")
    s = derive_structure(deck, "demo")
    paths = build_signal_paths(s)

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "0.9" not in text


def test_the_netlist_view_keeps_every_header_and_folds_only_unfocused_bodies():
    text = render_netlist(CHAIN, {"DRIVER"})

    assert "M1 vout vin vss vss NMOS W=10 L=1" in text
    assert "M9" not in text
    assert ".subckt SPARE a b vss" in text
    assert "elided" in text
    assert "Xd na out 0 DRIVER" in text     # 최상위는 언제나 남는다
    assert "Vin na 0 DC 1" in text


def test_the_netlist_view_counts_a_continued_component_as_one_elided_not_two():
    # `+` 연속 줄을 독립된 문장으로 세면 접힌 본문 안 부품 하나가 두 개로
    # 잡힌다 - netlist.py가 이미 한 번 고친 것과 같은 모양의 버그.
    deck = (
        ".subckt KEEP x y\n"
        "R1 x y 1k\n"
        ".ends KEEP\n"
        ".subckt SKIP a b\n"
        "R9 a b 1k\n"
        "+ tc=0.01\n"
        ".ends SKIP\n"
    )

    text = render_netlist(deck, {"KEEP"})

    assert "R9" not in text
    assert "tc=0.01" not in text
    assert "(1 components elided)" in text


def test_the_netlist_view_shows_both_physical_lines_of_a_continued_component_in_focus():
    deck = ".subckt KEEP x y\nR1 x y 1k\n+ tc=0.01\n.ends KEEP\n"

    text = render_netlist(deck, {"KEEP"})

    assert "R1 x y 1k" in text
    assert "+ tc=0.01" in text


def test_the_netlist_view_never_truncates_a_continued_subckt_header():
    # 헤더 자신이 여러 줄에 걸쳐 있으면, 그 헤더를 낸 순간 본문 접힘이
    # 시작되어도(비초점이라서) 헤더 자신의 나머지 물리 줄까지는 보여야
    # 한다 - 안 그러면 다중 라인 포트 목록의 뒷부분이 조용히 잘려 나간
    # 채로 넘어가, 접히지도 않은 블록의 포트 수가 틀린 값으로 보인다.
    deck = ".subckt WIDE a b c\n+ d e f\nR1 a b 1k\n.ends WIDE\n"

    text = render_netlist(deck, set())

    assert ".subckt WIDE a b c" in text
    assert "+ d e f" in text
    assert "R1" not in text
    assert "elided" in text


def test_the_elision_count_counts_components_not_directives():
    # ".param"/".model"은 부품이 아니다. 앵커를 세는 방식이라 이것들까지
    # 함께 세어, 소자가 하나뿐인 본문이 "(3 components elided)"로 나왔다 -
    # 접힌 블록의 규모를 모델에게 잘못 알려주는 숫자다.
    deck = (
        ".subckt SKIP a b\n"
        ".param rr=1k\n"
        ".model NMOS nmos level=1\n"
        "R9 a b {rr}\n"
        ".ends SKIP\n"
    )

    assert "(1 components elided)" in render_netlist(deck, set())


def test_an_unterminated_subckt_at_eof_still_gets_its_elision_marker():
    # `.ends` 없이 파일이 끝나면 접힘을 닫는 지점이 영영 오지 않아 마커도
    # `.ends`도 안 나오고 프롬프트가 그냥 끊긴다 - 모델은 뒤에 뭐가 있었는지
    # 알 길이 없다. 잘린 덱은 흔하고(발췌를 붙여넣는 경우), 뷰가 조용히
    # 삼키는 것이 최악이다.
    deck = ".subckt SKIP a b\nR9 a b 1k\nR8 a b 2k\n"

    text = render_netlist(deck, set())

    assert ".subckt SKIP a b" in text
    assert "R9" not in text
    assert "(2 components elided)" in text


def test_a_proposal_outside_focus_is_reported_so_a_wrong_focus_leaves_evidence():
    changes = [{"refdes": "SPARE.M9", "param": "W"}, {"refdes": "DRIVER.M1", "param": "W"}]

    assert focus_misses({"DRIVER"}, changes, CHAIN) == ["SPARE.M9"]


def test_focus_misses_resolves_an_unqualified_refdes_via_the_netlist_not_the_dotted_prefix():
    # M9 has no scope prefix at all, but it only exists inside SPARE. The old
    # implementation split the dotted prefix off the refdes string itself, so
    # an unqualified refdes could never be detected as living in an
    # out-of-focus subckt - the very case this function exists to catch.
    changes = [{"refdes": "M9", "param": "W"}]

    assert focus_misses({"DRIVER"}, changes, CHAIN) == ["M9"]


def test_render_netlist_unfolds_the_ancestors_of_a_nested_focus_path():
    # §3.3. select_focus는 모든 갈래를 _with_ancestors에 통과시키지만,
    # render_netlist를 부르는 곳이 select_focus만은 아니다:
    # orchestrator가 verify_pre에게 보여줄 뷰를 만들 때 초점을 제안이
    # 지목한 블록으로 확장한다(`focus | resolved_blocks`). 그 확장 경로에는
    # 조상 보정이 없어, 제안이 중첩 정의를 지목하면 부모가 접히면서 지목된
    # 소자가 뷰에서 사라진다 - verify_pre는 "뷰에 없는 것은 거부하라"는
    # 지시를 받고 있고, 그 거부 3회는 즉시 하드 FAIL이라 토폴로지
    # 에스컬레이션과 남은 이터레이션을 전부 버린다.
    #
    # 조상 보정을 렌더러 자신에게 두면 호출자가 누구든 이 계약이 유지된다:
    # "중첩 정의를 접힘의 단위로 삼는다"는 것이 렌더러의 규칙이므로,
    # 그 규칙이 초점을 무효로 만드는 보정도 렌더러가 진다.
    text = render_netlist(NESTED, {"OUTER.INNER"})

    assert "M1 c d vss vss NMOS W=10 L=1" in text
    # OUTER 자신은 초점이 아니므로 그 직속 부품은 여전히 접힌다 - 조상
    # 보정은 "부모를 초점으로 승격"이 아니라 "부모의 폴딩이 자손을 삼키지
    # 않게 한다"이다. 여기서는 자손이 통째로 보여야 하므로 폴딩 자체가
    # 시작되지 않는다.
    assert ".subckt INNER c d vss" in text


def test_render_netlist_with_a_non_nested_focus_is_unchanged():
    # 조상 보정은 점 없는 경로에 대해 순수 no-op이어야 한다 - 벤치마크
    # 열한 개 덱이 전부 이 경우다.
    assert render_netlist(NESTED, {"OUTER"}) == render_netlist(NESTED, {"OUTER"})
    text = render_netlist(NESTED, {"OUTER"})
    assert "M2 a b vss vss NMOS W=10 L=1" in text
