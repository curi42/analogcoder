"""파레토 공선 조립.

공선에 들어가는 점은 전부 **이미 측정된 점**이라 추가 시뮬레이션 비용이 0이다:
튜닝 루프의 착지점(`entry`), 면적 단계의 수락점 전부(`area`), 전류 단계의
수락점 전부(`objective`).
"""

import pytest

from analogcoder.curation import COMPARISON_REL_TOLERANCE
from analogcoder.pareto import Point, build_front, dominates


def _pt(source, area=None, objective=None, version="v0", criteria=None):
    return Point(
        source=source,
        area=area,
        objective=objective,
        netlist_version=version,
        criteria=criteria if criteria is not None else [],
    )


# --- 착지점 -------------------------------------------------------------------

def test_the_entry_point_is_always_in_the_front():
    """큐레이션에서 "현직의 점이 비교에서 빠져 있던" 것이 조용히 무력한 게이트
    12 건 중 하나다. 아무것도 바꾸지 않는 것이 최선일 수 있고, 그 선택지가
    표에 없으면 사람이 고를 수 없다."""
    f = build_front(entry=_pt("entry", area=10.0), area_points=[], objective_points=[])
    assert [p.source for p in f.points] == ["entry"]
    assert f.points[f.shipped_index].source == "entry"


def test_a_dominated_entry_point_is_kept_and_labelled_not_dropped():
    """지배당한 행을 지우면 증거가 사라진다. 공선은 **측정된 점 전부**를 싣고
    각 행이 자기가 지배당했는지를 말한다."""
    f = build_front(
        entry=_pt("entry", area=10.0, objective=100.0),
        area_points=[_pt("area", area=8.0, objective=100.0)],
        objective_points=[],
    )
    assert len(f.points) == 2
    entry = next(p for p in f.points if p.source == "entry")
    assert entry.dominated is True
    assert f.points[f.shipped_index].source == "area"


# --- 출하점 -------------------------------------------------------------------

def test_the_shipped_point_is_the_minimum_area_across_all_three_sources():
    """1 단계의 결과로 못박지 않는다 - 전류 단계가 소자를 줄여 전류와 면적을
    **함께** 낮추는 일이 실제로 가능하므로, 그렇게 못박으면 더 작은 점을 손에
    쥐고도 버리게 된다."""
    f = build_front(
        entry=_pt("entry", area=10.0, objective=100.0),
        area_points=[_pt("area", area=8.0, objective=100.0)],
        objective_points=[_pt("objective", area=7.0, objective=90.0)],
    )
    assert f.points[f.shipped_index].source == "objective"


def test_only_the_shipped_point_claims_corner_verification():
    """공선 전체를 코너 확인하면 45 코너 스윕 x N 이 되어 비용이 폭발한다."""
    f = build_front(
        entry=_pt("entry", area=10.0),
        area_points=[_pt("area", area=8.0)],
        objective_points=[],
    )
    assert sum(p.corner_verified for p in f.points) <= 1
    assert f.points[f.shipped_index].shipped is True
    assert all(not p.shipped for i, p in enumerate(f.points) if i != f.shipped_index)


def test_an_unmeasurable_area_never_ships_by_default():
    """`area is None` 은 면적 0 이 아니라 "못 쟀다" 다. 0 으로 읽으면 잴 수
    없는 점이 항상 출하된다."""
    f = build_front(
        entry=_pt("entry", area=None),
        area_points=[_pt("area", area=8.0)],
        objective_points=[],
    )
    assert f.points[f.shipped_index].source == "area"


def test_when_no_point_has_a_measurable_area_the_entry_ships_and_the_reason_is_recorded():
    """아무것도 못 골랐을 때 조용히 첫 점을 고르지 않는다 - 왜 그렇게 됐는지가
    남아야 "면적이 최소인 점을 골랐다" 와 구별된다."""
    f = build_front(
        entry=_pt("entry", area=None),
        area_points=[_pt("area", area=None)],
        objective_points=[],
    )
    assert f.points[f.shipped_index].source == "entry"
    assert f.shipped_reason == "area_unmeasurable"


# --- 축이 하나 ----------------------------------------------------------------

def test_without_an_objective_the_front_is_one_axis_and_says_so():
    """키를 빼면 "공선 기능이 없다" 와 구별되지 않는다."""
    f = build_front(entry=_pt("entry", area=10.0), area_points=[], objective_points=[])
    assert f.single_axis is True


def test_with_an_objective_it_is_two_axes():
    f = build_front(
        entry=_pt("entry", area=10.0, objective=100.0),
        area_points=[],
        objective_points=[_pt("objective", area=9.0, objective=95.0)],
    )
    assert f.single_axis is False


# --- 지배 판정 ----------------------------------------------------------------

