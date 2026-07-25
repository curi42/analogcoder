# PSR Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PSR+ / PSR- as new testbenches verified alongside the existing AC loop-gain testbench, with every criterion across all testbenches re-checked on every tuning iteration so a fix for one testbench can never silently regress another.

**Architecture:** `spec.yaml` becomes a list of testbenches (each with its own netlist file, control block, and criteria). `judge`/`tune`/`verify_pre`/`verify_post` stay at one LLM call per iteration by aggregating measurements/criteria across testbenches; `simulate` is called once per testbench. Netlist versioning in `RunState` becomes per-testbench but always advances in lockstep. Full netlist duplication per testbench (no `.include`), relying on the invariant that the shared `OPAMP2STAGE` subckt body stays byte-identical across all three `two_stage_opamp` testbench files.

**Tech Stack:** Python 3, pytest, pytest-asyncio, PyYAML, ngspice (real binary, no mocking in the ngspice-backend tests).

## Global Constraints

- No dual old/new `spec.yaml` format support — every existing spec migrates to the `testbenches:` list format, even ones with exactly one testbench.
- The **canonical testbench** for any operation that needs exactly one netlist text (tuning proposals, `verify_pre`, area-growth baseline indexing) is always `spec.testbenches[0]`.
- `judge`/`tune`/`verify_pre`/`verify_post` remain one LLM call per iteration (aggregate across testbenches before calling); `simulate` is called once per testbench.
- PSR+ threshold: `psr_plus <= -10.0 dB`. PSR- threshold: `psr_minus <= -8.0 dB`. These are validated values from the design spec's Validation section — do not change them without re-running the ngspice sweep.
- `benchmarks/two_stage_opamp/netlist.cir`'s `OPAMP2STAGE` subckt body must stay byte-identical to the one in `netlist_psr_plus.cir` and `netlist_psr_minus.cir` at every point — this is what lets tuning changes apply uniformly.

---

## Task 1: Multi-testbench `spec.py` schema + migrate benchmark YAML files

**Files:**
- Modify: `src/analogcoder/spec.py`
- Modify: `tests/unit/test_spec.py`
- Modify: `tests/unit/test_ngspice_backend.py` (1 line)
- Modify: `tests/unit/test_topology_swap_ngspice.py` (1 line)
- Modify: `benchmarks/inverting_amp/spec.yaml`
- Modify: `benchmarks/two_stage_opamp/spec.yaml`
- Modify: `benchmarks/two_stage_opamp/spec_topology_required.yaml`

