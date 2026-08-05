"""파레토 공선 — 세 출처의 수락점을 모아 보고하고 출하점을 고른다.

면적과 전류의 **교환율을 아무도 모른다.** 그래서 하나의 점수로 합치지 않고
둘 다 기록해 사람이 고르게 한다 - 가중합은 근거 없는 가중치가 되고, 단위가
다른 두 양을 더하는 일이기도 하다.

공선에 들어가는 점은 전부 **이미 측정된 점**이라 추가 시뮬레이션 비용이 0이다:

| 출처 | 라벨 |
|---|---|
| 튜닝 루프의 착지점 | `entry` |
| 면적 단계의 수락점 전부 | `area` |
| 전류 단계의 수락점 전부 | `objective` |

**지배당한 점도 남긴다.** 공선은 비지배 집합이지만, 지배당한 행을 지우면 증거가
사라지고 특히 착지점이 사라진다 - 큐레이션에서 "현직의 점이 비교에서 빠져
있던" 것이 조용히 무력한 게이트 12건 중 하나다. 각 행이 자기가 지배당했는지를
`dominated`로 말한다.
"""

from dataclasses import dataclass, field, replace

from analogcoder.curation import _at_least_as_good, _is_better

# 두 축 모두 **작을수록 좋다** - 면적도 전류도 그렇다. 큐레이션의 비교 원시함수는
# 기준의 연산자를 받으므로 그 방향을 이 상수 하나로 넘긴다.
_SMALLER_IS_BETTER = "<="


@dataclass(frozen=True)
class Point:
    """공선의 한 행. 전부 이미 측정된 점이다.

    `area`/`objective`가 `None`인 것은 **0이 아니라 "못 쟀다"**다
    (`AreaTotal.counted == 0`이 실제로 도달 가능하고, `objective` 선언이 없는
    스펙에서는 그 축이 아예 없다). 0으로 읽으면 잴 수 없는 점이 항상 이긴다."""

    source: str  # "entry" | "area" | "objective"
    area: float | None
    objective: float | None = None
    netlist_version: str = ""
    criteria: list[dict] = field(default_factory=list)
    # 아래 셋은 `build_front`가 채운다 - 점 하나만 보고는 알 수 없는 성질이다.
    shipped: bool = False
    corner_verified: bool = False
    dominated: bool = False


@dataclass(frozen=True)
class Front:
    points: list[Point]
    shipped_index: int
    single_axis: bool
    # "면적이 최소인 점을 골랐다"와 "면적을 아무 데서도 못 재서 착지점으로
    # 떨어졌다"는 다른 사실이다. 조용히 첫 점을 고르지 않는다.
    shipped_reason: str  # "min_area" | "area_unmeasurable"

    def as_dict(self) -> dict:
        return {
            "points": [
                {
                    "source": p.source,
                    "area": p.area,
                    "objective": p.objective,
                    "netlist_version": p.netlist_version,
                    "criteria": p.criteria,
                    "shipped": p.shipped,
                    "corner_verified": p.corner_verified,
                    "dominated": p.dominated,
                }
                for p in self.points
            ],
            "shipped_index": self.shipped_index,
            "single_axis": self.single_axis,
            "shipped_reason": self.shipped_reason,
        }


def _axes(point: Point, single_axis: bool) -> list[float | None]:
    return [point.area] if single_axis else [point.area, point.objective]


def dominates(a: Point, b: Point, single_axis: bool | None = None) -> bool:
    """`a`가 `b`를 파레토 지배하는가 - 모든 축에서 이상이고 한 축 이상에서
    의미 있게 낫다.

    "의미 있게"는 `curation.COMPARISON_REL_TOLERANCE`(1e-3)로 정의한다. 그
    값은 실측에서 왔다(가장 큰 잡음 4.2e-5, 실제 개선 0.102) - 여기서 새로
    정의하지 않고 그쪽을 import 하는 이유다. 영-허용치는 결합된 슬롯에서
    아무것도 거절하지 못하고 솔버 잡음을 주장으로 바꿨던 것이 실측돼 있다.

    **못 잰 축(`None`)이 하나라도 있으면 어느 쪽도 지배하지 못한다.** `None`을
    "같다"로 읽으면 잴 수 없는 점이 지배자가 된다 - 닫힌 실패를 고른다."""
    if single_axis is None:
        single_axis = a.objective is None and b.objective is None
    av, bv = _axes(a, single_axis), _axes(b, single_axis)
    if any(v is None for v in av) or any(v is None for v in bv):
        return False
    if not all(_at_least_as_good(_SMALLER_IS_BETTER, x, y) for x, y in zip(av, bv)):
        return False
    return any(_is_better(_SMALLER_IS_BETTER, x, y) for x, y in zip(av, bv))


def build_front(
    entry: Point,
    area_points: list[Point],
    objective_points: list[Point],
) -> Front:
    """세 출처를 하나의 공선으로 모은다. **착지점은 언제나 첫 행이다.**

    출하점은 공선 **전체**에서 면적이 최소인 점이다 - 면적 단계의 결과로
    못박지 않는다. 전류 단계가 소자를 줄여 전류와 면적을 함께 낮추는 일이
    실제로 가능하므로, 그렇게 못박으면 더 작은 점을 손에 쥐고도 버리게 된다.
    자동 단계가 겨냥한 것이 면적이고, 사람이 아무 선택도 하지 않았을 때의
    기본값이 명확해야 한다.

    **코너 확인은 출하점에만.** 공선 전체를 코너 확인하면 45코너 스윕 × N이
    되어 비용이 폭발한다. 나머지 행은 `corner_verified=False`이고, 그것이
    그 행들에 대해 이 실행이 말할 수 있는 전부다."""
    points = [entry, *area_points, *objective_points]
    # **`objective` 선언이 없으면 축이 하나뿐이라 공선이 아니다.** 그 사실을
    # 키로 싣는다 - 빼면 "공선 기능이 없다"와 구별되지 않는다.
    single_axis = all(p.objective is None for p in points)

    measurable = [i for i, p in enumerate(points) if p.area is not None]
    if measurable:
        shipped_index = min(measurable, key=lambda i: (points[i].area, i))
        shipped_reason = "min_area"
    else:
        # 조용히 첫 점을 고르는 것이 아니라, 착지점으로 떨어졌다는 것과 그
        # 이유를 함께 적는다.
        shipped_index = 0
        shipped_reason = "area_unmeasurable"

    resolved = []
    for i, p in enumerate(points):
        resolved.append(
            replace(
                p,
                shipped=(i == shipped_index),
                corner_verified=(i == shipped_index),
                dominated=any(
                    dominates(other, p, single_axis)
                    for j, other in enumerate(points)
                    if j != i
                ),
            )
        )
    return Front(
        points=resolved,
        shipped_index=shipped_index,
        single_axis=single_axis,
        shipped_reason=shipped_reason,
    )
