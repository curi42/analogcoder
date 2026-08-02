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
        self.logged_events = []     # list[(step, payload)]

    def spend_step(self, knob):
        if self._budget <= 0:
            return False
        self._budget -= 1
        return True

    def knob_state(self, knob):
        return opt.KnobState(token=knob.param, value=10.0, integer=False)

    def exhausted(self, knob, state, reason):
        self.exhausted_calls.append((knob, reason))

    def log_event(self, suffix, payload):
        self.logged_events.append((suffix, payload))

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

    # 위 시나리오는 A가 예산(8)이 바닥날 때까지 계속 수락되므로 B·C에 도달하지
    # 않고, 조합 갈래에서 `_try_partners`는 단 한 번도 호출되지 않는다 -
    # partners=0과 coordinate_descent가 실제로 갈라지는 유일한 지점
    # (`if not await _try_partners(...)`가 `break`를 대신하는 그 줄)을 밟지
    # 않는다는 뜻이다. 리드가 **실제로 거절되는** 시나리오를 추가해 그 줄을
    # 실제로 밟게 한다: 아무것도 수락하지 않으면 A·B·C가 각각 한 번씩
    # 시도되고, 조합 갈래에서는 `_try_partners(..., 0)`이 매번 빈 파트너
    # 목록을 만나 `False`를 돌려주며 `break`로 이어져야 한다.
    reject_all = lambda key: False

    base_rejected = FakeRun(_knobs("A", "B", "C"), reject_all, budget=8)
    await opt.coordinate_descent(base_rejected)

    comp_rejected = FakeRun(_knobs("A", "B", "C"), reject_all, budget=8)
    await opt._compound_fallback(0)(comp_rejected)

    assert [[(s.knob.refdes, s.value) for s in a] for a in comp_rejected.attempts] == \
           [[(s.knob.refdes, s.value) for s in a] for a in base_rejected.attempts]
    # 거절이 실제로 일어났다는 것: 노브마다 정확히 한 번씩 시도되고 다음
    # 노브로 넘어간다. `_try_partners`가 (버그로) 파트너 목록이 비어 있어도
    # 무조건 `True`를 돌려주면 while 루프가 `continue`로 A만 반복하며 예산이
    # 바닥날 때까지 시도하므로, 이 단언은 그 버그에서 깨진다.
    assert len(comp_rejected.attempts) == len(knobs) == 3


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


@pytest.mark.asyncio
async def test_a_partner_that_cannot_move_opposite_is_logged_not_exhausted():
    """파트너가 반대 방향으로 못 움직일 때 예산은 이미 나갔는데(`spend_step`이
    성공했으므로) 이력에 아무것도 안 남으면 "그 파트너를 아예 고려조차 안
    했다"와 구별되지 않는다. `run.exhausted`(거절 카운터를 올린다)를 쓰면 안
    된다 - 이 파트너는 평가된 적조차 없고 자기 방향으로는 멀쩡하다."""
    lead = opt.Knob(refdes="A", param="m", direction="increase")
    partner = opt.Knob(refdes="B", param="m", direction="increase")

    class Run(FakeRun):
        def knob_state(self, knob):
            # A는 정수 노브라 increase 로 문제없이 3으로 간다. B는 이미
            # 1이라 반대 방향(decrease)으로 갈 곳이 없다(`_next_value`가
            # None을 낸다).
            if knob.refdes == "A":
                return opt.KnobState(token="m", value=2.0, integer=True)
            return opt.KnobState(token="m", value=1.0, integer=True)

    run = Run([lead, partner], lambda key: False, budget=6)  # 전부 거절
    await opt._compound_fallback(1)(run)

    assert not run.exhausted_calls, "평가된 적 없는 파트너를 거절로 세면 안 된다"

    logged = [e for e in run.logged_events if e[0] == "compound_partner_direction_unavailable"]
    assert logged, "파트너가 못 움직일 때 아무 이벤트도 안 남았다"
    _, payload = logged[0]
    assert payload["lead_refdes"] == "A"
    assert payload["lead_param"] == "m"
    assert payload["partner_refdes"] == "B"
    assert payload["partner_param"] == "m"
    assert payload["attempted_direction"] == "decrease"
    assert payload["partner_value"] == 1.0
    assert payload["budget_spent"] is True


class _FakeState:
    """`SearchRun`이 이벤트를 넘기는 곳만 흉내낸다."""

    def __init__(self):
        self.events = []  # list[(name, payload)]

    def log_event(self, name, payload):
        self.events.append((name, payload))


def test_search_run_log_event_prefixes_the_phase_label():
    """`SearchRun.log_event`가 스스로 단계 라벨을 붙인다는 것을 못박는다.

    `FakeRun`은 `SearchRun`이 아니므로(전략이 보는 표면만 흉내낸다) 이 시험은
    진짜 `SearchRun`을 세운다. `run_area_optimization`이 `agents.search_strategy`를
    면적 단계에도 그대로 넘기므로, 한 실행에서 면적 단계와 목적 단계가 같은
    전략(예: `compound_fallback_1`)을 둘 다 돌릴 수 있다 - 라벨이 빠지면
    `history.jsonl`만 보고는 어느 단계가 낸 사건인지 구별할 수 없다. 이
    시험은 라벨이 빠지면(예: `log_event`가 `suffix`를 그대로 내보내면) 깨진다."""
    state = _FakeState()
    phase = opt.PhaseConfig(objective="dummy", area_budget=None, guard_band=None, label="optimize_area")
    run = opt.SearchRun(
        spec=None,
        state=state,
        oracle=None,
        knobs=[],
        canonical_name="",
        objective_before=0.0,
        records={},
        max_steps=10,
        phase=phase,
    )

    run.log_event("compound_partner_direction_unavailable", {"lead_refdes": "A"})

    assert state.events == [
        ("optimize_area_compound_partner_direction_unavailable", {"lead_refdes": "A"})
    ]
