from dataclasses import dataclass, replace

from analogcoder.pvt import CornerPoint

NOMINAL = None  # 코너 렌더링을 거치지 않은 덱 그대로. tt/27도 하나의 코너일
# 뿐이고 nominal과는 다르다 - 이름이나 숫자로 nominal을 알아내려 하지 않는다.


@dataclass(frozen=True)
class CornerSet:
    corners: tuple[CornerPoint | None, ...]  # NOMINAL이 항상 [0]
    probe_order: tuple[CornerPoint, ...]  # 집합 밖, severity 오름차순
    probe_index: int = 0

    def __post_init__(self) -> None:
        # 이 네 불변식은 이 하위 프로젝트 전체가 딛고 서는 기반이다 -
        # seed_from_sweep/grown_with/promote는 지금 이 불변식을 지키며 서로
        # 맞물리지만, 이 클래스는 public이고 frozen dataclass 기본 __init__을
        # 그대로 노출한다. 나중 태스크가 run state에서 CornerSet을
        # 역직렬화하거나 재개된 실행을 위해 직접 하나 만들면, 이 파일의
        # 함수를 하나도 거치지 않고 불변식이 깨진 값을 만들 수 있다 - 예를
        # 들어 probe_order에 이미 corners 안에 있는 코너가 남아 있으면
        # next_probe가 그 코너를 또 골라 이 프로젝트가 막으려는 낭비된
        # 시뮬레이션을 정확히 만들어 낸다. patterns.PatternMatch가 같은
        # 이유로 __post_init__에서 자기 자신과 짝지어지는 매치를 막는 것과
        # 같은 구조 - 여기서도 생성 자체를 막아 미래의 호출부가 개별적으로
        # 이 불변식을 지키는 데 의존하지 않게 한다. frozen이므로 여기서
        # 값을 고치지 않는다 - 고치는 대신 거부한다.
        if not self.corners or self.corners[0] is not NOMINAL:
            raise ValueError(
                f"CornerSet.corners[0] must be NOMINAL: {self.corners!r}"
            )
        if len(set(self.corners)) != len(self.corners):
            raise ValueError(
                f"CornerSet.corners must not contain a duplicate corner: {self.corners!r}"
            )
        overlap = set(self.probe_order) & set(self.corners)
        if overlap:
            raise ValueError(
                f"CornerSet.probe_order must not overlap corners, found {overlap!r} "
                f"in both corners={self.corners!r} and probe_order={self.probe_order!r}"
            )
        if len(set(self.probe_order)) != len(self.probe_order):
            raise ValueError(
                f"CornerSet.probe_order must not contain a duplicate corner: {self.probe_order!r}"
            )


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
    최악 코너이고, 오히려 중간 루프가 봐야 할 코너다.

    spec 인자는 지정된 인터페이스라서 유지하지만, 이 함수는 그 안의 어떤
    것도 읽지 않는다 - worst_case_corners의 각 항목이 spec.pvt_corners가
    선언한 교차곱 안에 실제로 있는지 검증하지 않는다. 오늘은 무해하지만
    (sweep은 항상 all_corners(spec.pvt_corners)에서 나온 코너로 채워진다),
    다른 출처의 sweep을 받는 순간 조용히 틀린 코너를 받아들일 수 있다."""
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
        # "point not in added" 쪽은 여기서 두 실패 기준이 같은 최악 코너를
        # 지목할 때를 잡는다 (예: gain과 pm 둘 다 FS) - 이게 없으면 added에
        # FS가 두 번 들어가고, 그대로 corners = (*cs.corners, *added)에
        # 실려 아래 CornerSet 생성에서 __post_init__의 중복 검사가 막아선다.
        # 즉 __post_init__은 최후 방어선이고, 이 줄은 그 방어선에 걸려
        # ValueError로 실행이 죽는 대신 애초에 올바른 결과를 돌려주기 위한
        # 것이다 - apply_changes가 check_refdes_resolution 이후에도 모호한
        # refdes에 대해 여전히 raise하는 것과 같은 이중 방어 구조.
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
