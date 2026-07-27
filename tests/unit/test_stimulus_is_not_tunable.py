"""최종 리뷰 CRITICAL 2의 재현과 그 폐쇄.

리뷰어가 실측한 시나리오: 주소록(tunable 인덱스)이 최상위 테스트벤치의
자극원 `Vin`을 튜닝 대상으로 광고하고, 결정론적 게이트 셋(면적/refdes/param)
중 어느 것도 그것을 막지 않으며, apply_changes가 실제로 덱을 고쳐 쓴다.
`gain_db = vdb(vout)`는 AC 자극을 100배로 키우면 20dB -> 60dB가 되므로
judge가 통과하고 verify_post는 롤백할 이유가 없다 - **회로를 하나도 안 고친
채로 PASS가 난다.**

아래 첫 번째 테스트가 그 게이트 통과 사실 자체를 고정한다(고치기 전후 모두
통과한다 - 이것이 왜 주소록이 유일한 방어선인지에 대한 증거다). 나머지
테스트가 실제 수정(주소록에서 제외 + 눈에 보이는 stimulus 줄 + 결정론적
게이트)을 고정한다."""

import os

from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import (
    apply_changes,
    check_param_applicability,
    check_refdes_resolution,
    check_stimulus_untouched,
)
from analogcoder.signal_path import build_signal_paths
from analogcoder.structure import derive_structure
from analogcoder.structure_view import render_structure

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INVERTING = os.path.join(REPO, "benchmarks", "inverting_amp", "netlist.cir")

SCALE_THE_STIMULUS = [
    {"refdes": "Vin", "param": "value", "old_value": "1", "new_value": "100"}
]


def _deck() -> str:
    with open(INVERTING) as f:
        return f.read()


def test_the_old_gate_chain_alone_never_stopped_a_stimulus_edit():
    # 이 테스트는 수정 전후 모두 통과한다. 고정하는 것은 "면적/refdes/param
    # 게이트는 자극원 편집에 대해 아무 말도 하지 않는다"는 사실이고, 그래서
    # 주소록이 그것을 광고하는 것 자체가 결함이었다.
    deck = _deck()

    assert check_area_growth(index_baseline_components(deck), SCALE_THE_STIMULUS) == (True, None)
    assert check_refdes_resolution(deck, SCALE_THE_STIMULUS)[0] is True
    assert check_param_applicability(deck, SCALE_THE_STIMULUS) == (True, None)
    # 그리고 실제로 덱이 바뀐다 - AC 크기가 100배가 되어 gain_db가 40dB
    # 뛴다. 회로는 한 글자도 안 바뀌었는데 judge는 통과한다.
    assert "Vin in 0 AC 100" in apply_changes(deck, SCALE_THE_STIMULUS)


def test_a_top_level_independent_source_is_not_in_the_tunable_index():
    structure = derive_structure(_deck(), "inverting_amp")

    assert not any(e.refdes == "Vin" for e in structure.tunable)
    # DUT 쪽 주소는 그대로 남아야 한다 - 제외가 지나치게 넓지 않은지 확인.
    assert ("Rf", "value") in {(e.refdes, e.param) for e in structure.tunable}


def test_the_structure_view_shows_the_stimulus_as_explicitly_not_tunable():
    # 조용히 빼면 "왜 Vin이 없지?"를 아무도 알 수 없다. 빠졌다는 사실이
    # 보여야 한다.
    structure = derive_structure(_deck(), "inverting_amp")
    paths = build_signal_paths(structure)
    text = render_structure(structure, paths, [], set())

    assert "stimulus (not tunable): Vin" in text
    assert "refdes=Vin" not in text


def test_a_source_inside_a_subckt_is_still_tunable():
    # 제외 근거는 "최상위의 독립 소스는 구조상 자극원"이다. 서브회로 안의
    # 소스는 DUT의 일부(예: 내부 바이어스)이므로 그 근거가 닿지 않는다.
    deck = (
        "* t\n"
        ".subckt BIAS out vss\n"
        "Vref out vss DC 1.2\n"
        ".ends BIAS\n"
        "Xb nb 0 BIAS\n"
        "Vdd vdd 0 DC 1.8\n"
        ".end\n"
    )
    entries = {(e.refdes, e.param) for e in derive_structure(deck, "t").tunable}

    assert ("BIAS.Vref", "value") in entries
    assert ("Vdd", "value") not in entries


def test_a_deterministic_gate_rejects_a_change_to_a_top_level_source():
    # 주소록에서 빼는 것은 LLM에게 하는 "권고"일 뿐이다. CRITICAL 1이
    # 가르치듯 프롬프트는 게이트가 아니다 - 결과가 "안 고친 회로에 PASS"인
    # 이상 결정론적 거부가 있어야 한다.
    ok, feedback = check_stimulus_untouched(_deck(), SCALE_THE_STIMULUS)

    assert ok is False
    assert "Vin" in feedback


def test_the_stimulus_gate_leaves_a_dut_change_alone():
    assert check_stimulus_untouched(
        _deck(), [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "12k"}]
    ) == (True, None)


def test_the_stimulus_gate_does_not_block_a_source_inside_a_subckt():
    deck = (
        "* t\n"
        ".subckt BIAS out vss\n"
        "Vref out vss DC 1.2\n"
        ".ends BIAS\n"
        "Xb nb 0 BIAS\n"
        ".end\n"
    )

    assert check_stimulus_untouched(
        deck, [{"refdes": "BIAS.Vref", "param": "value", "new_value": "1.25"}]
    ) == (True, None)