**Interfaces:**
- Produces: `Criterion` (unchanged shape: `name, measurement, operator, threshold, unit`), `Testbench(name: str, netlist_path: str, analyses: list[str], control_block: str, criteria: list[Criterion])`, `TargetSpec(circuit_name: str, testbenches: list[Testbench])` with properties `.canonical -> Testbench` (returns `testbenches[0]`) and `.all_criteria -> list[Criterion]` (flattens every testbench's criteria in order). `load_spec(path: str) -> TargetSpec` resolves each testbench's `netlist:` path relative to the spec file's own directory. Consumed by Task 3 (orchestrator), Task 4 (cli).

- [ ] **Step 1: Migrate the three benchmark spec.yaml files to the `testbenches:` list format**

Replace the full content of `benchmarks/inverting_amp/spec.yaml`:

```yaml
circuit_name: inverting_amplifier
testbenches:
  - name: ac_loop_gain
    netlist: netlist.cir
    analyses: ["ac"]
    control_block: |
      .control
      ac dec 10 1 1meg
      meas ac gain_db find vdb(vout) at=1k
      .endc
    criteria:
      - name: closed_loop_gain
        measurement: gain_db
        operator: ">="
        threshold: 19.5
        unit: dB
```

Replace the full content of `benchmarks/two_stage_opamp/spec.yaml`:

```yaml
circuit_name: two_stage_opamp
testbenches:
  - name: ac_loop_gain
    netlist: netlist.cir
    analyses: ["ac"]
    control_block: |
      .control
      set units=degrees
      ac dec 20 1 100meg
      meas ac gain_db find vdb(vout) at=1
      meas ac ugbw_hz when vdb(vout)=0
      meas ac phase_margin_deg find vp(vout) when vdb(vout)=0
      .endc
    criteria:
      - name: dc_gain
        measurement: gain_db
        operator: ">="
        threshold: 70.0
        unit: dB
      - name: unity_gain_bandwidth
        measurement: ugbw_hz
        operator: ">="
        threshold: 20000000.0
        unit: Hz
      - name: phase_margin
        measurement: phase_margin_deg
        operator: ">="
        threshold: 60.0
        unit: deg
```

(`netlist_psr_plus.cir`/`netlist_psr_minus.cir` and their `testbenches` entries are added in Task 5 — this task only migrates the existing single testbench so nothing breaks in between.)

Replace the full content of `benchmarks/two_stage_opamp/spec_topology_required.yaml`:

```yaml
circuit_name: two_stage_opamp
testbenches:
  - name: ac_loop_gain
    netlist: netlist.cir
    analyses: ["ac"]
    control_block: |
      .control
      set units=degrees
      ac dec 20 1 100meg
      meas ac gain_db find vdb(vout) at=1
      meas ac ugbw_hz when vdb(vout)=0
      meas ac phase_margin_deg find vp(vout) when vdb(vout)=0
      .endc
    criteria:
      - name: dc_gain
        measurement: gain_db
        operator: ">="
        threshold: 70.0
        unit: dB
      - name: unity_gain_bandwidth
        measurement: ugbw_hz
        operator: ">="
        threshold: 20000000.0
        unit: Hz
      - name: phase_margin
        measurement: phase_margin_deg
        operator: ">="
        threshold: 65.0
        unit: deg
```

- [ ] **Step 2: Write the failing tests for the new `spec.py` shape**

Replace the full content of `tests/unit/test_spec.py`:

```python
import textwrap

from analogcoder.spec import load_spec

SPEC_YAML = textwrap.dedent("""\
    circuit_name: inverting_amplifier
    testbenches:
      - name: ac_loop_gain
        netlist: netlist.cir
        analyses: ["ac"]
        control_block: |
          .control
          ac dec 10 1 1meg
          meas ac gain_db find vdb(vout) at=1k
          .endc
        criteria:
          - name: closed_loop_gain
            measurement: gain_db
            operator: ">="
            threshold: 19.5
            unit: dB
    """)


def test_load_spec(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")

    spec = load_spec(str(spec_path))

    assert spec.circuit_name == "inverting_amplifier"
    assert len(spec.testbenches) == 1
    tb = spec.testbenches[0]
    assert tb.name == "ac_loop_gain"
    assert tb.netlist_path == str(tmp_path / "netlist.cir")
    assert tb.analyses == ["ac"]
    assert "meas ac gain_db" in tb.control_block
    assert len(tb.criteria) == 1
    c = tb.criteria[0]
    assert c.name == "closed_loop_gain"
    assert c.measurement == "gain_db"
    assert c.operator == ">="
    assert c.threshold == 19.5
    assert c.unit == "dB"


def test_canonical_returns_first_testbench(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")

    spec = load_spec(str(spec_path))

    assert spec.canonical is spec.testbenches[0]
    assert spec.canonical.name == "ac_loop_gain"


def test_all_criteria_flattens_across_testbenches(tmp_path):
    multi_yaml = textwrap.dedent("""\
        circuit_name: two_stage_opamp
        testbenches:
          - name: ac_loop_gain
            netlist: a.cir
            analyses: ["ac"]
            control_block: ".control\\n.endc\\n"
            criteria:
              - name: dc_gain
                measurement: gain_db
                operator: ">="
                threshold: 70.0
                unit: dB
          - name: psr_plus
            netlist: b.cir
            analyses: ["ac"]
            control_block: ".control\\n.endc\\n"
            criteria:
              - name: psr_plus
                measurement: psr_plus_db
                operator: "<="
                threshold: -10.0
                unit: dB
        """)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(multi_yaml)

    spec = load_spec(str(spec_path))

    assert [c.name for c in spec.all_criteria] == ["dc_gain", "psr_plus"]


def test_netlist_path_resolved_relative_to_spec_directory(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    spec_path = nested / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    spec = load_spec(str(spec_path))

    assert spec.testbenches[0].netlist_path == str(nested / "netlist.cir")


def test_topology_required_spec_has_stricter_phase_margin_threshold():
    spec = load_spec("benchmarks/two_stage_opamp/spec_topology_required.yaml")
    baseline = load_spec("benchmarks/two_stage_opamp/spec.yaml")

    phase_margin = next(c for c in spec.canonical.criteria if c.name == "phase_margin")
    baseline_phase_margin = next(c for c in baseline.canonical.criteria if c.name == "phase_margin")

    assert phase_margin.threshold == 65.0
    assert phase_margin.threshold > baseline_phase_margin.threshold
    # gain and UGBW thresholds are unchanged from the baseline spec
    assert {c.name: c.threshold for c in spec.canonical.criteria if c.name != "phase_margin"} == {
        c.name: c.threshold for c in baseline.canonical.criteria if c.name != "phase_margin"
    }
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: FAIL — `TargetSpec`/`load_spec` still return the old flat shape (no `.testbenches`, no `.canonical`), and the benchmark YAML files (if Step 1 hasn't been applied yet in your working copy) don't have a `testbenches:` key yet, or `load_spec` doesn't understand it.

- [ ] **Step 4: Rewrite `spec.py`**

Replace the full content of `src/analogcoder/spec.py`:

```python
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

    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Fix the two other tests that read `spec.control_block` directly**

In `tests/unit/test_ngspice_backend.py`, line 14, change:

```python
    result = backend.run(netlist_path, {"control_block": spec.control_block})
```

to:

```python
    result = backend.run(netlist_path, {"control_block": spec.canonical.control_block})
```

In `tests/unit/test_topology_swap_ngspice.py`, line 26, change:

```python
    return backend.run(str(swapped_path), {"control_block": spec.control_block})
```

to:

```python
    return backend.run(str(swapped_path), {"control_block": spec.canonical.control_block})
```

- [ ] **Step 7: Run the full unit suite to confirm nothing else broke**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: All tests pass except any in `test_cli.py`, `test_orchestrator.py`, `test_state.py`, `test_report.py` — those are expected to still be broken/stale until Tasks 2-4 land (they exercise the old single-testbench orchestrator/state/cli API, which Task 1 does not touch). Confirm the failures you see are confined to those four files and `test_spec.py`/`test_ngspice_backend.py`/`test_topology_swap_ngspice.py` are all green.

- [ ] **Step 8: Commit**

```bash
git add src/analogcoder/spec.py tests/unit/test_spec.py tests/unit/test_ngspice_backend.py tests/unit/test_topology_swap_ngspice.py benchmarks/inverting_amp/spec.yaml benchmarks/two_stage_opamp/spec.yaml benchmarks/two_stage_opamp/spec_topology_required.yaml
git commit -m "feat: migrate spec.yaml to a multi-testbench schema"
```

---

## Task 2: Multi-testbench `RunState`

**Files:**
- Modify: `src/analogcoder/state.py`
- Modify: `tests/unit/test_state.py`

**Interfaces:**
- Consumes: nothing from Task 1 (this task's `RunState` is testbench-name-agnostic — it takes plain `str` names, not `Testbench` objects).
- Produces: `RunState(run_dir: str, testbench_names: list[str])` with `push_netlist_version(texts: dict[str, str]) -> dict[str, str]`, `current_netlist_paths() -> dict[str, str]`, `current_netlist_texts() -> dict[str, str]`, `rollback() -> dict[str, str]`, `log_event(step: str, data: dict) -> None`. Consumed by Task 3 (orchestrator) and Task 4 (cli).

- [ ] **Step 1: Write the failing tests**

Replace the full content of `tests/unit/test_state.py`:

```python
import json
import os

import pytest

from analogcoder.state import RunState


def test_push_netlist_version_writes_one_file_per_testbench(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    v0_paths = state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    v1_paths = state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    assert os.path.basename(v0_paths["ac_loop_gain"]) == "netlist_v0_ac_loop_gain.cir"
    assert os.path.basename(v0_paths["psr_plus"]) == "netlist_v0_psr_plus.cir"
    assert os.path.basename(v1_paths["ac_loop_gain"]) == "netlist_v1_ac_loop_gain.cir"
    assert state.current_netlist_paths() == v1_paths
    with open(v1_paths["psr_plus"]) as f:
        assert f.read() == "* psr v1\n.end\n"


def test_current_netlist_texts_reads_back_latest_version(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])
    state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    texts = state.current_netlist_texts()

    assert texts == {"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"}


def test_rollback_restores_every_testbench_together(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])
    v0_paths = state.push_netlist_version({"ac_loop_gain": "* ac v0\n.end\n", "psr_plus": "* psr v0\n.end\n"})
    state.push_netlist_version({"ac_loop_gain": "* ac v1\n.end\n", "psr_plus": "* psr v1\n.end\n"})

    restored_paths = state.rollback()

    assert restored_paths == v0_paths
    assert state.current_netlist_paths() == v0_paths


def test_rollback_raises_when_no_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.push_netlist_version({"ac_loop_gain": "* v0\n.end\n"})

    with pytest.raises(ValueError):
        state.rollback()


def test_log_event_appends_jsonl(tmp_path):
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])
    state.log_event("judge", {"overall_pass": False})
    state.log_event("judge", {"overall_pass": True})

    with open(state.history_path) as f:
        lines = [json.loads(line) for line in f]

    assert lines[0] == {"step": "judge", "overall_pass": False}
    assert lines[1] == {"step": "judge", "overall_pass": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_state.py -v`
Expected: FAIL — `RunState` doesn't accept `testbench_names`, `push_netlist_version` doesn't accept a dict.

- [ ] **Step 3: Rewrite `state.py`**

Replace the full content of `src/analogcoder/state.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_state.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/state.py tests/unit/test_state.py
git commit -m "feat: version netlists per-testbench in RunState, in lockstep"
```

---

## Task 3: Multi-testbench `orchestrator.py`

**Files:**
- Modify: `src/analogcoder/orchestrator.py`
- Modify: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `RunState` from Task 2 (`push_netlist_version(dict) -> dict`, `current_netlist_paths() -> dict`, `current_netlist_texts() -> dict`, `rollback() -> dict`). Does NOT depend on the real `TargetSpec`/`Testbench` classes from Task 1 — only duck-types `spec.canonical.name` (a plain attribute access), same as the existing code duck-types `spec.criteria`.
- Produces: `async run_orchestration(initial_netlist_texts: dict[str, str], spec, state: RunState, agents: OrchestratorAgents) -> dict` returning `{"status": "PASS"|"FAIL", "final_netlist_paths": dict[str, str], "run_dir": str, "iterations_used": int, "final_criteria": list, "failure_reason"?: str}`. `agents.simulate(netlist_texts: dict[str, str], spec) -> dict` and `agents.judge(measurements: dict, spec) -> dict` keep their existing shapes (still called once per iteration) — only what's passed to `simulate` changes, from a single string to a dict. `agents.tune`/`agents.verify_pre` still take a single `netlist_text: str` (now always `netlist_texts[spec.canonical.name]`). Consumed by Task 4 (cli).

- [ ] **Step 1: Write the failing tests**

Replace the full content of `tests/unit/test_orchestrator.py`:

```python
# tests/unit/test_orchestrator.py
from types import SimpleNamespace

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState

PASS_JUDGE = {"overall_pass": True, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}]}
FAIL_JUDGE = {"overall_pass": False, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 18.0, "pass": False, "margin": -1.5}]}


def make_spec(*testbench_names):
    testbenches = [SimpleNamespace(name=n, criteria=[]) for n in testbench_names]
    return SimpleNamespace(testbenches=testbenches, canonical=testbenches[0])


FAKE_SPEC = make_spec("ac_loop_gain")
MULTI_SPEC = make_spec("ac_loop_gain", "psr_plus")
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}
SUBCKT_NETLIST = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "R1 vinp mid 1k\n"
    "R2 mid vout 2k\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)
FAKE_TOPOLOGY_PROPOSAL = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
AREA_TEST_NETLIST = (
    "* test\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".end\n"
)
AREA_TEST_NETLIST_WITH_SUBCKT = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)


def make_agents(**overrides):
    async def default_analyze(netlist_text):
        return {"circuit_type": "inverting amplifier"}

    async def default_simulate(netlist_texts, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return FAKE_PROPOSAL

    async def default_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    async def default_propose_topology(analysis, judge_result, available_topologies, rejection_feedback):
        return FAKE_TOPOLOGY_PROPOSAL

    defaults = dict(
        analyze=default_analyze,
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
        propose_topology=default_propose_topology,
    )
    defaults.update(overrides)
    return OrchestratorAgents(**defaults)


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_immediate_pass_returns_pass_on_first_iteration(tmp_path):
    agents = make_agents()
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert result["final_netlist_paths"] == state.current_netlist_paths()
    assert result["run_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_fail_then_pass_after_tuning(tmp_path):
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert len(state.netlist_versions["ac_loop_gain"]) == 2  # v0 initial + v1 after applied tuning


@pytest.mark.asyncio
async def test_prereview_always_rejected_fails_run(tmp_path):
    async def always_reject(analysis, judge_result, proposal, netlist_text):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_pre=always_reject)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_postreview_rollback_consumes_an_iteration_then_succeeds(tmp_path):
    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        # iter1 pre: FAIL, iter1 post (rolled back): FAIL, iter2 pre: FAIL, iter2 post: PASS
        return [FAIL_JUDGE, FAIL_JUDGE, FAIL_JUDGE, PASS_JUDGE][judge_calls["count"] - 1]

    verify_post_calls = {"count": 0}

    async def verify_post_first_rollback(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        if verify_post_calls["count"] == 1:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "worse"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "better"}

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_first_rollback)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 2


@pytest.mark.asyncio
async def test_max_iterations_exhausted_fails_run(tmp_path):
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_post=lambda p, n, c: _async(
        {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no progress"}
    ))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_agent_execution_error_before_loop_returns_fail_with_zero_iterations(tmp_path):
    async def failing_analyze(netlist_text):
        raise AgentExecutionError("boom")

    agents = make_agents(analyze=failing_analyze)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["final_criteria"] == []
    assert result["failure_reason"] == "agent execution error: boom"


@pytest.mark.asyncio
async def test_agent_execution_error_mid_loop_reports_last_completed_iteration(tmp_path):
    call_count = {"n": 0}

    async def simulate_then_fail(netlist_texts, spec):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise AgentExecutionError("simulator backend unreachable")
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    agents = make_agents(simulate=simulate_then_fail, judge=lambda m, s: _async(FAIL_JUDGE))
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["iterations_used"] == 0
    assert result["failure_reason"] == "agent execution error: simulator backend unreachable"


@pytest.mark.asyncio
async def test_topology_swap_never_offered_without_exactly_one_subckt(tmp_path):
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return FAKE_TOPOLOGY_PROPOSAL

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": "* netlist\n.end\n"}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 0


@pytest.mark.asyncio
async def test_topology_swap_triggers_after_threshold_consecutive_rollbacks(tmp_path):
    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        return PASS_JUDGE if judge_calls["count"] == 8 else FAIL_JUDGE

    verify_post_calls = {"count": 0}

    async def verify_post_sequence(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        if verify_post_calls["count"] <= 3:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "fixed"}

    propose_topology_calls = []

    async def propose_topology(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls.append([t.id for t in available_topologies])
        return {"topology_id": available_topologies[0].id, "reasoning": "matches phase margin gap", "confidence": 90}

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 4
    assert len(propose_topology_calls) == 1
    assert set(propose_topology_calls[0]) == {"miller_basic", "miller_nulling_resistor"}
    assert len(state.netlist_versions["ac_loop_gain"]) == 2  # v0 initial + v1 after the kept topology swap


@pytest.mark.asyncio
async def test_topology_swap_repeatedly_invalid_id_fails_run(tmp_path):
    async def always_bad_topology(analysis, judge_result, available_topologies, rejection_feedback):
        return {"topology_id": "not_a_real_topology", "reasoning": "x", "confidence": 50}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=always_bad_topology,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "topology proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_topology_swap_rollback_restores_analysis_without_reanalyzing(tmp_path):
    analyze_calls = {"count": 0}

    async def counting_analyze(netlist_text):
        analyze_calls["count"] += 1
        return {"circuit_type": "amp", "call": analyze_calls["count"]}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_once(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        analyze=counting_analyze,
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_once,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": SUBCKT_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert analyze_calls["count"] == 3
    assert propose_topology_calls["count"] == 2


@pytest.mark.asyncio
async def test_area_check_rejects_without_calling_verify_pre(tmp_path):
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(analysis, judge_result, proposal, netlist_text):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def oversized_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        verify_pre=counting_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST}, FAKE_SPEC, state, agents)

    assert verify_pre_calls["count"] == 0
    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_area_check_mixed_with_verify_pre_rejection_hard_fails(tmp_path):
    call_count = {"n": 0}

    async def mixed_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        call_count["n"] += 1
        if call_count["n"] % 2 == 1:
            new_value = "100u"  # oversized -> area-rejected, 2.5x
        else:
            new_value = "50u"  # right-sized, 1.25x -> passes area, reaches verify_pre
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": new_value, "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    async def always_reject_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=mixed_tune,
        verify_pre=always_reject_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    result = await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST}, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_area_rejection_eventually_triggers_topology_swap(tmp_path):
    async def oversized_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain"])

    await run_orchestration({"ac_loop_gain": AREA_TEST_NETLIST_WITH_SUBCKT}, FAKE_SPEC, state, agents)

    assert propose_topology_calls["count"] >= 1


@pytest.mark.asyncio
async def test_multi_testbench_tuning_change_applied_to_every_testbench(tmp_path):
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    initial = {
        "ac_loop_gain": "* ac\nRf vminus vout 10k\n.end\n",
        "psr_plus": "* psr\nRf vminus vout 10k\n.end\n",
    }
    result = await run_orchestration(initial, MULTI_SPEC, state, agents)

    assert result["status"] == "PASS"
    final_texts = state.current_netlist_texts()
    assert "11k" in final_texts["ac_loop_gain"]
    assert "11k" in final_texts["psr_plus"]


@pytest.mark.asyncio
async def test_multi_testbench_tune_and_verify_pre_receive_only_canonical_text(tmp_path):
    seen_texts = {"tune": None, "verify_pre": None}

    async def spying_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        seen_texts["tune"] = netlist_text
        return FAKE_PROPOSAL

    async def spying_verify_pre(analysis, judge_result, proposal, netlist_text):
        seen_texts["verify_pre"] = netlist_text
        return {"approved": True, "concerns": [], "feedback": "ok"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE) if seen_texts["tune"] is None else _async(PASS_JUDGE),
        tune=spying_tune,
        verify_pre=spying_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    initial = {"ac_loop_gain": "* canonical text\n.end\n", "psr_plus": "* other testbench text\n.end\n"}
    await run_orchestration(initial, MULTI_SPEC, state, agents)

    assert seen_texts["tune"] == "* canonical text\n.end\n"
    assert seen_texts["verify_pre"] == "* canonical text\n.end\n"


@pytest.mark.asyncio
async def test_multi_testbench_rollback_restores_every_testbench(tmp_path):
    verify_post_calls = {"count": 0}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_post=verify_post_always_rollback)
    state = RunState(run_dir=str(tmp_path), testbench_names=["ac_loop_gain", "psr_plus"])

    initial = {
        "ac_loop_gain": "* ac original\nRf vminus vout 10k\n.end\n",
        "psr_plus": "* psr original\nRf vminus vout 10k\n.end\n",
    }
    await run_orchestration(initial, MULTI_SPEC, state, agents)

    assert verify_post_calls["count"] >= 1
    final_texts = state.current_netlist_texts()
    assert final_texts["ac_loop_gain"] == "* ac original\nRf vminus vout 10k\n.end\n"
    assert final_texts["psr_plus"] == "* psr original\nRf vminus vout 10k\n.end\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: FAIL — `run_orchestration` still takes a single netlist string, `RunState` calls like `push_netlist_version(dict)` don't match the old single-text API.

- [ ] **Step 3: Rewrite `orchestrator.py`**

Replace the full content of `src/analogcoder/orchestrator.py`:

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
from analogcoder.state import RunState
from analogcoder.topologies import TOPOLOGY_LIBRARY

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3
TOPOLOGY_SWITCH_THRESHOLD = 3


@dataclass
class OrchestratorAgents:
    analyze: Callable
    simulate: Callable
    judge: Callable
    tune: Callable
    verify_pre: Callable
    verify_post: Callable
    propose_topology: Callable


def _final_result(
    status: str, state: RunState, iterations_used: int, judge_result: dict | None, failure_reason: str | None = None
) -> dict:
    result = {
        "status": status,
        "final_netlist_paths": state.current_netlist_paths(),
        "run_dir": state.run_dir,
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"] if judge_result else [],
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


def _apply_to_all(netlist_texts: dict[str, str], changes: list[dict]) -> dict[str, str]:
    return {name: apply_changes(text, changes) for name, text in netlist_texts.items()}


async def run_orchestration(
    initial_netlist_texts: dict[str, str], spec, state: RunState, agents: OrchestratorAgents
) -> dict:
    canonical_name = spec.canonical.name
    state.push_netlist_version(initial_netlist_texts)
    outer_iter = 0
    judge_result: dict = {}

    try:
        analysis = await agents.analyze(initial_netlist_texts[canonical_name])
        state.log_event("analysis", analysis)

        topology_swap_available = len(parse_netlist(initial_netlist_texts[canonical_name]).subckts) == 1
        tried_topologies: set[str] = set()
        consecutive_rollbacks = 0
        # Intentionally computed once from netlist_v0 and never refreshed after a
        # topology swap: components introduced by a swapped-in topology (e.g. a
        # nulling resistor Rz) have nothing in the original netlist to compare
        # against, so they are simply unconstrained by the area gate for the
        # rest of the run. This is by-design, not a bug - do not "fix" it.
        baseline_components = index_baseline_components(initial_netlist_texts[canonical_name])

        tuning_history: list[dict] = []

        for outer_iter in range(1, MAX_OUTER_ITERATIONS + 1):
            netlist_texts = state.current_netlist_texts()

            sim_result = await agents.simulate(netlist_texts, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

            judge_result = await agents.judge(sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, **judge_result})

            if judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, judge_result)

            untried_topologies = (
                [t for t in TOPOLOGY_LIBRARY.values() if t.id not in tried_topologies]
                if topology_swap_available and consecutive_rollbacks >= TOPOLOGY_SWITCH_THRESHOLD
                else []
            )

            if untried_topologies:
                topology_id = None
                rejection_feedback = None
                for retry in range(1, MAX_TUNING_RETRIES + 1):
                    proposal = await agents.propose_topology(
                        analysis, judge_result, untried_topologies, rejection_feedback
                    )
                    state.log_event("topology_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                    candidate = proposal["topology_id"]
                    if candidate in TOPOLOGY_LIBRARY and candidate not in tried_topologies:
                        topology_id = candidate
                        break
                    rejection_feedback = (
                        f"'{candidate}' is not an available untried topology. "
                        f"Choose one of: {[t.id for t in untried_topologies]}"
                    )

                if topology_id is None:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="topology proposal repeatedly rejected",
                    )

                tried_topologies.add(topology_id)
                topology = TOPOLOGY_LIBRARY[topology_id]
                subckt_name = next(iter(parse_netlist(netlist_texts[canonical_name]).subckts))
                # Replaces the whole subckt body with the library's fixed defaults, so any
                # parameter-tuning changes made earlier in the run (before the rollback streak
                # that triggered this swap) are silently discarded, not carried forward. This is
                # intentional: the new topology's own defaults are what was verified to work.
                new_netlist_texts = {
                    name: apply_topology_swap(text, subckt_name, topology.subckt_body)
                    for name, text in netlist_texts.items()
                }
                state.push_netlist_version(new_netlist_texts)

                pre_swap_analysis = analysis
                analysis = await agents.analyze(new_netlist_texts[canonical_name])
                state.log_event("analysis", {"outer_iter": outer_iter, "topology_id": topology_id, **analysis})

                new_sim_result = await agents.simulate(new_netlist_texts, spec)
                state.log_event(
                    "simulation", {"outer_iter": outer_iter, "post_topology_swap": True, **new_sim_result}
                )

                new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
                state.log_event(
                    "judge", {"outer_iter": outer_iter, "post_topology_swap": True, **new_judge_result}
                )

                post_review = await agents.verify_post(
                    judge_result, new_judge_result, [{"topology_id": topology_id}]
                )
                state.log_event("verify_post", {"outer_iter": outer_iter, "topology_swap": True, **post_review})

                consecutive_rollbacks = 0

                if post_review["recommendation"] == "rollback":
                    state.rollback()
                    analysis = pre_swap_analysis
                    judge_result = new_judge_result
                    continue

                if new_judge_result["overall_pass"]:
                    return _final_result("PASS", state, outer_iter, new_judge_result)

                judge_result = new_judge_result
                continue

            approved_proposal = None
            rejection_feedback = None
            verify_pre_rejected_any = False
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                proposal = await agents.tune(
                    analysis, judge_result, tuning_history, rejection_feedback, netlist_texts[canonical_name]
                )
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                area_ok, area_feedback = check_area_growth(baseline_components, proposal["proposed_changes"])
                state.log_event(
                    "area_check",
                    {"outer_iter": outer_iter, "retry": retry, "approved": area_ok, "feedback": area_feedback},
                )
                if not area_ok:
                    rejection_feedback = area_feedback
                    continue

                review = await agents.verify_pre(analysis, judge_result, proposal, netlist_texts[canonical_name])
                state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

                if review["approved"]:
                    approved_proposal = proposal
                    break
                verify_pre_rejected_any = True
                rejection_feedback = review["feedback"]

            if approved_proposal is None:
                if verify_pre_rejected_any:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="tuning proposal repeatedly rejected",
                    )
                consecutive_rollbacks += 1
                continue

            new_netlist_texts = _apply_to_all(netlist_texts, approved_proposal["proposed_changes"])
            state.push_netlist_version(new_netlist_texts)

            new_sim_result = await agents.simulate(new_netlist_texts, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, "post_tuning": True, **new_sim_result})

            new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, "post_tuning": True, **new_judge_result})

            post_review = await agents.verify_post(
                judge_result, new_judge_result, approved_proposal["proposed_changes"]
            )
            state.log_event("verify_post", {"outer_iter": outer_iter, **post_review})

            tuning_history.append({
                "outer_iter": outer_iter,
                "proposal": approved_proposal,
                "recommendation": post_review["recommendation"],
            })

            if post_review["recommendation"] == "rollback":
                state.rollback()
                consecutive_rollbacks += 1
                judge_result = new_judge_result
                continue

            consecutive_rollbacks = 0

            if new_judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, new_judge_result)

            judge_result = new_judge_result

        return _final_result("FAIL", state, MAX_OUTER_ITERATIONS, judge_result, failure_reason="max iterations reached")
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result, failure_reason=f"agent execution error: {exc}"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: PASS (17 tests — the 14 pre-existing plus the 3 new multi-testbench-specific tests)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: drive the orchestrator loop with per-testbench netlist dicts"
```

---

## Task 4: Multi-testbench `cli.py` + `report.py`

**Files:**
- Modify: `src/analogcoder/cli.py`
- Modify: `src/analogcoder/report.py`
- Modify: `tests/unit/test_cli.py`
- Modify: `tests/unit/test_report.py`
- Modify: `tests/integration/test_end_to_end.py`
- Modify: `tests/integration/test_local_llm_backend.py`

**Interfaces:**
- Consumes: `load_spec`/`TargetSpec`/`Testbench` from Task 1, `RunState` from Task 2, `run_orchestration`/`OrchestratorAgents` from Task 3.
- Produces: `build_arg_parser()` (no more `--netlist`), `_run(args) -> dict` wiring a multi-testbench `simulate_fn`/`judge_fn` into `OrchestratorAgents`. `main()` uses `result["run_dir"]` instead of deriving it from a netlist path.

- [ ] **Step 1: Write the failing tests**

Replace the full content of `tests/unit/test_cli.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.cli import _build_agent_backend, _run, build_arg_parser

SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    "    analyses: [\"ac\"]\n"
    "    control_block: |\n"
    "      .control\n"
    "      .endc\n"
    "    criteria: []\n"
)


def test_arg_parser_requires_spec_only():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"
    assert args.agent_backend == "claude"
    assert not hasattr(args, "netlist")


def test_build_agent_backend_returns_claude_backend_by_default():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    backend = _build_agent_backend(args)
    assert isinstance(backend, ClaudeSDKBackend)


def test_build_agent_backend_returns_openai_compatible_backend_when_configured():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--spec", "s.yaml",
            "--agent-backend", "openai-compatible",
            "--llm-base-url", "http://local",
            "--llm-model", "glm-5.2",
        ]
    )
    backend = _build_agent_backend(args)
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url == "http://local"
    assert backend.model == "glm-5.2"
    assert backend.api_key_env == "LOCAL_LLM_API_KEY"


def test_build_agent_backend_raises_when_openai_compatible_missing_config():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-backend", "openai-compatible"])
    with pytest.raises(ValueError):
        _build_agent_backend(args)


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    fake_result = {
        "status": "PASS",
        "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "runs" / "r1" / "netlist_v0_ac_loop_gain.cir")},
        "run_dir": str(tmp_path / "runs" / "r1"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)):
        result = await _run(args)

    assert result == fake_result


@pytest.mark.asyncio
async def test_run_passes_one_netlist_text_per_testbench_to_run_orchestration(tmp_path):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* psr netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
        "  - name: psr_plus\n"
        "    netlist: netlist_psr_plus.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r2")])

    captured = {}

    async def fake_run_orchestration(initial_netlist_texts, spec, state, agents):
        captured["texts"] = initial_netlist_texts
        return {
            "status": "PASS",
            "final_netlist_paths": {},
            "run_dir": str(tmp_path / "runs" / "r2"),
            "iterations_used": 1,
            "final_criteria": [],
        }

    with patch("analogcoder.cli.run_orchestration", new=fake_run_orchestration):
        await _run(args)

    assert captured["texts"] == {"ac_loop_gain": "* ac netlist\n.end\n", "psr_plus": "* psr netlist\n.end\n"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: FAIL — `build_arg_parser` still requires `--netlist`, `_run` still reads a single netlist file.

- [ ] **Step 3: Rewrite `cli.py`**

Replace the full content of `src/analogcoder/cli.py`:

```python
import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.analyzer import analyze_netlist
from analogcoder.agents.backend import AgentBackend
from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.report import write_report_md, write_result_json
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analogcoder")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--simulator", choices=["ngspice"], default="ngspice")
    parser.add_argument("--agent-backend", choices=["claude", "openai-compatible"], default="claude")
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--run-dir", default=None)
    return parser


def _build_agent_backend(args) -> AgentBackend:
    if args.agent_backend == "claude":
        return ClaudeSDKBackend()
    if not args.llm_base_url or not args.llm_model:
        raise ValueError("--llm-base-url and --llm-model are required when --agent-backend=openai-compatible")
    return OpenAICompatibleBackend(base_url=args.llm_base_url, api_key_env="LOCAL_LLM_API_KEY", model=args.llm_model)


async def _run(args) -> dict:
    spec = load_spec(args.spec)
    initial_netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            initial_netlist_texts[tb.name] = f.read()

    run_dir = args.run_dir or os.path.join("runs", uuid.uuid4().hex[:8])
    state = RunState(run_dir=run_dir, testbench_names=[tb.name for tb in spec.testbenches])
    sim_backend = NgspiceBackend()
    agent_backend = _build_agent_backend(args)

    async def simulate_fn(netlist_texts, spec_arg):
        merged_measurements = {}
        by_testbench = {}
        paths = state.current_netlist_paths()
        for tb in spec_arg.testbenches:
            result = await agent_simulate(paths[tb.name], tb.control_block, sim_backend, agent_backend)
            merged_measurements.update(result["measurements"])
            by_testbench[tb.name] = result
        return {"measurements": merged_measurements, "by_testbench": by_testbench}

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.all_criteria, agent_backend)

    async def analyze_fn(netlist_text_arg):
        return await analyze_netlist(netlist_text_arg, agent_backend)

    async def tune_fn(analysis, judge_result, history, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            analysis, judge_result, history, rejection_feedback, netlist_text_arg, agent_backend
        )

    async def verify_pre_fn(analysis, judge_result, proposal, netlist_text_arg):
        return await verify_pre(analysis, judge_result, proposal, netlist_text_arg, agent_backend)

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backend)

    async def propose_topology_fn(analysis, judge_result, available_topologies, rejection_feedback):
        return await propose_topology_swap(
            analysis, judge_result, available_topologies, rejection_feedback, agent_backend
        )

    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=propose_topology_fn,
    )

    return await run_orchestration(initial_netlist_texts, spec, state, agents)


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(_run(args))

    run_dir = result["run_dir"]
    write_result_json(run_dir, result)
    write_report_md(run_dir, result)

    print(f"Status: {result['status']}")
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Write the failing test for `report.py`**

