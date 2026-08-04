"""대안 정규화와 선택 규칙.

이 모듈이 `orchestrator.py`에서 분리되어 있는 이유는 선택 규칙이
**시뮬레이션도 LLM도 모르는 순수 함수**이기 때문이다. 설계 스펙이
"선택 규칙 양 분기 전부"를 반드시 핀하라고 적었고, 분리하면 오케스트레이터
없이 그것이 가능하다.
"""

import pytest

from analogcoder.alternatives import Alternative, Measured, normalize, select


def _alt(index, source="alternative"):
    return Alternative(
        index=index,
        changes=[{"refdes": f"M{index}", "param": "W", "old_value": "1",
                  "new_value": "2", "reasoning": "x"}],
        reasoning="x",
        source=source,
    )


# --- 선택 규칙: 양 분기 -------------------------------------------------------

def test_when_two_alternatives_pass_the_smaller_area_wins():
    """통과하는 순간이 착지점이 결정되는 순간이고, 착지점은 면적 단계의
    출발점이다. 그래서 통과했으면 면적이 결정한다 - 개선량이 훨씬 커도."""
    a, b = _alt(0, "primary"), _alt(1)
    sel = select([
        Measured(a, passed=True, area_after=9.0, improvement=5.0),
        Measured(b, passed=True, area_after=4.0, improvement=0.1),
    ])
    assert sel.winner is b
    assert sel.rule == "min_area_among_passing"
    assert sel.passing_count == 2


def test_when_nothing_passes_the_largest_improvement_wins():
    """실현 가능성은 이진값이고 루프의 목표가 그것이다. 못 통과할 때는
    진도가 결정한다 - 면적이 훨씬 작아도."""
    a, b = _alt(0, "primary"), _alt(1)
    sel = select([
        Measured(a, passed=False, area_after=9.0, improvement=5.0),
        Measured(b, passed=False, area_after=1.0, improvement=0.1),
    ])
    assert sel.winner is a
    assert sel.rule == "max_improvement"
    assert sel.passing_count == 0


def test_one_passing_alternative_still_reports_the_area_rule():
    """분기 발화 계측이 뜻을 가지려면 '통과 1개'와 '통과 2개 이상'이
    구별되어야 한다 - `passing_count`가 그것을 싣는다."""
    a, b = _alt(0, "primary"), _alt(1)
    sel = select([
        Measured(a, passed=True, area_after=9.0, improvement=0.0),
        Measured(b, passed=False, area_after=1.0, improvement=5.0),
    ])
    assert sel.winner is a
    assert sel.rule == "min_area_among_passing"
    assert sel.passing_count == 1


# --- 잴 수 없는 면적 ---------------------------------------------------------

def test_an_unmeasurable_area_loses_to_a_measurable_one_and_never_wins_by_default():
    """`area_after`가 `None`인 것은 '면적 0'이 아니라 '못 쟀다'다. `None`을
    0으로 읽으면 잴 수 없는 대안이 항상 이긴다 - `AreaTotal.counted == 0`이
    실제로 도달 가능한 상태이므로 가상의 위험이 아니다."""
    a, b = _alt(0, "primary"), _alt(1)
    sel = select([
        Measured(a, passed=True, area_after=None, improvement=0.0),
        Measured(b, passed=True, area_after=9.0, improvement=0.0),
    ])
    assert sel.winner is b
    assert sel.rule == "min_area_among_passing"


def test_all_areas_unmeasurable_falls_back_to_improvement_and_says_so():
    """폴백을 썼다는 사실이 규칙 이름에 남아야 한다. `max_improvement`와
    같은 이름을 쓰면 '통과가 없어서'와 '면적을 못 재서'가 로그에서 같아진다."""
    a, b = _alt(0, "primary"), _alt(1)
    sel = select([
        Measured(a, passed=True, area_after=None, improvement=1.0),
        Measured(b, passed=True, area_after=None, improvement=5.0),
    ])
    assert sel.winner is b
    assert sel.rule == "max_improvement_area_unmeasurable"
    assert sel.passing_count == 2


