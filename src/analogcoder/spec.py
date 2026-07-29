import os
from dataclasses import dataclass

import yaml


@dataclass
class Criterion:
    name: str
    measurement: str
    operator: str
    threshold: float
    unit: str | None = None


@dataclass
class PVTCorners:
    process: list[str]
    voltage: list[float]
    temperature: list[float]


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

    return PVTCorners(
        process=process,
        voltage=numbers("voltage"),
        temperature=numbers("temperature"),
    )


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
