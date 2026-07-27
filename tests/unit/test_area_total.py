import pytest

from analogcoder.area import total_area
from analogcoder.area_limits import annotate_resolved_params, index_baseline_components
from analogcoder.netlist import parse_netlist
from analogcoder.params import build_param_envs
from tests.unit.wrapper_decks import WRAPPER_DECK

DECK = (
    "* t\n"
    "M1 d g s b NCH w=2e-6 l=1e-6 m=2\n"
    "M2 d g s b NCH w=4e-6 l=1e-6 m=1\n"
    ".end\n"
)


def test_area_is_w_times_l_times_m_summed():
    # M1: 2u*1u*2 = 4e-12,  M2: 4u*1u*1 = 4e-12
    result = total_area(DECK)

    assert result.area == pytest.approx(8e-12)
    assert result.counted == 2
    assert result.skipped == 0


def test_nf_does_not_change_area():
    # nf는 총 폭을 나누기만 한다 - w=2u nf=2는 1u 핑거 둘, 총 폭은 그대로 2u.
    with_nf = DECK.replace("m=2\n", "m=2 nf=4\n")

    assert total_area(with_nf).area == pytest.approx(total_area(DECK).area)


def test_option_scale_is_honoured():
    scaled = "* t\n.option scale=1.0u\nM1 d g s b NCH w=2 l=1 m=2\n.end\n"

    assert total_area(scaled).area == pytest.approx(4e-12)


def test_an_unresolvable_device_is_skipped_and_counted_as_such():
    # 조용히 0으로 치면 총합이 거짓이 된다. 건너뛴 개수를 드러낸다.
    deck = DECK.replace("M2 d g s b NCH w=4e-6", "M2 d g s b NCH w='wx*2'")

    result = total_area(deck)

    assert result.counted == 1
    assert result.skipped == 1
    assert result.area == pytest.approx(4e-12)


def test_a_device_without_m_counts_as_one():
    deck = "* t\nM1 d g s b NCH w=2e-6 l=1e-6\n.end\n"

    assert total_area(deck).area == pytest.approx(2e-12)


def test_a_device_with_an_unresolvable_m_is_skipped_not_guessed():
    # area_limits가 같은 이유로 m을 추측하지 않는다 - 여기서도 같다.
    deck = "* t\nM1 d g s b NCH w=2e-6 l=1e-6 m=mm\n.end\n"

    result = total_area(deck)

    assert result.counted == 0
    assert result.skipped == 1


def test_annotate_resolved_params_agrees_between_area_gate_and_area_total():
    """WRAPPER_DECK의 두 인스턴스(xin1/xin2)는 wn 값이 갈린다(2e-6 vs 20e-6) -
    서브회로 정의 단위 환경에서는 wn이 해소 불가로 남는(_instance_overrides의
    disagreeing 경로) 까다로운 경우다. area_limits.index_baseline_components
    (면적 게이트)와 이 파일의 total_area(면적 합산)가 같은 공개 헬퍼
    (area_limits.annotate_resolved_params)를 쓰는지 이 경우로 고정한다 -
    예전에는 두 곳이 같은 6줄을 각자 복제하고 있었고, 이 저장소는 넷리스트
    해소 로직이 두 갈래로 갈라져 조용히 어긋난 사고를 이미 여러 번 겪었다.
    한쪽에만 새 폴백 규칙이 붙으면 이 테스트가 실패해야 한다."""
    gate_indexed = index_baseline_components(WRAPPER_DECK)

    parsed = parse_netlist(WRAPPER_DECK)
    envs = build_param_envs(WRAPPER_DECK)

    checked = 0
    for path, subckt in parsed.subckts.items():
        for component in subckt.components:
            annotate_resolved_params(component, envs)
            gate_component = gate_indexed[f"{path}.{component.refdes}"]
            assert component.resolved_params == gate_component.resolved_params
            assert component.resolved_value == gate_component.resolved_value
            checked += 1

    assert checked > 0
    # wn is the adversarial case itself: two sibling instances disagree on it,
    # so it must be missing from both paths' resolved_params, not guessed.
    ma1 = parsed.subckts["WRAP_PAIR_TN33"].components[0]
    assert ma1.refdes == "ma1"
    assert "w" not in ma1.resolved_params
