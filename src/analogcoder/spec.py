import os
from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class CornerPoint:
    """스윕할 한 점. **불변이며 필드로 비교·해시된다** - `CornerSet`의 집합
    연산 전부가 그 위에 서 있다.

    여기(스펙 모듈)에 사는 이유: 코너는 스펙이 **선언**하는 것이고, `pvt.py`는
    그것을 렌더링하고 도는 쪽이다. 방향도 그렇게만 성립한다 - `pvt.py`가
    `spec.py`를 import하므로 반대는 순환이다. `pvt`가 이 이름을 그대로
    재수출하므로 `from analogcoder.pvt import CornerPoint`는 계속 동작한다.

    **여기에 NaN을 절대 넣지 마라.** `NaN != NaN`이므로 그런 값은 자기
    자신과도 같지 않고, 그러면 `point not in cs.corners`가 이미 있는 코너에
    대해 참이 된다 - `grown_with`가 다시 추가하고 중복 검사가 진단을
    `ValueError`로 바꾼다. 오늘 이것을 만드는 코드는 없다(좌표는 스펙에서
    온다). 코너를 **측정**에서 유도하는 첫 사람을 위한 규칙이다."""

    process: str
    voltage: float
    temperature: float


@dataclass
class Criterion:
    name: str
    measurement: str
    operator: str
    threshold: float
    unit: str | None = None


@dataclass
class PVTCorners:
    """스윕할 코너의 **열거**. 축 선언은 로더에서 여기로 전개되는 설탕이다.

    **왜 곱이 아니라 목록인가.** 대상 흐름에서 사인오프가 요구하는 것은
    데카르트 곱 전체가 아니라 **사람이 고른 서명 코너 N개**이고, 그 선택은
    이 저장소 밖의 코드가 한다(2026-07-29 확인). 그런 부분 격자 - 금지 조합이
    빠진 집합 - 는 **어떤 축 선언으로도 만들 수 없다.** 목록은 곱을 표현할 수
    있지만(전개해서 나열) 곱은 임의의 목록을 표현할 수 없으므로, 열거형이 두
    세계의 정확한 공통 표현이다.

    그래서 **나중에 "곱으로 되돌리는 최적화"를 하면 안 된다** - 표현력이
    줄어들고, 곱으로 코너를 생성하면 존재하지 않는 조합을 가리키게 된다.

    `process`/`voltage`/`temperature`는 축 선언으로 만들어진 경우에만 채워지는
    **원본 기록**이다. 명시 목록으로 선언되면 비어 있다 - 그 경우 축이라는
    것이 존재하지 않기 때문이고, 없는 축을 열거에서 역산해 채우는 것은 이
    저장소가 금지한 추측이다(파일명에서 축 정체성을 읽는 것과 같은 부류).
    코너를 소비하는 코드는 `corners`만 읽는다.
    """

    corners: list[CornerPoint] | None = None
    process: list[str] = field(default_factory=list)
    voltage: list[float] = field(default_factory=list)
    temperature: list[float] = field(default_factory=list)

    def __post_init__(self):
        """축만 주고 만들면 **여기서 한 번** 전개된다 - 곱은 생성 시점의
        설탕이고, 그 뒤로는 아무도 곱을 다시 만들지 않는다.

        전개를 생성자에 두는 이유는 `corners`가 유일한 진실이 되게 하기
        위해서다. 소비자가 축을 읽어 곱을 다시 만들 수 있으면 명시 목록으로
        선언된 부분 격자에서 그 경로가 조용히 틀린 코너 집합을 만든다."""
        if self.corners is None:
            self.corners = [
                CornerPoint(process=p, voltage=v, temperature=t)
                for p in self.process
                for v in self.voltage
                for t in self.temperature
            ]


@dataclass
class OptimizeSpec:
    """스펙에 여유가 있을 때 무엇을 어디까지 줄일지. 선언이 없으면
    최적화 단계 자체를 돌리지 않는다 - 조용히 안 도는 것과 명시적으로
    안 도는 것은 다르다."""

    objective: str
    area_budget: float
    guard_band: float


@dataclass
class CornerReduction:
    """중간 반복의 코너 축소 설정.

    enabled=False면 오늘 동작(nominal 한 점)이 그대로다. pvt_corners가 선언되지
    않은 스펙에서는 축소할 것이 없으므로 이 블록이 있어도 아무 일도 하지
    않으며, 그 사실은 cli가 로그로 남긴다."""

    enabled: bool = True
    retry_budget: int = 2
    probe: bool = True


