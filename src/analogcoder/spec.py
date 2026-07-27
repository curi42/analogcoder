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


def _load_pvt_corners(raw: dict) -> PVTCorners | None:
    raw_pvt = raw.get("pvt_corners")
    if raw_pvt is None:
        return None
    return PVTCorners(
        process=raw_pvt["process"],
        voltage=[float(v) for v in raw_pvt["voltage"]],
        temperature=[float(t) for t in raw_pvt["temperature"]],
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

    return CornerReduction(
        enabled=get_bool("enabled", True),
        retry_budget=int(block.get("retry_budget", 2)),
        probe=get_bool("probe", True),
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

    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches, pvt_corners=_load_pvt_corners(raw), optimize=_load_optimize(raw), corner_reduction=_load_corner_reduction(raw))
