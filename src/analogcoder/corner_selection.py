from dataclasses import dataclass, replace

from analogcoder.pvt import CornerPoint
from analogcoder.spec import axis_corner_id

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
    않은 덱 그대로라는 뜻이므로 tt/27 같은 실제 코너와 혼동되어서는 안 된다.

    코너의 이름은 이제 코너 자신이 들고 있다(`CornerPoint.corner_id`). 축으로
    선언된 코너에서는 `spec.axis_corner_id`가 채운 값이라 이 함수가 예전에
    만들던 문자열과 **바이트 동일**하다."""
    if point is NOMINAL:
        return "(deck)"
    return point.corner_id


def raw_label(raw: dict | None) -> str | None:
    """`worst_case_corners` 항목 **하나**(원시 dict)의 사람이 읽는 이름.

    `label`의 dict 판이며, 좌표를 이미 갖고 있는 `CornerPoint`가 아니라
    산출물에서 되읽은 dict를 받는다. 세 갈래가 서로 다른 사실이다:

    - `None` — 그 기준에 최악 코너 항목이 **없다**
    - `"(deck)"` — 항목은 있는데 **정체성이 없다**(`pvt._corner_fields`가
      렌더링을 거치지 않은 덱에 적는 모양: `{"corner_id": None}`)
    - `sig_01` 또는 `p/v/t` — 진짜 코너

    **판별은 정체성 키의 부재로 한다. 이름 매칭이 아니다** — `_as_point`의
    거부 조건과 같은 방향이어야 하고, 두 함수를 **같은 커밋에서** 같이
    바꿔야 한다. 한쪽만 바꾸면 모든 코너가 `"(deck)"`가 되고 `cli`의
    `_argmax_drift`가 두 라벨의 문자열 비교뿐이라 `moved_count`가 **영구히
    0**이 된다 - 실행은 안 죽고, "설계가 움직여도 최악 코너는 안 움직였다"는
    재본 적 없는 결론이 report.md에 남는다. D1의 반복제안률 0.000과 정확히
    같은 자리의 무효 지표다.

    반응은 `_as_point`와 반대다: 저쪽은 거부하고 이쪽은 적기만 하는데,
    argmax 계측은 순수한 기록이고 기록이 실행을 멈출 수는 없기 때문이다.

    **여기 사는 이유**: `cli.py`와 `report.py`가 이 함수를 각자 복사해
    갖고 있었고 두 독스트링 모두 "다른 쪽과 같은 문자열을 내야 한다"고
    **주장만** 했다. 강제하는 것은 없었고, `report.py`의 사본에는 테스트가
    하나도 없었다. `label`·`_as_point`와 같은 파일에 두면 판별이 한 곳에
    모인다.
    """
    if raw is None:
        return None
    identity = _identity_of(raw)
    if identity is None:
        return "(deck)"
    return identity


def _identity_of(raw: dict) -> str | None:
    """산출물 dict 하나가 가리키는 코너의 정체성. 없으면 None.

    `_corner_fields`가 쓰는 세 모양을 그대로 되읽는다: 라벨 코너는
    `corner_id`를, 축 코너는 좌표를(정체성은 `axis_corner_id`로 **유도**),
    렌더링을 거치지 않은 덱은 아무것도 갖지 않는다."""
    corner_id = raw.get("corner_id")
    if corner_id is not None:
        return corner_id
    if raw.get("voltage") is None or raw.get("temperature") is None:
        return None
    return axis_corner_id(raw["process"], raw["voltage"], raw["temperature"])


def _as_point(raw: dict) -> CornerPoint:
    """worst_case_corners/per_corner 항목 하나를 코너로 읽는다.

    **`(deck)` 항목은 코너가 아니므로 거부한다.** pvt._corner_fields는 렌더링을
    거치지 않은 덱을 `{"corner_id": None}`으로 적고, corner_sim의 corner_worst는
    선택 집합에 NOMINAL을 포함하므로 그 모양을 실제로 만들어 낸다. 검사 없이
    통과시키면 좌표 없는 코너가 렌더러에 넘어가
    `.include ".../pdk_corner_(deck).inc"` 같은 존재하지 않는 파일을 ngspice에
    넘긴다 - "좌표가 없다"는 사실이 조용히 좌표로 둔갑하는 것이다.

    **판별은 정체성 키의 부재로 한다**(이름 매칭이 아니다). 좌표만 보던 예전
    규칙은 좌표 없는 **진짜** 코너를 100% 거부했다 - 실측으로 확인된 바,
    라벨 코너를 넣으면 스윕 첫 코너에서 ValueError로 죽는다."""
    identity = _identity_of(raw)
    if identity is None:
        raise ValueError(
            f"not a corner: {raw!r} carries neither a corner_id nor voltage/temperature "
            f"coordinates. pvt._corner_fields writes this shape for the unrendered deck "
            f'("(deck)", i.e. corner_selection.NOMINAL), which is not a point '
            f"any corner set can grow to - it is already corners[0]."
        )
    if raw.get("corner_id") is not None:
        return CornerPoint(corner_id=identity, payload=raw.get("payload"))
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
    # **probe_index를 0으로 되돌린다 - 자리를 이어받지 않는다.** probe_order에서
    # 항목이 빠졌으므로 옛 인덱스는 다른 코너를 가리키고, 그것을 그대로 쓰면
    # 회전 순서가 조용히 어긋난다. 0으로 돌리는 대가는 **회전이 멈추는 것**이다:
    # 성장이 연달아 일어나면 매번 첫 코너부터 다시 시작하므로 뒤쪽 코너는 영영
    # 안 돌 수 있다. 그래도 이쪽을 고르는 이유는 순서가 severity 오름차순(가장
    # 아슬한 것부터)이기 때문이다 - 먼저 도는 것이 가장 볼 값어치가 있는
    # 코너이고, 성장은 그 앞쪽을 목록에서 빼 가므로 정체는 스스로 풀린다.
    return CornerSet(corners=corners, probe_order=remaining, probe_index=0), added


def next_probe(cs: CornerSet) -> tuple[CornerPoint | None, CornerSet]:
    """다음 탐침 코너와 회전이 진행된 CornerSet. 집합 밖이 비면 (None, cs)."""
    if not cs.probe_order:
        return None, cs
    index = cs.probe_index % len(cs.probe_order)
    return cs.probe_order[index], replace(cs, probe_index=index + 1)


def promote(cs: CornerSet, corner: CornerPoint) -> CornerSet:
    """탐침에서 실패한 코너를 선택 집합으로 올린다.

    grown_with과 **같은 이유로** probe_index를 0으로 되돌린다: probe_order에서
    한 항목이 빠지므로 옛 인덱스는 다른 코너를 가리킨다. 여기서는 정체가 더
    가볍다 - 승격은 한 번에 하나씩이고, 회전은 다음 반복부터 다시 앞에서
    시작한다."""
    if corner in cs.corners:
        return cs
    return CornerSet(
        corners=(*cs.corners, corner),
        probe_order=tuple(p for p in cs.probe_order if p != corner),
        probe_index=0,
    )