@dataclass
class Testbench:
    name: str
    netlist_path: str
    analyses: list[str]
    control_block: str
    criteria: list[Criterion]


@dataclass
class TargetSpec:
    circuit_name: str
    testbenches: list[Testbench]
    pvt_corners: PVTCorners | None = None
    optimize: OptimizeSpec | None = None
    corner_reduction: CornerReduction | None = None

    @property
    def canonical(self) -> Testbench:
        return self.testbenches[0]

    @property
    def all_criteria(self) -> list[Criterion]:
        return [c for tb in self.testbenches for c in tb.criteria]


def _load_criteria(raw_criteria: list[dict]) -> list[Criterion]:
    return [
        Criterion(
            name=c["name"],
            measurement=c["measurement"],
            operator=c["operator"],
            threshold=float(c["threshold"]),
            unit=c.get("unit"),
        )
        for c in raw_criteria
    ]


def _axis(raw_pvt: dict, key: str) -> list:
    """축 하나를 리스트로 꺼낸다. 검사하는 것은 **모양뿐**이고 내용은 보지
    않는다.

    `process: [tt, ss, ff]`를 대괄호 없이 `process: tt`로 적으면 YAML이
    문자열을 주고, 그 문자열은 순회 가능하므로 아무 예외 없이 문자 단위로
    풀려 CornerPoint(process='t') 넷이 만들어진다. 그 코너는
    존재하지 않는 pdk_corner_t.inc를 include하므로 45코너 전부가 NaN·FAIL이
    되고, 사람이 보는 표층 신호는 "회로가 모든 코너에서 망가졌다"가 된다 -
    원인은 대괄호 두 개다. 빈 리스트도 같은 부류로 거부한다: 축 하나가
    비면 itertools.product가 0점을 내므로, pvt_corners를 선언한 스펙이
    코너를 하나도 안 돌면서 아무 말도 하지 않는다. 같은 파일의
    _load_corner_reduction이 이미 세워 둔 규율("조용히 0으로 동작하느니
    크게 터진다")을 이 함수에도 적용한다.

    **내용은 검사하지 않는다.** process 라벨을 알려진 집합(tt/ss/ff)으로
    제한하면 라벨로 코너를 선언하는 흐름(대상 환경에서 코너가 좌표가
    아니라 이름으로 오는 경우)을 로더가 구조적으로 막게 된다 - 이 저장소가
    "이름으로 레일을 알아보기"를 금지하는 것과 같은 이유로, 모양은 사실이고
    내용은 추측이다."""
    if key not in raw_pvt:
        raise ValueError(f"pvt_corners.{key} is required")
    value = raw_pvt[key]
    if isinstance(value, str) or not isinstance(value, list):
        raise ValueError(
            f"pvt_corners.{key} must be a list, not {type(value).__name__}: {value!r} "
            f"(a bare scalar is iterated element-by-element and yields nonsense corners)"
        )
    if not value:
        raise ValueError(
            f"pvt_corners.{key} must not be empty: an empty axis makes the corner grid "
            f"zero points while the spec still declares pvt_corners"
        )
    return value


def _load_pvt_corners(raw: dict) -> PVTCorners | None:
    raw_pvt = raw.get("pvt_corners")
    if raw_pvt is None:
        return None
    if not isinstance(raw_pvt, dict):
        raise ValueError(
            f"pvt_corners must be a mapping of process/voltage/temperature axes, not "
            f"{type(raw_pvt).__name__}: {raw_pvt!r}"
        )

    if "corners" in raw_pvt:
        # 축 선언과 명시 목록이 함께 있으면 어느 쪽이 이기는지 **추측해야**
        # 한다. 이 저장소는 그런 자리에서 조용히 한쪽을 고르지 않는다.
        axes_present = [k for k in ("process", "voltage", "temperature") if k in raw_pvt]
        if axes_present:
            raise ValueError(
                f"pvt_corners declares both an explicit 'corners' list and the axis "
                f"key(s) {axes_present} - declare one or the other, never both"
            )
        return _explicit_corners(raw_pvt["corners"])

    process = _axis(raw_pvt, "process")
    for entry in process:
        # 코너 이름은 include 파일 이름의 조각이 되므로 문자열이어야 한다.
        # `process: [tt, 1.8]`(들여쓰기 실수로 전압이 섞인 경우)은 float를
        # 문자열로 포매팅해 pdk_corner_1.8.inc를 만든다.
        if not isinstance(entry, str):
            raise ValueError(
                f"pvt_corners.process entries must be strings, not "
                f"{type(entry).__name__}: {entry!r}"
            )

    def numbers(key: str) -> list[float]:
        out = []
        for entry in _axis(raw_pvt, key):
            try:
                out.append(float(entry))
            except (TypeError, ValueError):
                # float()의 기본 메시지는 어느 축인지 말하지 않는다.
                raise ValueError(
                    f"pvt_corners.{key} entries must be numbers, not {entry!r}"
                ) from None
        return out

    # 전개는 PVTCorners.__post_init__ 이 한다 - 곱을 만드는 자리를 하나로 둔다.
    return PVTCorners(
        process=process,
        voltage=numbers("voltage"),
        temperature=numbers("temperature"),
    )


