from dataclasses import dataclass, replace

from analogcoder.pvt import CornerPoint

NOMINAL = None  # 코너 렌더링을 거치지 않은 덱 그대로. tt/27도 하나의 코너일
# 뿐이고 nominal과는 다르다 - 이름이나 숫자로 nominal을 알아내려 하지 않는다.


@dataclass(frozen=True)
class CornerSet:
    corners: tuple[CornerPoint | None, ...]  # NOMINAL이 항상 [0]
    probe_order: tuple[CornerPoint, ...]  # 집합 밖, severity 오름차순
    probe_index: int = 0


def label(point: CornerPoint | None) -> str:
    """사람이 읽는 코너 이름. NOMINAL은 "(deck)" - 어떤 코너 렌더링도 거치지
    않은 덱 그대로라는 뜻이므로 tt/27 같은 실제 코너와 혼동되어서는 안 된다."""
    if point is NOMINAL:
        return "(deck)"
    return f"{point.process}/{point.voltage}/{point.temperature}"


def _as_point(raw: dict) -> CornerPoint:
    return CornerPoint(
        process=raw["process"], voltage=raw["voltage"], temperature=raw["temperature"]
    )


def _probe_order(sweep: dict, corners: tuple) -> tuple[CornerPoint, ...]:
    """집합 밖 코너를 severity 오름차순(가장 아슬한 것부터)으로.

    severity는 Task 2가 per_corner 항목에 실어 준다. per_corner가 없으면 빈
    튜플 - 탐침 없이 도는 것이 조용히 잘못된 순서로 도는 것보다 낫다."""
    entries = []
    for entry in sweep.get("per_corner", []):
        point = _as_point(entry["corner"])
        if point in corners:
            continue
        entries.append((entry.get("severity"), point))
    entries.sort(key=lambda e: (e[0] is None, e[0]))
    return tuple(point for _, point in entries)


def seed_from_sweep(sweep: dict, spec) -> CornerSet:
    """진입 스윕에서 기준별 최악 코너를 뽑아 합집합. 새 시뮬레이션은 없다.

    value가 None인 항목도 포함한다 - 그 코너에서 측정값이 아예 안 나왔다는
    뜻이고, 회로가 거기서 동작하지 않는다는 가장 강한 증거다.

    진입 스윕의 overall_pass는 보지 않는다 - 실패한 설계의 최악 코너도
    최악 코너이고, 오히려 중간 루프가 봐야 할 코너다."""
    chosen: list[CornerPoint] = []
    for raw in sweep.get("worst_case_corners", {}).values():
        point = _as_point(raw)
        if point not in chosen:
            chosen.append(point)
    corners = (NOMINAL, *chosen)
    return CornerSet(corners=corners, probe_order=_probe_order(sweep, corners))


def grown_with(
    cs: CornerSet, sweep: dict, failing_names: list[str]
) -> tuple[CornerSet, list[CornerPoint]]:
    """실패한 기준들의 최악 코너를 집합에 더한다. 새로 더해진 것만 함께 돌려준다.

    빈 목록은 **경로 불일치**다: 판정 스윕이 실패한 코너가 전부 이미 중간 루프
    집합 안에 있다면, 같은 덱의 같은 코너를 두고 두 실행 경로가 서로 다른 말을
    하고 있는 것이다. 호출부는 그때 재시도하지 않고 그 사실을 보고한다."""
    worst = sweep.get("worst_case_corners", {})
    added: list[CornerPoint] = []
    for name in failing_names:
        raw = worst.get(name)
        if raw is None:
            continue
        point = _as_point(raw)
        if point not in cs.corners and point not in added:
            added.append(point)
    if not added:
        return cs, []
    corners = (*cs.corners, *added)
    remaining = tuple(p for p in cs.probe_order if p not in corners)
    return CornerSet(corners=corners, probe_order=remaining, probe_index=0), added


def next_probe(cs: CornerSet) -> tuple[CornerPoint | None, CornerSet]:
    """다음 탐침 코너와 회전이 진행된 CornerSet. 집합 밖이 비면 (None, cs)."""
    if not cs.probe_order:
        return None, cs
    index = cs.probe_index % len(cs.probe_order)
    return cs.probe_order[index], replace(cs, probe_index=index + 1)


def promote(cs: CornerSet, corner: CornerPoint) -> CornerSet:
    """탐침에서 실패한 코너를 선택 집합으로 올린다."""
    if corner in cs.corners:
        return cs
    return CornerSet(
        corners=(*cs.corners, corner),
        probe_order=tuple(p for p in cs.probe_order if p != corner),
        probe_index=0,
    )