def test_the_tolerance_actually_rejects_something():
    """영-허용치의 반대 방향 결함도 핀한다. 1e-3 안쪽의 차이는 솔버 잡음이지
    지배가 아니다 - 실측 잡음 4.2e-5, 실차 0.102 사이에 있는 값이다."""
    assert COMPARISON_REL_TOLERANCE == 1e-3
    # 상대차 1e-4
    assert not dominates(_pt("a", area=1.0000, objective=1.0),
                         _pt("b", area=1.0001, objective=1.0))
    # 상대차 1e-2
    assert dominates(_pt("a", area=1.00, objective=1.0),
                     _pt("b", area=1.01, objective=1.0))


def test_dominance_needs_at_least_as_good_on_every_axis():
    """면적은 낫지만 전류가 나쁘면 지배가 아니다 - 그것이 파레토의 정의이고,
    두 목적의 교환율을 아무도 모른다는 것이 이 설계가 파레토를 고른 이유다."""
    assert not dominates(_pt("a", area=1.0, objective=2.0),
                         _pt("b", area=2.0, objective=1.0))


def test_a_point_with_an_unmeasurable_axis_dominates_nothing():
    """못 잰 축을 "같다"로 읽으면 잴 수 없는 점이 지배자가 된다. 닫힌 실패."""
    assert not dominates(_pt("a", area=None, objective=1.0),
                         _pt("b", area=2.0, objective=2.0))
    assert not dominates(_pt("a", area=1.0, objective=1.0),
                         _pt("b", area=None, objective=2.0))


def test_on_a_single_axis_only_area_decides_dominance():
    assert dominates(_pt("a", area=1.0), _pt("b", area=2.0))
    assert not dominates(_pt("a", area=2.0), _pt("b", area=1.0))


# --- 직렬화 -------------------------------------------------------------------

def test_the_front_serialises_with_every_row_saying_what_it_is():
    """"결과는 자기가 반환하는 덱을 설명해야 한다" 가 다섯 번 재발한 규칙이다.
    각 행이 출하 여부와 코너 확인 여부를 스스로 말한다."""
    f = build_front(
        entry=_pt("entry", area=10.0, objective=100.0, version="v0"),
        area_points=[_pt("area", area=8.0, objective=100.0, version="v3")],
        objective_points=[],
    )
    d = f.as_dict()
    assert d["single_axis"] is False
    assert d["shipped_index"] == f.shipped_index
    assert d["shipped_reason"] == "min_area"
    for row in d["points"]:
        assert set(row) >= {
            "source", "area", "objective", "netlist_version",
            "shipped", "corner_verified", "dominated",
        }


# --- cli 조립: 면적 단계의 `objective`는 축이 아니다 --------------------------

def test_the_area_phases_objective_is_not_a_second_axis():
    """면적 단계에서는 `objective`가 곧 파생 면적이다(`AREA_OBJECTIVE`).
    그것을 목적 축으로 실으면 **두 축이 같은 값인 가짜 2축 공선**이 되고,
    `single_axis`가 거짓이 되어 리포트가 "축이 하나여서 공선이 아니다"를
    적어야 할 자리에 표를 그린다. 실제 데이터에서 잡았다 -
    `two_stage_opamp/spec.yaml`의 area 와 objective 가 둘 다 2.370369e-10."""
    from analogcoder.cli import _build_pareto_front

    front = _build_pareto_front({
        "final_criteria": [],
        "area_optimization": {
            "area_before": 2.370369e-10,
            "objective_before": 2.370369e-10,   # = 면적. 축이 아니다.
            "accepted_points": [
                {"version": 0, "area": 2.370369e-10, "objective": 2.370369e-10,
                 "criteria": [], "landed": True},
            ],
        },
        "optimization": {},
    })
    assert front.single_axis is True
    assert all(p.objective is None for p in front.points)


def test_the_objective_phases_points_do_carry_the_second_axis():
    from analogcoder.cli import _build_pareto_front

    front = _build_pareto_front({
        "final_criteria": [],
        "area_optimization": {
            "area_before": 1.0e-8,
            "objective_before": 1.0e-8,
            "accepted_points": [
                {"version": 0, "area": 1.0e-8, "objective": 1.0e-8, "criteria": [], "landed": False},
                {"version": 4, "area": 8.9e-9, "objective": 8.9e-9, "criteria": [], "landed": True},
            ],
        },
        "optimization": {
            "objective_before": 212.99,
            "accepted_points": [
                {"version": 9, "area": 8.8e-9, "objective": 211.68, "criteria": [], "landed": True},
            ],
        },
    })
    assert front.single_axis is False
    entry = next(p for p in front.points if p.source == "entry")
    assert entry.objective == 212.99
    # 같은 버전이 두 단계에 걸쳐 나와도 행은 하나다.
    assert len({p.netlist_version for p in front.points}) == len(front.points)