In `tests/unit/test_report.py`, replace the full content:

```python
import json
import os

from analogcoder.report import write_report_md, write_result_json

SAMPLE_RESULT = {
    "status": "PASS",
    "final_netlist_paths": {
        "ac_loop_gain": "runs/abc123/netlist_v1_ac_loop_gain.cir",
        "psr_plus": "runs/abc123/netlist_v1_psr_plus.cir",
    },
    "run_dir": "runs/abc123",
    "iterations_used": 2,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
}

SAMPLE_FAIL_RESULT = {
    "status": "FAIL",
    "final_netlist_paths": {"ac_loop_gain": "runs/abc123/netlist_v3_ac_loop_gain.cir"},
    "run_dir": "runs/abc123",
    "iterations_used": 10,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 15.0, "pass": False, "margin": -4.5}],
    "failure_reason": "max iterations reached",
}


def test_write_result_json(tmp_path):
    path = write_result_json(str(tmp_path), SAMPLE_RESULT)
    assert os.path.basename(path) == "result.json"
    with open(path) as f:
        assert json.load(f) == SAMPLE_RESULT


def test_write_report_md_includes_status_criteria_and_every_testbench_netlist(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "PASS" in content
    assert "gain" in content
    assert "[PASS] gain" in content
    assert "ac_loop_gain" in content
    assert "netlist_v1_ac_loop_gain.cir" in content
    assert "psr_plus" in content
    assert "netlist_v1_psr_plus.cir" in content


def test_write_report_md_includes_failure_reason(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_FAIL_RESULT)
    with open(path) as f:
        content = f.read()
    assert "max iterations reached" in content
    assert "[FAIL] gain" in content
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_report.py -v`
Expected: FAIL — `write_report_md` still prints `result['final_netlist_path']` (singular key, now absent from the fixtures) and raises `KeyError`.

