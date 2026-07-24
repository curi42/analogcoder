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
class TargetSpec:
    circuit_name: str
    analyses: list[str]
    control_block: str
    criteria: list[Criterion]


def load_spec(path: str) -> TargetSpec:
    with open(path) as f:
        raw = yaml.safe_load(f)

    criteria = [
        Criterion(
            name=c["name"],
            measurement=c["measurement"],
            operator=c["operator"],
            threshold=float(c["threshold"]),
            unit=c.get("unit"),
        )
        for c in raw["criteria"]
    ]

    return TargetSpec(
        circuit_name=raw["circuit_name"],
        analyses=raw["analyses"],
        control_block=raw["control_block"],
        criteria=criteria,
    )
