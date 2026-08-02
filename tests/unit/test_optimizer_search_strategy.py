import pytest
from analogcoder import optimizer as opt


class FakeRun:
    """SearchRun 중 전략이 쓰는 표면만 흉내낸다.

    `accept` 는 (refdes, param) 튜플의 frozenset -> bool. 이 제안 집합이
    수락되는가를 시험이 정한다."""

    def __init__(self, knobs, accept, budget=50):
        self.knobs = knobs
        self._accept = accept
        self._budget = budget
        self.attempts = []          # list[list[ProposedStep]]
        self.exhausted_calls = []

    def spend_step(self, knob):
        if self._budget <= 0:
            return False
        self._budget -= 1
        return True

    def knob_state(self, knob):
        return opt.KnobState(token=knob.param, value=10.0, integer=False)

    def exhausted(self, knob, state, reason):
        self.exhausted_calls.append((knob, reason))

    async def attempt(self, steps):
        self.attempts.append(list(steps))
        key = frozenset((s.knob.refdes, s.knob.param) for s in steps)
        ok = self._accept(key)
        return opt.StepOutcome(accepted=ok, reason=None if ok else "no", objective=1.0)


def _knobs(*names):
    return [opt.Knob(refdes=n, param="W", direction="decrease") for n in names]


@pytest.mark.asyncio
async def test_partners_zero_is_byte_for_byte_coordinate_descent():
    """`partners=0` 이 기존 전략과 같다는 것은 주장이 아니라 시험 대상이다.

    사전 등록이 대조군으로 `coordinate_descent` 자신을 쓰기로 한 이유가
    이것이다 - 같다고 적어 두고 다른 코드를 돌리면 A/B 의 대조군이 A/B 밖에
    있게 된다."""
    knobs = _knobs("A", "B", "C")
    accept = lambda key: key == frozenset({("A", "W")})  # A만 단독 수락

    base = FakeRun(_knobs("A", "B", "C"), accept, budget=8)
    await opt.coordinate_descent(base)

    comp = FakeRun(knobs, accept, budget=8)
    await opt._compound_fallback(0)(comp)

    assert [[(s.knob.refdes, s.value) for s in a] for a in comp.attempts] == \
           [[(s.knob.refdes, s.value) for s in a] for a in base.attempts]


@pytest.mark.asyncio
async def test_a_rejected_knob_is_retried_paired_with_the_next_ranked_knob():
    knobs = _knobs("A", "B")
    # A 단독은 거절, {A,B} 조합은 수락
    accept = lambda key: key == frozenset({("A", "W"), ("B", "W")})
    run = FakeRun(knobs, accept, budget=6)
    await opt._compound_fallback(1)(run)

    pairs = [a for a in run.attempts if len(a) == 2]
    assert pairs, "조합 스텝이 한 번도 시도되지 않았다"


@pytest.mark.asyncio
async def test_the_partner_moves_in_the_opposite_direction():
    """이 전략의 전부다. 두 노브를 같은 방향으로 움직이면 면적 단계에서는
    원리적으로 거절을 구제할 수 없다 - 축소로 깨진 기준은 둘을 같이 축소하면
    더 깨진다. 설계 문서가 초안에서 그 형태를 빼면서 남긴 이유다."""
    knobs = _knobs("A", "B")
    run = FakeRun(knobs, lambda key: len(key) == 2, budget=6)
    await opt._compound_fallback(1)(run)

    pair = next(a for a in run.attempts if len(a) == 2)
    lead, partner = pair[0], pair[1]
    assert lead.knob.direction == "decrease"
    assert partner.knob.direction == "increase"
    assert lead.value < lead.state.value      # 줄었다
    assert partner.value > partner.state.value  # 늘었다


@pytest.mark.asyncio
async def test_compound_attempts_spend_the_same_budget():
    """예산을 늘리면 '조합이 좋아서'와 '더 많이 시도해서'를 가를 수 없다."""
    knobs = _knobs("A", "B", "C", "D")
    run = FakeRun(knobs, lambda key: False, budget=3)   # 전부 거절
    await opt._compound_fallback(3)(run)
    assert len(run.attempts) == 3, "조합 시도가 예산을 쓰지 않았다"