- [ ] **Step 7: Rewrite `report.py`**

Replace the full content of `src/analogcoder/report.py`:

```python
import json
import os


def write_result_json(run_dir: str, result: dict) -> str:
    path = os.path.join(run_dir, "result.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return path


def write_report_md(run_dir: str, result: dict) -> str:
    lines = [
        "# Run Report",
        "",
        f"**Status:** {result['status']}",
        f"**Iterations used:** {result['iterations_used']}",
        "**Final netlists:**",
    ]
    for name, path in result["final_netlist_paths"].items():
        lines.append(f"- {name}: `{path}`")
    lines += [
        "",
        "## Final criteria",
        "",
    ]
    for c in result["final_criteria"]:
        mark = "PASS" if c["pass"] else "FAIL"
        lines.append(f"- [{mark}] {c['name']}: target {c['target']}, actual {c['actual']} (margin {c['margin']})")
    if result.get("failure_reason"):
        lines.append("")
        lines.append(f"**Failure reason:** {result['failure_reason']}")
    text = "\n".join(lines) + "\n"
    path = os.path.join(run_dir, "report.md")
    with open(path, "w") as f:
        f.write(text)
    return path
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_report.py -v`
Expected: PASS (3 tests)

- [ ] **Step 9: Fix the two real-backend integration tests to match the new wiring**

