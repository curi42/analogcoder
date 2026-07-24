import json
import os
from dataclasses import dataclass, field


@dataclass
class RunState:
    run_dir: str
    netlist_versions: list[str] = field(default_factory=list)
    history_path: str = field(init=False)

    def __post_init__(self):
        os.makedirs(self.run_dir, exist_ok=True)
        self.history_path = os.path.join(self.run_dir, "history.jsonl")

    def push_netlist_version(self, text: str) -> str:
        version = len(self.netlist_versions)
        path = os.path.join(self.run_dir, f"netlist_v{version}.cir")
        with open(path, "w") as f:
            f.write(text)
        self.netlist_versions.append(path)
        return path

    def current_netlist_path(self) -> str:
        return self.netlist_versions[-1]

    def rollback(self) -> str:
        if len(self.netlist_versions) < 2:
            raise ValueError("no previous netlist version to roll back to")
        self.netlist_versions.pop()
        return self.netlist_versions[-1]

    def log_event(self, step: str, data: dict) -> None:
        with open(self.history_path, "a") as f:
            f.write(json.dumps({"step": step, **data}) + "\n")
