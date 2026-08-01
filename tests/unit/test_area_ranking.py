"""노브별 면적 이득 순위 - 시뮬레이션도 LLM도 쓰지 않는다."""
from analogcoder.area_ranking import rank_by_area_gain
from analogcoder.optimizer import _format_value, _next_value

DECK = (
    "* t\n"
    ".subckt AMP a b vss\n"
    "Mbig  a b vss vss NCH w=100 l=10\n"
    "Msml  a b vss vss NCH w=2 l=1 nf=4\n"
    ".ends AMP\n"
    "Xa p q 0 AMP\n"
    "Vdd vdd 0 DC 1.8\n"
    ".end\n"
)


def _make_change(refdes, param, current, integer):
    """optimizer가 주입할 것과 같은 것 - 스텝 규칙을 복제하지 않는다."""
    target = _next_value(current, integer, "decrease")
    if target is None:
        return None
    return {
        "refdes": refdes,
        "param": param,
        "old_value": _format_value(current, integer),
        "new_value": _format_value(target, integer),
    }


BIG = ("AMP.Mbig", "w", 100.0, False)
SMALL = ("AMP.Msml", "w", 2.0, False)
FINGERS = ("AMP.Msml", "nf", 4.0, True)
FINGERS_AT_FLOOR = ("AMP.Msml", "nf", 1.0, True)


def test_absolute_gain_wins_not_ratio():
    """큰 소자의 10%가 작은 소자의 10%보다 앞선다.

    비율로 정렬하면 둘이 동률이 되고, 시뮬레이션 예산이 면적을 거의 못 줄이는
    후보에 먼저 쓰인다 - 이 단계의 존재 이유가 사라진다."""
    ranking = rank_by_area_gain(DECK, [SMALL, BIG], _make_change)
    assert [(e.refdes, e.param) for e in ranking.entries] == [
        ("AMP.Mbig", "w"),
        ("AMP.Msml", "w"),
    ]
    assert ranking.entries[0].gain > ranking.entries[1].gain


def test_nf_lands_in_zero_gain_and_never_in_the_ranking():
    """핑거 분할은 총 폭을 바꾸지 않으므로 면적 중립이다.

    별도의 nf 배제 규칙을 두지 않는 것이 요점이다 - 이득이 0이라는 사실이
    스스로 nf를 밀어낸다. 규칙을 손으로 적으면 그 규칙이 언젠가 진짜 면적
    중립 파라미터를 놓친다."""
    ranking = rank_by_area_gain(DECK, [FINGERS], _make_change)
    assert ranking.entries == []
    assert ranking.zero_gain == ["AMP.Msml.nf"]
    assert ranking.unknown == []


def test_zero_gain_and_unknown_are_different_lists():
    """"이득이 없다"와 "이득을 잴 수 없다"는 다른 사실이다.

    후자는 그 노브가 탐색에서 사실상 사라졌다는 뜻이고, 합쳐 두면 몇 개가
    사라졌는지 아무도 모른다."""
    ranking = rank_by_area_gain(DECK, [FINGERS_AT_FLOOR], _make_change)
    assert ranking.entries == []
    assert ranking.zero_gain == []
    assert ranking.unknown == ["AMP.Msml.nf"]


def test_a_knob_that_cannot_be_applied_is_unknown_not_a_crash():
    """적용 자체가 안 되는 노브가 단계 전체를 죽이면 안 된다."""
    ranking = rank_by_area_gain(DECK, [("NOPE", "w", 1.0, False)], _make_change)
    assert ranking.entries == []
    assert ranking.unknown == ["NOPE.w"]


def test_it_never_simulates_and_never_calls_an_llm():
    """이 모듈이 비싼 것을 부르지 않는다는 사실 자체를 핀한다.

    나중에 누군가 '더 정확한 이득'을 위해 시뮬레이션을 넣으면 167 노브 x
    시뮬레이션이 되어 이 단계가 감당 불가능해진다. 그 변경이 여기서
    깨져야 한다."""
    import analogcoder.area_ranking as mod

    source = open(mod.__file__, encoding="utf-8").read()
    for forbidden in ("simulate", "run_agent", "backend", "AgentBackend"):
        assert forbidden not in source, f"{forbidden} 이 들어왔다"
