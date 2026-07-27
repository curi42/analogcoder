import pytest

from analogcoder.area import total_area
from analogcoder.area_limits import index_baseline_components
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


def test_total_area_and_area_gate_agree_on_the_adversarial_wn_case():
    """WRAPPER_DECK의 두 인스턴스(xin1/xin2)는 wn 값이 갈린다(2e-6 vs 20e-6) -
    서브회로 정의 단위 환경에서는 wn이 해소 불가로 남는다
    (params._instance_overrides의 disagreeing 경로).

    두 실제 공개 진입점 - total_area()(면적 합산)와
    index_baseline_components()(면적 게이트) - 을 **직접** 호출해서 각자
    이 경우를 같은 결론(=w 해소 불가, 추측하지 않고 건너뜀)으로 판정하는지
    고정한다. area_limits.annotate_resolved_params를 테스트가 직접 불러
    두 번 돌리면 안 된다 - 그 함수는 (component.params, component.value,
    component.scope, envs)만의 순수 함수라 같은 입력을 두 번 넣으면 항상
    같은 결과가 나오고, 그러면 total_area 안에 나중에 따로 복제된(그래서
    갈라질 수 있는) 주석 로직이 생겨도 이 테스트는 절대 못 잡는다 -
    total_area() 자신을 통과시켜야만 그 회귀를 잡는다."""
    result = total_area(WRAPPER_DECK)
    # WRAP_PAIR_TN33 has exactly two w/l-bearing devices (ma1, mb1); both must be
    # skipped, not guessed, because wn cannot resolve at the definition level.
    assert result.counted == 0
    assert result.skipped == 2

    gate_indexed = index_baseline_components(WRAPPER_DECK)
    for refdes in ("ma1", "mb1"):
        resolved = gate_indexed[f"WRAP_PAIR_TN33.{refdes}"].resolved_params
        assert "w" not in resolved