These are both skip-gated (one on `ANTHROPIC_API_KEY`, one on `LOCAL_LLM_BASE_URL`) so they won't run in a normal local/CI pass, but must stay correct for whoever runs them with real credentials.

In `tests/integration/test_end_to_end.py`, replace the full content:

```python
# tests/integration/test_end_to_end.py
import os

import pytest

from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "inverting_amp")


@pytest.mark.asyncio
async def test_inverting_amp_benchmark_passes_immediately(tmp_path, monkeypatch):
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))
    initial_netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            initial_netlist_texts[tb.name] = f.read()

    state = RunState(run_dir=str(tmp_path), testbench_names=[tb.name for tb in spec.testbenches])
    backend = NgspiceBackend()

    # The real simulation agent needs a live netlist path on disk, which only
    # exists once the orchestrator has pushed a version - so route it through
    # state.current_netlist_paths() exactly like the CLI does.
    async def simulate_fn(netlist_texts, spec_arg):
        merged_measurements = {}
        paths = state.current_netlist_paths()
        for tb in spec_arg.testbenches:
            result = await agent_simulate(paths[tb.name], tb.control_block, backend)
            merged_measurements.update(result["measurements"])
        return {"measurements": merged_measurements}

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.all_criteria)

    async def fake_analyze(netlist_text_arg):
        return {"circuit_type": "inverting amplifier", "stages": [], "component_roles": {}, "tunable_params": []}

    # This benchmark is designed to pass on the first simulation, so tune/verify
    # should never be invoked; make that an explicit assertion by failing loudly
    # if they are.
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("tuning/verification should not run for a passing benchmark")

    agents = OrchestratorAgents(
        analyze=fake_analyze,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=fail_if_called,
        verify_pre=fail_if_called,
        verify_post=fail_if_called,
        propose_topology=fail_if_called,
    )

    # These two calls hit the real Claude Agent SDK (simulate_fn -> agent_simulate,
    # judge_fn -> judge_measurements). If ANTHROPIC_API_KEY / SDK auth is not
    # configured in this environment, skip rather than fail the suite.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("requires a configured Claude Agent SDK credential to run live agents")

    result = await run_orchestration(initial_netlist_texts, spec, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert result["final_criteria"][0]["pass"] is True
```

