import json
import os
from dataclasses import dataclass, field


@dataclass
class RunState:
    run_dir: str
    testbench_names: list[str] = field(default_factory=list)
    netlist_versions: dict[str, list[str]] = field(default_factory=dict)
    history_path: str = field(init=False)

    def __post_init__(self):
        os.makedirs(self.run_dir, exist_ok=True)
        self.history_path = os.path.join(self.run_dir, "history.jsonl")

    def push_netlist_version(self, texts: dict[str, str]) -> dict[str, str]:
        version = len(self.netlist_versions.get(self.testbench_names[0], []))
        paths = {}
        for name in self.testbench_names:
            path = os.path.join(self.run_dir, f"netlist_v{version}_{name}.cir")
            with open(path, "w") as f:
                f.write(texts[name])
            self.netlist_versions.setdefault(name, []).append(path)
            paths[name] = path
        return paths

    def current_netlist_paths(self) -> dict[str, str]:
        return {name: paths[-1] for name, paths in self.netlist_versions.items()}

    def current_netlist_texts(self) -> dict[str, str]:
        texts = {}
        for name, path in self.current_netlist_paths().items():
            with open(path) as f:
                texts[name] = f.read()
        return texts

    def rollback(self) -> dict[str, str]:
        for name in self.testbench_names:
            if len(self.netlist_versions[name]) < 2:
                raise ValueError("no previous netlist version to roll back to")
        for name in self.testbench_names:
            self.netlist_versions[name].pop()
        return self.current_netlist_paths()

    def log_event(self, step: str, data: dict) -> None:
        with open(self.history_path, "a") as f:
            f.write(json.dumps({"step": step, **data}) + "\n")
