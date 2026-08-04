"""튜너가 낸 대안들을 정규화하고, 측정된 대안 중 하나를 고른다.

**이 모듈은 시뮬레이션도 LLM도 모른다.** 선택 규칙은 순수 함수이고, 그래서
설계 스펙이 "반드시 핀하라"고 적은 양 분기를 오케스트레이터 없이 단위
테스트할 수 있다. 배선은 `orchestrator.py`가 한다.

선택 규칙의 근거는 임계값이 아니라 문제의 구조다:

- 실현 가능성은 이진값이고 루프의 목표가 그것이다. **못 통과할 때는 진도가
  결정한다** - 통과 뒤의 추가 개선량은 루프에게 의미가 없다.
- **여러 대안이 다 통과하는 순간이 착지점이 결정되는 순간**이고, 착지점은 곧
  면적 최소화 단계의 출발점이다. 그래서 통과하는 순간에는 면적이 결정한다.

가중치도 허용치도 없다. 개선량만으로 고르면 면적 계산은 돌지만 선택을 한 번도
바꾸지 않는 **조용히 무력한 게이트**가 되고, 면적만으로 고르면 거의 개선되지
않는 작은 변경을 계속 골라 수렴하지 못한다.
"""

from dataclasses import dataclass, field

MAX_ALTERNATIVES = 3


@dataclass(frozen=True)
class Alternative:
    """튜너가 낸 변경 집합 하나. 1차 제안도 여기 들어온다."""

    index: int
    changes: list[dict]
    reasoning: str
    # "primary" | "alternative". 로그에서 1차 제안이 이겼는지를 보려면
    # 순서(index 0)만으로는 부족하다 - 정규화가 바뀌면 그 규약이 조용히 깨진다.
    source: str

    def as_proposal(self) -> dict:
        """게이트와 `_record_rejected`가 받는 모양으로 되돌린다.

        호출부마다 손으로 감싸면 키 이름이 갈라진다."""
        return {
            "proposed_changes": self.changes,
            "overall_reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class Measured:
    """한 대안을 실제로 적용해 잰 결과.

    `area_after`가 `None`인 것은 **면적 0이 아니라 '못 쟀다'**다
    (`AreaTotal.counted == 0`이 실제로 도달 가능하다). 0으로 읽으면 잴 수 없는
    대안이 항상 이긴다."""

    alt: Alternative
    passed: bool
    area_after: float | None
    improvement: float


@dataclass(frozen=True)
class Selection:
    winner: Alternative
    # "min_area_among_passing" | "max_improvement" | "max_improvement_area_unmeasurable"
    # 셋째는 폴백을 썼다는 사실을 이름에 남긴다 - `max_improvement`와 같은
    # 이름을 쓰면 "통과가 없어서"와 "면적을 못 재서"가 로그에서 같아진다.
    rule: str
    passing_count: int
    candidates: list[Measured] = field(default_factory=list)


def normalize(proposal: dict) -> tuple[list[Alternative], int]:
    """`{proposed_changes, alternatives?}`를 하나의 목록으로 편다.

    1차 제안이 언제나 첫 번째다(동점의 승자이므로 순서가 규칙의 일부다).
    반환은 `(대안 목록, 버린 개수)`이고, **버린 개수는 조용히 0이 되지
    않는다** - 조용한 절단은 "전부 봤다"로 읽힌다.

    버리는 경우는 둘이다: 상한 `MAX_ALTERNATIVES`를 넘은 것과, 변경이 하나도
    없는 것. 후자를 통과시키면 시뮬레이션이 적용 이전 상태를 재고, 그 결과가
    통과로 나오면 루프가 **아무것도 바꾸지 않은 자기 자신**을 승자로 고른다.
    """
    dropped = 0
    out: list[Alternative] = [
        Alternative(
            index=0,
            changes=proposal["proposed_changes"],
            reasoning=proposal.get("overall_reasoning", ""),
            source="primary",
        )
    ]
    for entry in proposal.get("alternatives") or []:
        changes = entry.get("changes") or []
        if not changes:
            dropped += 1
            continue
        if len(out) >= MAX_ALTERNATIVES:
            dropped += 1
            continue
        out.append(
            Alternative(
                index=len(out),
                changes=changes,
                reasoning=entry.get("reasoning", ""),
                source="alternative",
            )
        )
    return out, dropped


def select(candidates: list[Measured]) -> Selection:
    """측정된 대안 중 하나를 고른다.

    빈 목록에서 승자를 만들어 내지 않는다 - 조용히 `None`을 내면 호출부가
    "아무도 안 살아남았다"를 놓치고, 그 경로는 오늘의 재시도가 담당한다."""
    if not candidates:
        raise ValueError("고를 대안이 없다 - 호출부가 재시도 경로로 가야 한다")

    passing = [m for m in candidates if m.passed]
    passing_count = len(passing)

    if passing:
        measurable = [m for m in passing if m.area_after is not None]
        if measurable:
            # 동점은 index로 깬다 - 흔들리면 같은 입력이 다른 착지점을 낸다.
            winner = min(measurable, key=lambda m: (m.area_after, m.alt.index))
            return Selection(winner.alt, "min_area_among_passing", passing_count, candidates)
        winner = max(passing, key=lambda m: (m.improvement, -m.alt.index))
        return Selection(
            winner.alt, "max_improvement_area_unmeasurable", passing_count, candidates
        )

    winner = max(candidates, key=lambda m: (m.improvement, -m.alt.index))
    return Selection(winner.alt, "max_improvement", passing_count, candidates)