In `tests/integration/test_local_llm_backend.py`, replace the full content:

```python
import os

import pytest

from analogcoder.agents.analyzer import analyze_netlist
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOCAL_LLM_BASE_URL"),
    reason="requires LOCAL_LLM_BASE_URL (and LOCAL_LLM_API_KEY) pointed at a real OpenAI-compatible server",
)


@pytest.mark.asyncio
async def test_inverting_amp_benchmark_with_local_llm_backend(tmp_path):
    agent_backend = OpenAICompatibleBackend(
        base_url=os.environ["LOCAL_LLM_BASE_URL"],
        api_key_env="LOCAL_LLM_API_KEY",
        model=os.environ.get("LOCAL_LLM_MODEL", "glm-5.2"),
    )
    sim_backend = NgspiceBackend()

    spec = load_spec("benchmarks/inverting_amp/spec.yaml")
    initial_netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            initial_netlist_texts[tb.name] = f.read()

    state = RunState(run_dir=str(tmp_path), testbench_names=[tb.name for tb in spec.testbenches])

    async def simulate_fn(netlist_texts, spec_arg):
        merged_measurements = {}
        paths = state.current_netlist_paths()
        for tb in spec_arg.testbenches:
            result = await agent_simulate(paths[tb.name], tb.control_block, sim_backend, agent_backend)
            merged_measurements.update(result["measurements"])
        return {"measurements": merged_measurements}

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.all_criteria, agent_backend)

    async def analyze_fn(netlist_text):
        return await analyze_netlist(netlist_text, agent_backend)

    async def tune_fn(analysis, judge_result, history, rejection_feedback, netlist_text_arg):
        return await propose_tuning(
            analysis, judge_result, history, rejection_feedback, netlist_text_arg, agent_backend
        )

    async def verify_pre_fn(analysis, judge_result, proposal, netlist_text_arg):
        return await verify_pre(analysis, judge_result, proposal, netlist_text_arg, agent_backend)

    async def verify_post_fn(prev_judge_result, new_judge_result, applied_changes):
        return await verify_post(prev_judge_result, new_judge_result, applied_changes, agent_backend)

    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=None,
    )

    result = await run_orchestration(initial_netlist_texts, spec, state, agents)

    assert result["status"] in ("PASS", "FAIL")
```

