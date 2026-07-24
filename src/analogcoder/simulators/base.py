from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RawSimResult:
    status: str  # "success" | "convergence_failure" | "error"
    measurements: dict[str, float]
    raw_log: str
    warnings: list[str] = field(default_factory=list)


class SimulatorBackend(ABC):
    @abstractmethod
    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        ...
