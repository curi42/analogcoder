"""`history.jsonl`을 읽는 한 곳 - 버려진 이터레이션의 이벤트를 떨어뜨린다.

크래시는 이터레이션 **중간**에 난다. 그래서 로그에는 그 이터레이션의 부분
이벤트가 남는다(`simulation`은 기록됐고 `judge` 전에 죽는 식). 재개하면 같은
이터레이션이 다시 돌아 같은 종류의 이벤트를 또 쓰므로, 로그를 그대로 세는
소비자는 **버려진 시도의 제안을 실제 제안으로 센다** - D1 측정을 무효로 만든
것과 같은 부류의 결함이다(측정 대상이 아닌 것이 측정에 들어간다).

**로그는 자르지 않는다. 증거를 파괴하는 것은 답이 아니다.** 대신 재개할 때
`resume` 이벤트가 자기가 버리는 줄 범위(반열린 구간, 0-기준 물리 줄 번호)를
선언하고, 이 모듈이 읽을 때 그 범위를 떨어뜨린다. 원본은 디스크에 그대로
남아 있으므로 무엇이 버려졌는지 사람이 되짚을 수 있다.

여러 번 재개된 로그도 처리한다: 범위가 여러 개이고 서로 겹치지 않는다.
겹치는 경우는 하나뿐인데(재개 직후 새 체크포인트를 쓰기 전에 또 죽어서 다음
재개가 같은 체크포인트를 다시 쓰는 경우), 그때는 뒤 범위가 앞 범위를 통째로
덮으므로 "어느 범위든 하나라도 걸리면 버린다"로 결과가 같다.
"""

import json
import os

RESUME_STEP = "resume"
# resume 이벤트가 버린 줄 범위를 싣는 키. 반열린 구간 [start, end).
DISCARDED_LINES_KEY = "discarded_lines"


def line_count(path) -> int:
    """지금 이 파일의 물리 줄 수. 체크포인트가 기록하는 값이 이것이다.

    파일이 없으면 0 - 아직 아무 이벤트도 안 쓴 실행과 같은 사실이다.
    """
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return len(f.read().splitlines())


def discarded_ranges(events: list[dict]) -> list[list[int]]:
    """이벤트 목록이 선언하는 버려진 줄 범위 전부.

    **떨어뜨릴 이벤트를 포함한 전체 목록**에서 뽑아야 한다. 살아남은 것만 보고
    뽑으면, 앞선 resume 이벤트가 뒤 범위에 삼켜진 경우 그 이벤트가 선언했던
    범위를 못 읽는다 - 그 범위는 뒤 범위의 부분집합이라 결과는 같지만, 순서에
    의존하는 계산을 만들지 않는다.
    """
    ranges = []
    for event in events:
        if event.get("step") != RESUME_STEP:
            continue
        raw = event.get(DISCARDED_LINES_KEY)
        if not raw:
            continue
        ranges.append([int(raw[0]), int(raw[1])])
    return ranges


def _numbered(path) -> list[tuple[int, dict]]:
    """(물리 줄 번호, 이벤트). 빈 줄은 건너뛰되 **번호는 소비한다** - 번호가
    밀리면 체크포인트가 기록한 줄 수와 어긋나 엉뚱한 범위를 버린다."""
    with open(path) as f:
        lines = f.read().splitlines()
    return [(index, json.loads(line)) for index, line in enumerate(lines) if line.strip()]


def read_events(path, *, drop_discarded: bool = True) -> list[dict]:
    """이 실행이 실제로 겪은 이벤트들. 버려진 범위는 빠진다.

    `drop_discarded=False`는 진단용이다 - 로그 전문을 그대로 본다.
    """
    numbered = _numbered(path)
    if not drop_discarded:
        return [event for _, event in numbered]
    ranges = discarded_ranges([event for _, event in numbered])
    if not ranges:
        return [event for _, event in numbered]
    return [
        event
        for index, event in numbered
        if not any(start <= index < end for start, end in ranges)
    ]


def count_events(path, start: int, end: int) -> int:
    """`[start, end)` 줄 범위 안의 **이벤트** 수. 줄 수와 다를 수 있다(빈 줄).

    재개할 때 "이번에 무엇을 버렸는가"를 결과에 적기 위한 것이다 - 누적
    합계(`discard_summary`)와는 다른 사실이므로 둘 다 낸다.
    """
    return sum(1 for index, _ in _numbered(path) if start <= index < end)


def discard_summary(path) -> dict:
    """무엇이 버려졌는지 - **버린 것이 없어도 항상 낼 수 있는 모양**이다.

    소비자가 이것을 함께 찍어야 "버려진 줄이 0이었다"와 "버리는 계산이
    사라졌다"가 구별된다. 이 저장소에서 조용히 무력해진 검사가 아홉 번이고,
    아홉 번 다 실행 기록만으로는 알아챌 수 없었다.
    """
    numbered = _numbered(path)
    ranges = discarded_ranges([event for _, event in numbered])
    kept = read_events(path)
    return {
        "total_lines": line_count(path),
        "ranges": ranges,
        "discarded": len(numbered) - len(kept),
        "kept": len(kept),
    }