# --- 결정성 -----------------------------------------------------------------

def test_a_tie_is_broken_by_index_so_the_primary_wins():
    """동점에서 순서가 흔들리면 같은 입력이 다른 착지점을 낸다. 1차 제안이
    먼저이므로 동점이면 그것이 이긴다."""
    a, b = _alt(0, "primary"), _alt(1)
    sel = select([
        Measured(a, passed=True, area_after=5.0, improvement=0.0),
        Measured(b, passed=True, area_after=5.0, improvement=9.0),
    ])
    assert sel.winner is a


def test_select_refuses_an_empty_candidate_list():
    """빈 목록에서 승자를 만들어 내면 호출부가 '아무도 안 살아남았다'를
    놓친다. 조용히 `None`을 내지 않는다."""
    with pytest.raises(ValueError):
        select([])


# --- 정규화 -----------------------------------------------------------------

def test_normalize_puts_the_primary_first_and_reports_nothing_dropped():
    proposal = {
        "proposed_changes": [{"refdes": "M1", "param": "W", "old_value": "1",
                              "new_value": "2", "reasoning": "p"}],
        "alternatives": [
            {"changes": [{"refdes": "M2", "param": "W", "old_value": "1",
                          "new_value": "2", "reasoning": "a"}], "reasoning": "a"},
            {"changes": [{"refdes": "M3", "param": "W", "old_value": "1",
                          "new_value": "2", "reasoning": "b"}], "reasoning": "b"},
        ],
    }
    alts, dropped = normalize(proposal)
    assert [a.source for a in alts] == ["primary", "alternative", "alternative"]
    assert [a.index for a in alts] == [0, 1, 2]
    assert dropped == 0


def test_normalize_without_alternatives_is_todays_behaviour():
    """`alternatives`가 없으면 오늘 동작과 바이트 동일해야 한다."""
    proposal = {"proposed_changes": [{"refdes": "M1", "param": "W",
                "old_value": "1", "new_value": "2", "reasoning": "p"}]}
    alts, dropped = normalize(proposal)
    assert len(alts) == 1
    assert alts[0].source == "primary"
    assert alts[0].changes == proposal["proposed_changes"]
    assert dropped == 0


def test_normalize_counts_what_it_drops_over_the_cap():
    """스키마가 3을 막지만 스키마를 거치지 않는 호출부가 생길 수 있다.
    조용히 자르지 않는다 - 조용한 절단은 '전부 봤다'로 읽힌다."""
    proposal = {
        "proposed_changes": [{"refdes": "M1", "param": "W", "old_value": "1",
                              "new_value": "2", "reasoning": "p"}],
        "alternatives": [
            {"changes": [{"refdes": f"M{i}", "param": "W", "old_value": "1",
                          "new_value": "2", "reasoning": "x"}], "reasoning": str(i)}
            for i in range(2, 7)
        ],
    }
    alts, dropped = normalize(proposal)
    assert len(alts) == 3
    assert dropped == 3


def test_an_alternative_with_no_changes_is_dropped_and_counted():
    """빈 변경 집합은 '아무것도 바꾸지 않는 대안'이 아니라 무의미한 항목이다.
    적용하면 시뮬레이션이 1차 제안 이전 상태를 재고, 그것이 '통과'로 나오면
    루프가 자기 자신을 승자로 고른다."""
    proposal = {
        "proposed_changes": [{"refdes": "M1", "param": "W", "old_value": "1",
                              "new_value": "2", "reasoning": "p"}],
        "alternatives": [{"changes": [], "reasoning": "빈 것"}],
    }
    alts, dropped = normalize(proposal)
    assert len(alts) == 1 and dropped == 1


def test_as_proposal_round_trips_into_the_shape_the_gates_expect():
    """게이트와 `_record_rejected`는 `{"proposed_changes": [...]}`를 받는다.
    대안을 그 모양으로 되돌리지 못하면 호출부마다 손으로 감싸게 된다."""
    a = _alt(1)
    p = a.as_proposal()
    assert p["proposed_changes"] == a.changes
    assert "overall_reasoning" in p