def _explicit_corners(raw_corners) -> PVTCorners:
    """사람이 고른 서명 코너 목록. 축 선언으로는 표현할 수 없는 부분 격자다.

    `process`/`voltage`/`temperature`는 **비워 둔다**. 열거에서 축을 역산해
    채우면 선언되지 않은 조합이 축 목록에 나타나고, 그것을 읽는 다음 사람이
    "이 스펙은 곱을 돈다"고 읽는다. 없는 사실을 만들지 않는다.
    """
    if not isinstance(raw_corners, list):
        raise ValueError(
            f"pvt_corners.corners must be a list of corner mappings, not "
            f"{type(raw_corners).__name__}: {raw_corners!r}"
        )
    if not raw_corners:
        # 빈 목록은 "코너 없음"이 아니라 선언 실수다 - 코너를 안 돌 생각이면
        # `pvt_corners` 블록을 안 쓰면 되고, 그 경우는 이미 따로 로그된다.
        raise ValueError("pvt_corners.corners is empty - omit the pvt_corners block instead")

    points: list[CornerPoint] = []
    for index, entry in enumerate(raw_corners):
        if not isinstance(entry, dict):
            raise ValueError(
                f"pvt_corners.corners[{index}] must be a mapping with process/voltage/"
                f"temperature, not {type(entry).__name__}: {entry!r}"
            )
        for key in ("process", "voltage", "temperature"):
            if key not in entry:
                # 빠진 좌표를 기본값으로 채우면 N개 코너가 조용히 같은 조건을
                # 돌면서 코너별 값으로 보고된다.
                raise ValueError(f"pvt_corners.corners[{index}] has no '{key}': {entry!r}")
        if not isinstance(entry["process"], str):
            raise ValueError(
                f"pvt_corners.corners[{index}].process must be a string, not "
                f"{type(entry['process']).__name__}: {entry['process']!r}"
            )
        numbers = {}
        for key in ("voltage", "temperature"):
            try:
                numbers[key] = float(entry[key])
            except (TypeError, ValueError):
                raise ValueError(
                    f"pvt_corners.corners[{index}].{key} must be a number, not {entry[key]!r}"
                ) from None
        points.append(
            CornerPoint(
                process=entry["process"],
                voltage=numbers["voltage"],
                temperature=numbers["temperature"],
            )
        )

    # `CornerSet`이 중복을 불변식으로 거부하므로, 통과시키면 나중에 진단이
    # `ValueError`로 바뀌어 사유가 흐려진다. 선언 자리에서 거부한다.
    seen: set[CornerPoint] = set()
    for point in points:
        if point in seen:
            raise ValueError(
                f"pvt_corners.corners has a duplicate corner: "
                f"{point.process}/{point.voltage}/{point.temperature}"
            )
        seen.add(point)
    return PVTCorners(corners=points)


def _load_optimize(raw: dict) -> OptimizeSpec | None:
    raw_opt = raw.get("optimize")
    if raw_opt is None:
        return None
    return OptimizeSpec(
        objective=raw_opt["objective"],
        area_budget=float(raw_opt["area_budget"]),
        guard_band=float(raw_opt["guard_band"]),
    )


