from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure
from analogcoder.structure_view import (
    focus_misses,
    render_netlist,
    render_structure,
    select_focus,
)

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

    assert select_focus(s, paths, {"out"}, set()) == {"DRIVER"}


def test_focus_walks_one_hop_back_to_whatever_drives_what_a_seed_senses():
    deck = CHAIN.replace("Xd na out 0 DRIVER", "Xd mid out 0 DRIVER").replace(
        "Xs p q 0 SPARE", "Xs na mid 0 SPARE"
    )
    s = derive_structure(deck, "demo")
    paths = build_signal_paths(s)

    # DRIVER는 mid를 감지하고 SPARE가 mid를 구동한다. 씨앗의 입력을 만드는
    # 블록을 못 보면 튜너가 원인 쪽을 건드릴 수 없다.
    assert select_focus(s, paths, {"out"}, set()) == {"DRIVER", "SPARE"}


def test_a_block_already_touched_this_run_stays_in_focus():
    s, paths = _built()

    assert select_focus(s, paths, {"out"}, {"SPARE.M9"}) == {"DRIVER", "SPARE"}


def test_no_seed_falls_back_to_every_block_rather_than_to_nothing():
    s, paths = _built()

    assert select_focus(s, paths, set(), set()) == {"DRIVER", "SPARE"}


def test_the_structure_view_lists_every_block_but_details_only_the_focused_ones():
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "SPARE" in text          # 레벨 0으로는 반드시 보인다
    assert "DRIVER.M1.W" in text    # 초점 블록의 주소록
    assert "SPARE.M9.W" not in text


def test_the_structure_view_never_repeats_a_value():
    # 값의 단일 출처는 넷리스트 원문이다. 두 벌이 들어가면 E1이 겪은
    # "덱에 W가 두 번" 과 같은 모양의 불일치가 모델 쪽에서 재발한다.
    s, paths = _built()

    text = render_structure(s, paths, [], {"DRIVER"})

    assert "W=10" not in text


def test_the_netlist_view_keeps_every_header_and_folds_only_unfocused_bodies():
    text = render_netlist(CHAIN, {"DRIVER"})

    assert "M1 vout vin vss vss NMOS W=10 L=1" in text
    assert "M9" not in text
    assert ".subckt SPARE a b vss" in text
    assert "elided" in text
    assert "Xd na out 0 DRIVER" in text     # 최상위는 언제나 남는다
    assert "Vin na 0 DC 1" in text


def test_a_proposal_outside_focus_is_reported_so_a_wrong_focus_leaves_evidence():
    changes = [{"refdes": "SPARE.M9", "param": "W"}, {"refdes": "DRIVER.M1", "param": "W"}]

    assert focus_misses({"DRIVER"}, changes) == ["SPARE.M9"]