- [ ] **Step 10: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: All tests pass.

- [ ] **Step 11: Commit**

```bash
git add src/analogcoder/cli.py src/analogcoder/report.py tests/unit/test_cli.py tests/unit/test_report.py tests/integration/test_end_to_end.py tests/integration/test_local_llm_backend.py
git commit -m "feat: wire cli.py and report.py for multi-testbench runs"
```

---

## Task 5: PSR benchmark files and real-ngspice validation

**Files:**
- Create: `benchmarks/two_stage_opamp/netlist_psr_plus.cir`
- Create: `benchmarks/two_stage_opamp/netlist_psr_minus.cir`
- Modify: `benchmarks/two_stage_opamp/spec.yaml`
- Create: `tests/unit/test_psr_benchmark_ngspice.py`

**Interfaces:**
- Consumes: `load_spec` from Task 1 (real file), `NgspiceBackend` (unchanged, `src/analogcoder/simulators/ngspice.py`).
- Produces: nothing consumed by later tasks — this is the final, benchmark-facing task.

- [ ] **Step 1: Create the PSR+ testbench netlist**

Create `benchmarks/two_stage_opamp/netlist_psr_plus.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), generic level-1 devices.
* PSR+ testbench: AC=1 injected on Vdd, no input stimulus, same AC loop-break
* (Lfb) topology as the main AC testbench so the amp sees the same AC bias
* environment. Reading vdb(vout) with this topology gives supply-to-output
* gain directly. The OPAMP2STAGE subckt body below must stay byte-identical
* to netlist.cir and netlist_psr_minus.cir - tuning changes are applied to
* all three files independently and rely on that.
.model NMOSG NMOS (LEVEL=1 VTO=0.7 KP=120u LAMBDA=0.02)
.model PMOSG PMOS (LEVEL=1 VTO=-0.7 KP=40u LAMBDA=0.02)

.subckt OPAMP2STAGE vinp vinn vout vdd vss
Iref nb1 vdd 100u
M9 nb1 nb1 vdd vdd PMOSG W=20u L=1u

M1 n1   vinn tail vdd PMOSG W=40u L=1u
M2 outA vinp tail vdd PMOSG W=40u L=1u

M3 n1   n1   vss vss NMOSG W=20u L=1u
M4 outA n1   vss vss NMOSG W=20u L=1u

M5 tail nb1 vdd vdd PMOSG W=40u L=1u

M6 vout outA vss vss NMOSG W=40u L=1u
M7 vout nb1  vdd vdd PMOSG W=60u L=1u

Cc outA vout 2p
Ca outA 0 0.3p
.ends OPAMP2STAGE

Vdd vdd 0 DC 2.5 AC 1
Vss vss 0 DC -2.5

Vinp vinp 0 DC 0
Lfb vout vinn 1e6
Cin vstim vinn 1
Vstim vstim 0 DC 0

Xdut vinp vinn vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

Create `benchmarks/two_stage_opamp/netlist_psr_minus.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), generic level-1 devices.
* PSR- testbench: AC=1 injected on Vss, no input stimulus, same AC loop-break
* (Lfb) topology as the main AC testbench. The OPAMP2STAGE subckt body below
* must stay byte-identical to netlist.cir and netlist_psr_plus.cir - tuning
* changes are applied to all three files independently and rely on that.
.model NMOSG NMOS (LEVEL=1 VTO=0.7 KP=120u LAMBDA=0.02)
.model PMOSG PMOS (LEVEL=1 VTO=-0.7 KP=40u LAMBDA=0.02)

