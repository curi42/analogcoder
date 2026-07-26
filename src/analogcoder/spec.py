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

    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches, pvt_corners=_load_pvt_corners(raw))
