import os
from dataclasses import dataclass, field

from analogcoder.json_io import dumps as json_dumps


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
        # Validate: ensure all testbenches are present in texts dict.
        # This must happen before any file writes or state mutations.
        text_keys = set(texts.keys())
        expected_keys = set(self.testbench_names)
        if text_keys != expected_keys:
            missing = sorted(expected_keys - text_keys)
            extra = sorted(text_keys - expected_keys)
            raise ValueError(
                f"texts keys {sorted(text_keys)} do not match testbench_names "
                f"{sorted(self.testbench_names)}; missing: {missing}, extra: {extra}"
            )

        # Compute the version number before any writes.
        version = len(self.netlist_versions.get(self.testbench_names[0], []))

        # Write phase: write all files and collect paths.
        # File writes are the only mutation in this phase; self.netlist_versions is untouched.
        paths = {}
        for name in self.testbench_names:
            path = os.path.join(self.run_dir, f"netlist_v{version}_{name}.cir")
            with open(path, "w") as f:
                f.write(texts[name])
            paths[name] = path

        # Update phase: only after all writes succeed, update self.netlist_versions.
        for name in self.testbench_names:
            self.netlist_versions.setdefault(name, []).append(paths[name])

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
        # `json_io.dumps`는 비유한 float를 문자열 표지로 정규화한 뒤
        # `allow_nan=False`로 쓴다. 그 이유 전부는 `json_io`의 모듈 독스트링에
        # 있다 - 요지는 bare `NaN`이 RFC 8259가 아니고, jq가 그것을 거부하는
        # 대신 `-1.797e308`로 **조용히 바꿔 준다**는 것이다.
        # 되읽는 쪽은 `history.read_events`가 `restore_non_finite`로 되돌린다.
        with open(self.history_path, "a") as f:
            f.write(json_dumps({"step": step, **data}) + "\n")