.subckt OPAMP2STAGE vinp vinn vout vdd vss
Iref nb1 vdd 100u
M9 nb1 nb1 vdd vdd PMOSG W=20u L=1u

M1 n1   vinn tail vdd PMOSG W=40u L=1u
M2 outA vinp tail vdd PMOSG W=40u L=1u

M3 n1   n1   vss vss NMOSG W=20u L=1u
M4 outA n1   vss vss NMOSG W=20u L=1u

M5 tail nb1 vdd vdd PMOSG W=40u L=1u

M6 vout outA vss vss NMOSG W=40u L=1u
M7 vout nb1  vdd vdd PMOSG W=60u L=1u

Cc outA vout 2p
Ca outA 0 0.3p
.ends OPAMP2STAGE

Vdd vdd 0 DC 2.5
Vss vss 0 DC -2.5 AC 1

Vinp vinp 0 DC 0
Lfb vout vinn 1e6
Cin vstim vinn 1
Vstim vstim 0 DC 0

Xdut vinp vinn vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

- [ ] **Step 2: Add the PSR testbenches to `benchmarks/two_stage_opamp/spec.yaml`**

Append two entries to the `testbenches:` list (the file already has the `ac_loop_gain` entry from Task 1 — add these after it, keeping the same indentation):

```yaml
  - name: psr_plus
    netlist: netlist_psr_plus.cir
    analyses: ["ac"]
    control_block: |
      .control
      ac dec 20 1 100meg
      meas ac psr_plus_db find vdb(vout) at=1
      .endc
    criteria:
      - name: psr_plus
        measurement: psr_plus_db
        operator: "<="
        threshold: -10.0
        unit: dB

  - name: psr_minus
    netlist: netlist_psr_minus.cir
    analyses: ["ac"]
    control_block: |
      .control
      ac dec 20 1 100meg
      meas ac psr_minus_db find vdb(vout) at=1
      .endc
    criteria:
      - name: psr_minus
        measurement: psr_minus_db
        operator: "<="
        threshold: -8.0
        unit: dB
```

The full file should now have three `testbenches` entries: `ac_loop_gain`, `psr_plus`, `psr_minus`.

- [ ] **Step 3: Write the real-ngspice validation test**

This mirrors the existing non-skip-gated real-ngspice pattern in `tests/unit/test_topology_swap_ngspice.py` and `tests/unit/test_ngspice_backend.py` (ngspice is a required local dependency per this project's setup, not mocked in these files).

Create `tests/unit/test_psr_benchmark_ngspice.py`:

```python
import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def _load_two_stage_opamp_spec():
    return load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))


def test_spec_declares_three_testbenches_with_expected_criteria():
    spec = _load_two_stage_opamp_spec()

    assert [tb.name for tb in spec.testbenches] == ["ac_loop_gain", "psr_plus", "psr_minus"]

    psr_plus = next(tb for tb in spec.testbenches if tb.name == "psr_plus")
    assert psr_plus.criteria[0].measurement == "psr_plus_db"
    assert psr_plus.criteria[0].operator == "<="
    assert psr_plus.criteria[0].threshold == -10.0

    psr_minus = next(tb for tb in spec.testbenches if tb.name == "psr_minus")
    assert psr_minus.criteria[0].measurement == "psr_minus_db"
    assert psr_minus.criteria[0].operator == "<="
    assert psr_minus.criteria[0].threshold == -8.0


def test_baseline_netlist_matches_validated_psr_measurements():
    # These are the real ngspice-46 measurements recorded in
    # docs/superpowers/specs/2026-07-25-psr-verification-design.md's Validation
    # section for the unmodified benchmark netlists. This test exists to catch
    # unintentional drift in the committed .cir files (e.g. a future edit to
    # netlist.cir's subckt not mirrored into the PSR files) - not to re-derive
    # the thresholds.
    spec = _load_two_stage_opamp_spec()
    backend = NgspiceBackend()

    psr_plus = next(tb for tb in spec.testbenches if tb.name == "psr_plus")
    result = backend.run(psr_plus.netlist_path, {"control_block": psr_plus.control_block})
    assert result.status == "success"
    assert -15.5 <= result.measurements["psr_plus_db"] <= -14.5

    psr_minus = next(tb for tb in spec.testbenches if tb.name == "psr_minus")
    result = backend.run(psr_minus.netlist_path, {"control_block": psr_minus.control_block})
    assert result.status == "success"
    assert -3.6 <= result.measurements["psr_minus_db"] <= -3.1


def test_psr_plus_and_psr_minus_subckt_bodies_match_main_testbench():
    # Enforces the invariant this whole feature depends on: tuning changes
    # applied independently to each testbench file only stay consistent if
    # the OPAMP2STAGE subckt text is byte-identical across all three.
    spec = _load_two_stage_opamp_spec()
    bodies = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            text = f.read()
        start = text.index(".subckt OPAMP2STAGE")
        end = text.index(".ends OPAMP2STAGE") + len(".ends OPAMP2STAGE")
        bodies[tb.name] = text[start:end]

    assert bodies["ac_loop_gain"] == bodies["psr_plus"] == bodies["psr_minus"]
```

- [ ] **Step 4: Run the new test to verify it passes against the real files**

Run: `.venv/bin/python -m pytest tests/unit/test_psr_benchmark_ngspice.py -v`
Expected: PASS (3 tests). If `test_baseline_netlist_matches_validated_psr_measurements` fails, re-check the netlist files against Step 1 exactly — a typo in the stimulus lines is the most likely cause, since the subckt content is copy-pasted from the already-committed `netlist.cir`.

- [ ] **Step 5: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/two_stage_opamp/netlist_psr_plus.cir benchmarks/two_stage_opamp/netlist_psr_minus.cir benchmarks/two_stage_opamp/spec.yaml tests/unit/test_psr_benchmark_ngspice.py
git commit -m "feat: add PSR+/PSR- testbenches to the two_stage_opamp benchmark"
```

---

## Post-plan manual validation (not automated)

After all five tasks land, run a real end-to-end orchestration against the full 3-testbench `two_stage_opamp` benchmark (real Claude backend), the same way prior features in this project were validated:

```bash
.venv/bin/analogcoder --spec benchmarks/two_stage_opamp/spec.yaml --run-dir runs/psr_validation_1
```

Check `runs/psr_validation_1/result.json` and `history.jsonl` for: all 5 criteria passing at the end, `area_check` events showing what was rejected/approved, and — the property this whole feature exists to prove — at least one `verify_post` step where a testbench other than the one currently being targeted was checked and didn't regress. If the run happens to find a solution in one shot without ever triggering a rollback, that's a legitimate pass but doesn't exercise the cross-testbench regression path live; note that limitation the same way the area-aware-tuning validation did.
