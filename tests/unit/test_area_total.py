import pytest

from analogcoder.area import total_area

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