def _load_corner_reduction(raw: dict) -> CornerReduction | None:
    block = raw.get("corner_reduction")
    if block is None:
        return None

    # Helper to validate and extract booleans — fail loud like int/float do.
    # bool("false") returns True (non-empty string), silently inverting explicit false.
    def get_bool(key: str, default: bool) -> bool:
        value = block.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(
                f"corner_reduction.{key} must be a boolean, not {type(value).__name__}: {value!r}"
            )
        return value

    # A negative budget silently behaves as 0 - the re-entry loop compares
    # `attempt >= retry_budget` and 0 >= -1 on the first pass - so a spec that
    # believes it enabled re-entry gets none, with nothing said. Same reason
    # get_bool above fails loud rather than coercing.
    retry_budget = int(block.get("retry_budget", 2))
    if retry_budget < 0:
        raise ValueError(
            f"corner_reduction.retry_budget must be >= 0, not {retry_budget}: a "
            f"negative budget silently behaves as 0 (no re-entry at all)"
        )

    return CornerReduction(
        enabled=get_bool("enabled", True),
        retry_budget=retry_budget,
        probe=get_bool("probe", True),
    )


def _reject_name_collisions(testbenches: list[Testbench]) -> None:
    """이름으로 색인된 슬롯 하나에 두 값이 들어가는 스펙을 거부한다.

    판정 경로에 last-wins 병합이 두 군데 있다:

      * `pvt.run_full_pvt_sweep`의 `combined_worst_corners`는 **criterion
        이름**으로 색인된 dict를 update로 채운다. 같은 이름을 두 테스트벤치가
        쓰면 뒤에 온 쪽의 최악 코너가 앞의 것을 덮어써, 앞 테스트벤치가
        실제로 위반하는데도 overall_pass가 True로 나온다.
      * `cli.simulate_fn`의 `merged_measurements`는 **measurement 이름**으로
        색인된 dict를 update로 채운다. 두 테스트벤치가 같은 이름의 측정값을
        내면 앞의 값이 버려지고, judge는 두 회로의 기준을 한 회로의 값으로
        판정한다.

    orchestrator.py는 초점(relevance 전용) 경로에서 정확히 이 이유로 이미
    합집합 병합으로 고쳐졌다 - 판정 경로는 덮어쓰기로 남아 있다. 소비자
    두 곳이 다른 에이전트 소유라 여기서 계약 위반 입력을 로더가 거부한다:
    `_load_corner_reduction`의 음수 예산과 같은 규율이다.

    **measurement 이름을 한 테스트벤치 *안에서* 나눠 쓰는 것은 정상이고
    막지 않는다.** 두 기준이 한 측정값의 양쪽을 보는 two-sided window
    (`vbgout_min`/`vbgout_max`)가 출하 스펙에 실제로 있고, 그 충돌은
    corner_sim이 "위반하는 쪽을 이긴 값으로 고른다"로 이미 해소한다.
    여기서 거부하는 것은 **테스트벤치 경계를 넘는** 공유뿐이다."""
    seen_criteria: dict[str, str] = {}
    seen_measurements: dict[str, str] = {}
    for tb in testbenches:
        for criterion in tb.criteria:
            owner = seen_criteria.get(criterion.name)
            if owner is not None:
                raise ValueError(
                    f"criterion name {criterion.name!r} is declared by both testbench "
                    f"{owner!r} and {tb.name!r}: the PVT sweep indexes worst corners by "
                    f"criterion name, so one verdict would silently overwrite the other"
                )
            seen_criteria[criterion.name] = tb.name
        for criterion in tb.criteria:
            owner = seen_measurements.setdefault(criterion.measurement, tb.name)
            if owner != tb.name:
                raise ValueError(
                    f"measurement name {criterion.measurement!r} is produced by both "
                    f"testbench {owner!r} and {tb.name!r}: per-testbench measurements are "
                    f"merged into one name-keyed dict, so one value would be dropped"
                )


def load_spec(path: str) -> TargetSpec:
    with open(path) as f:
        raw = yaml.safe_load(f)

    spec_dir = os.path.dirname(os.path.abspath(path))
    testbenches = [
        Testbench(
            name=tb["name"],
            netlist_path=os.path.join(spec_dir, tb["netlist"]),
            analyses=tb["analyses"],
            control_block=tb["control_block"],
            criteria=_load_criteria(tb["criteria"]),
        )
        for tb in raw["testbenches"]
    ]
    _reject_name_collisions(testbenches)

    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches, pvt_corners=_load_pvt_corners(raw), optimize=_load_optimize(raw), corner_reduction=_load_corner_reduction(raw))
