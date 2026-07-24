# 회로 검증-튜닝 멀티 에이전트 시스템 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI tool that repeatedly simulates a SPICE netlist with ngspice, judges it against a target spec, and — via a team of 5 Claude Agent SDK agents coordinated by a deterministic Python orchestrator — tunes component parameter values until the circuit passes or a hardcoded iteration budget is exhausted.

**Architecture:** A deterministic Python orchestrator loop (no LLM) drives five independent Claude Agent SDK `query()` calls (netlist analysis, simulation, judgment, tuning, verification), each constrained to a JSON-schema `output_format` so the orchestrator never parses free text. Simulation goes through a `SimulatorBackend` abstract interface (`NgspiceBackend` for MVP) so a future `HspiceBackend` can be swapped in without touching any agent code.

**Tech Stack:** Python >= 3.11, `claude-agent-sdk` (Python), `pyyaml`, `pytest` + `pytest-asyncio`, `jsonschema` (dev), ngspice CLI (local: `ngspice-46` at `/opt/homebrew/bin/ngspice`).

## Global Constraints

- Python >= 3.11; package uses `src/` layout, installed with `pip install -e ".[dev]"`.
- ngspice must be on `PATH` (already confirmed installed locally).
- Orchestrator loop constants: `MAX_OUTER_ITERATIONS=10`, `MAX_TUNING_RETRIES=3` — defined once in `analogcoder/orchestrator.py`, not duplicated elsewhere.
- Every agent function returns a `dict` already validated against its schema in `analogcoder/schemas.py` — no free-text parsing anywhere in orchestrator or CLI code.
- All ngspice invocations go through `analogcoder/simulators/ngspice.py`; no other module calls the `ngspice` binary directly, so a future `HspiceBackend` is a drop-in.
- CLI exit code: `0` on `PASS`, `1` on `FAIL`.
- Every new Python module gets a corresponding test module under `tests/unit/` (or `tests/integration/` for the final task).

---

## Task 1: Project scaffolding & dependencies

**Files:**
- Create: `pyproject.toml`
- Create: `src/analogcoder/__init__.py`
- Create: `.gitignore`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`

**Interfaces:**
- Produces: an installable `analogcoder` package (src-layout) that every later task adds modules to, and a `pytest` setup every later task's tests run under.

- [ ] **Step 1: Create the package skeleton**

```bash
mkdir -p src/analogcoder tests/unit tests/integration
touch src/analogcoder/__init__.py tests/__init__.py tests/unit/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "analogcoder"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "claude-agent-sdk>=0.1.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "jsonschema>=4.21",
]

[project.scripts]
analogcoder = "analogcoder.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/analogcoder"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 3: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
*.egg-info/
runs/
.pytest_cache/
```

- [ ] **Step 4: Install and verify pytest runs**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Expected: `pip install` succeeds; `pytest -q` reports no tests collected (exit code 5) since no test files exist yet — this is expected at this step.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore src/analogcoder/__init__.py tests/__init__.py tests/unit/__init__.py
git commit -m "chore: scaffold analogcoder package with pytest setup"
```

---

## Task 2: SPICE netlist parser and editor

**Files:**
- Create: `src/analogcoder/netlist.py`
- Test: `tests/unit/test_netlist.py`

**Interfaces:**
- Produces: `Component`, `Subckt`, `ParsedNetlist` dataclasses; `parse_netlist(text: str) -> ParsedNetlist`; `apply_changes(text: str, changes: list[dict]) -> str` where each change dict has keys `refdes`, `param` (`"value"` for the primary positional value, else a named param like `"W"`), `new_value`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_netlist.py
from analogcoder.netlist import parse_netlist, apply_changes

SIMPLE_NETLIST = """\
* simple RC
Vin in 0 AC 1
Rin in vminus 1k
Rf vminus vout 10k
.end
"""

SUBCKT_NETLIST = """\
.subckt amp in out
R1 in mid 1k
R2 mid out 2k
.ends
Xamp1 a b amp
.end
"""


def test_parse_netlist_top_level_components():
    parsed = parse_netlist(SIMPLE_NETLIST)
    refdes = [c.refdes for c in parsed.top_components]
    assert refdes == ["Vin", "Rin", "Rf"]
    rin = next(c for c in parsed.top_components if c.refdes == "Rin")
    assert rin.nodes == ["in", "vminus"]
    assert rin.value == "1k"


def test_parse_netlist_subckt_block():
    parsed = parse_netlist(SUBCKT_NETLIST)
    assert "amp" in parsed.subckts
    subckt = parsed.subckts["amp"]
    assert subckt.ports == ["in", "out"]
    assert [c.refdes for c in subckt.components] == ["R1", "R2"]
    assert len(parsed.top_components) == 1
    assert parsed.top_components[0].refdes == "Xamp1"


def test_apply_changes_replaces_primary_value():
    updated = apply_changes(SIMPLE_NETLIST, [{"refdes": "Rf", "param": "value", "new_value": "20k"}])
    parsed = parse_netlist(updated)
    rf = next(c for c in parsed.top_components if c.refdes == "Rf")
    assert rf.value == "20k"


def test_apply_changes_sets_named_param():
    netlist = "M1 d g s b nmos W=1u L=0.18u\n.end\n"
    updated = apply_changes(netlist, [{"refdes": "M1", "param": "W", "new_value": "2u"}])
    parsed = parse_netlist(updated)
    m1 = parsed.top_components[0]
    assert m1.params["W"] == "2u"
    assert m1.params["L"] == "0.18u"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_netlist.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.netlist'`.

- [ ] **Step 3: Implement `src/analogcoder/netlist.py`**

```python
import re
from dataclasses import dataclass, field


@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None
    params: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""


@dataclass
class Subckt:
    name: str
    ports: list[str]
    components: list[Component] = field(default_factory=list)


@dataclass
class ParsedNetlist:
    top_components: list[Component]
    subckts: dict[str, Subckt]


_PARAM_RE = re.compile(r"^(\w+)=(\S+)$")


def _parse_component_line(line: str) -> Component:
    tokens = line.split()
    refdes = tokens[0]
    ctype = refdes[0].upper()
    params: dict[str, str] = {}
    positional: list[str] = []
    for tok in tokens[1:]:
        m = _PARAM_RE.match(tok)
        if m:
            params[m.group(1)] = m.group(2)
        else:
            positional.append(tok)
    nodes = positional[:-1] if positional else []
    value = positional[-1] if positional else None
    return Component(refdes=refdes, ctype=ctype, nodes=nodes, value=value, params=params, raw_line=line)


def parse_netlist(text: str) -> ParsedNetlist:
    top_components: list[Component] = []
    subckts: dict[str, Subckt] = {}
    current_subckt: Subckt | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        lower = line.lower()
        if lower.startswith(".subckt"):
            tokens = line.split()
            name = tokens[1]
            ports = tokens[2:]
            current_subckt = Subckt(name=name, ports=ports)
            subckts[name] = current_subckt
            continue
        if lower.startswith(".ends"):
            current_subckt = None
            continue
        if line.startswith("."):
            continue
        component = _parse_component_line(line)
        if current_subckt is not None:
            current_subckt.components.append(component)
        else:
            top_components.append(component)

    return ParsedNetlist(top_components=top_components, subckts=subckts)


def apply_changes(text: str, changes: list[dict]) -> str:
    lines = text.splitlines()
    for change in changes:
        refdes = change["refdes"]
        param = change["param"]
        new_value = change["new_value"]
        for i, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("*") or stripped.startswith("."):
                continue
            tokens = stripped.split()
            if tokens[0] != refdes:
                continue
            if param == "value":
                positional_idx = [j for j, t in enumerate(tokens) if "=" not in t]
                tokens[positional_idx[-1]] = new_value
            else:
                replaced = False
                for j, tok in enumerate(tokens):
                    if tok.startswith(f"{param}="):
                        tokens[j] = f"{param}={new_value}"
                        replaced = True
                        break
                if not replaced:
                    tokens.append(f"{param}={new_value}")
            lines[i] = " ".join(tokens)
            break
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_netlist.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/netlist.py tests/unit/test_netlist.py
git commit -m "feat: add SPICE netlist parser and parameter editor"
```

---

## Task 3: Target spec loader

**Files:**
- Create: `src/analogcoder/spec.py`
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Criterion` and `TargetSpec` dataclasses; `load_spec(path: str) -> TargetSpec`. `TargetSpec.criteria: list[Criterion]` and `TargetSpec.control_block: str` are consumed by Task 4 (simulator), Task 10 (judge tool), and Task 14 (orchestrator).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec.py
import textwrap
from analogcoder.spec import load_spec

SPEC_YAML = textwrap.dedent("""\
    circuit_name: inverting_amplifier
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

    spec = load_spec(str(spec_path))

    assert spec.circuit_name == "inverting_amplifier"
    assert spec.analyses == ["ac"]
    assert "meas ac gain_db" in spec.control_block
    assert len(spec.criteria) == 1
    c = spec.criteria[0]
    assert c.name == "closed_loop_gain"
    assert c.measurement == "gain_db"
    assert c.operator == ">="
    assert c.threshold == 19.5
    assert c.unit == "dB"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_spec.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.spec'`.

- [ ] **Step 3: Implement `src/analogcoder/spec.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_spec.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/spec.py tests/unit/test_spec.py
git commit -m "feat: add YAML target spec loader"
```

---

## Task 4: SimulatorBackend interface, NgspiceBackend, and the inverting-amplifier benchmark

**Files:**
- Create: `src/analogcoder/simulators/__init__.py`
- Create: `src/analogcoder/simulators/base.py`
- Create: `src/analogcoder/simulators/ngspice.py`
- Create: `benchmarks/inverting_amp/netlist.cir`
- Create: `benchmarks/inverting_amp/spec.yaml`
- Test: `tests/unit/test_ngspice_backend.py`

**Interfaces:**
- Consumes: `analogcoder.spec.load_spec` (Task 3) to read `control_block` from the benchmark spec in the test.
- Produces: `RawSimResult` dataclass, `SimulatorBackend` ABC with `run(netlist_path: str, testbench_config: dict) -> RawSimResult`, `NgspiceBackend(ngspice_bin: str = "ngspice")`. `testbench_config` is a dict with key `"control_block"`. Consumed by Task 9 (simulation agent tool) and Task 17 (integration test).

- [ ] **Step 1: Create the benchmark fixture**

```
# benchmarks/inverting_amp/netlist.cir
* Inverting amplifier built from an ideal op-amp (VCVS)
Vin in 0 AC 1
Rin in vminus 1k
Rf vminus vout 10k
Eopamp vout 0 0 vminus 100k
.end
```

```yaml
# benchmarks/inverting_amp/spec.yaml
circuit_name: inverting_amplifier
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

This models an ideal op-amp as a voltage-controlled voltage source (`Eopamp`) with very high open-loop gain (100k), so the closed-loop gain is set by the resistor ratio `-Rf/Rin = -10` (20 dB magnitude), independent of any transistor model — no PDK required, and ngspice converges trivially.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_ngspice_backend.py
import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "inverting_amp")


def test_ngspice_backend_runs_inverting_amp_benchmark():
    netlist_path = os.path.join(BENCHMARK_DIR, "netlist.cir")
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))

    backend = NgspiceBackend()
    result = backend.run(netlist_path, {"control_block": spec.control_block})

    assert result.status == "success"
    assert "gain_db" in result.measurements
    assert 19.0 <= result.measurements["gain_db"] <= 21.0


def test_ngspice_backend_reports_error_on_bad_netlist(tmp_path):
    bad_netlist = tmp_path / "bad.cir"
    bad_netlist.write_text("Rin in vminus\n.end\n")  # missing value token

    backend = NgspiceBackend()
    result = backend.run(str(bad_netlist), {"control_block": ".control\nac dec 10 1 1meg\n.endc"})

    assert result.status == "error"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/unit/test_ngspice_backend.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.simulators'`.

- [ ] **Step 4: Implement the backend**

```python
# src/analogcoder/simulators/__init__.py
```
(empty file)

```python
# src/analogcoder/simulators/base.py
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
```

```python
# src/analogcoder/simulators/ngspice.py
import os
import re
import subprocess
import tempfile

from analogcoder.simulators.base import RawSimResult, SimulatorBackend

_MEASURE_RE = re.compile(r"^(\w+)\s*=\s*([-+0-9.eE]+)\s*$")


class NgspiceBackend(SimulatorBackend):
    def __init__(self, ngspice_bin: str = "ngspice"):
        self.ngspice_bin = ngspice_bin

    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        with open(netlist_path) as f:
            lines = f.readlines()

        body = [ln for ln in lines if ln.strip().lower() != ".end"]
        control_block = testbench_config["control_block"]
        deck = "".join(body) + "\n" + control_block + "\n.end\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            deck_path = os.path.join(tmpdir, "deck.cir")
            with open(deck_path, "w") as f:
                f.write(deck)

            proc = subprocess.run(
                [self.ngspice_bin, "-b", deck_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            log_text = proc.stdout + proc.stderr

        measurements: dict[str, float] = {}
        for line in log_text.splitlines():
            m = _MEASURE_RE.match(line.strip())
            if m:
                measurements[m.group(1)] = float(m.group(2))

        warnings = [ln for ln in log_text.splitlines() if "warning" in ln.lower()]

        lower_log = log_text.lower()
        if "no convergence" in lower_log or "singular matrix" in lower_log:
            status = "convergence_failure"
        elif proc.returncode != 0 or not measurements:
            status = "error"
        else:
            status = "success"

        return RawSimResult(status=status, measurements=measurements, raw_log=log_text, warnings=warnings)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/unit/test_ngspice_backend.py -v
```

Expected: 2 passed. (This actually invokes the local `ngspice` binary — if it fails, run `ngspice -b <deck>` manually on the printed temp path to debug the deck syntax.)

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/simulators benchmarks/inverting_amp tests/unit/test_ngspice_backend.py
git commit -m "feat: add SimulatorBackend interface, NgspiceBackend, and inverting-amp benchmark"
```

---

## Task 5: Deterministic judge tool (`evaluate_criteria`)

**Files:**
- Create: `src/analogcoder/judge_tools.py`
- Test: `tests/unit/test_judge_tools.py`

**Interfaces:**
- Consumes: `Criterion` from `analogcoder.spec` (Task 3).
- Produces: `evaluate_criteria(measurements: dict[str, float], criteria: list[Criterion]) -> dict` returning `{"overall_pass": bool, "criteria": [{"name", "target", "actual", "pass", "margin"}], "summary": str}`. Consumed by Task 10 (judge agent tool wrapper) and Task 14 (orchestrator tests, indirectly via the judge agent).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_judge_tools.py
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.spec import Criterion


def test_evaluate_criteria_all_pass():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    result = evaluate_criteria({"gain_db": 20.0}, criteria)
    assert result["overall_pass"] is True
    assert result["criteria"][0]["pass"] is True
    assert result["criteria"][0]["margin"] == 0.5


def test_evaluate_criteria_one_fails():
    criteria = [
        Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB"),
        Criterion(name="power", measurement="power_mw", operator="<=", threshold=5.0, unit="mW"),
    ]
    result = evaluate_criteria({"gain_db": 18.0, "power_mw": 4.0}, criteria)
    assert result["overall_pass"] is False
    gain_result = next(c for c in result["criteria"] if c["name"] == "gain")
    assert gain_result["pass"] is False
    assert gain_result["margin"] == -1.5


def test_evaluate_criteria_missing_measurement_fails():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    result = evaluate_criteria({}, criteria)
    assert result["overall_pass"] is False
    assert result["criteria"][0]["pass"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_judge_tools.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.judge_tools'`.

- [ ] **Step 3: Implement `src/analogcoder/judge_tools.py`**

```python
import math

from analogcoder.spec import Criterion

_OPERATORS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}


def evaluate_criteria(measurements: dict, criteria: list[Criterion]) -> dict:
    results = []
    overall_pass = True

    for c in criteria:
        actual = measurements.get(c.measurement)
        if actual is None:
            results.append({
                "name": c.name,
                "target": f"{c.operator}{c.threshold}",
                "actual": math.nan,
                "pass": False,
                "margin": math.nan,
            })
            overall_pass = False
            continue

        passed = _OPERATORS[c.operator](actual, c.threshold)
        margin = actual - c.threshold
        results.append({
            "name": c.name,
            "target": f"{c.operator}{c.threshold}",
            "actual": actual,
            "pass": passed,
            "margin": margin,
        })
        overall_pass = overall_pass and passed

    summary = "all criteria passed" if overall_pass else "one or more criteria failed"
    return {"overall_pass": overall_pass, "criteria": results, "summary": summary}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_judge_tools.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/judge_tools.py tests/unit/test_judge_tools.py
git commit -m "feat: add deterministic evaluate_criteria judge tool"
```

---

## Task 6: Agent output schemas

**Files:**
- Create: `src/analogcoder/schemas.py`
- Test: `tests/unit/test_schemas.py`

**Interfaces:**
- Produces: `ANALYZER_SCHEMA`, `SIMULATION_SCHEMA`, `JUDGE_SCHEMA`, `TUNER_SCHEMA`, `VERIFIER_PRE_SCHEMA`, `VERIFIER_POST_SCHEMA` — JSON Schema dicts. Consumed by every agent module in Tasks 8–12 as their `output_format`/`output_schema`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_schemas.py
import jsonschema

from analogcoder.schemas import (
    ANALYZER_SCHEMA,
    JUDGE_SCHEMA,
    SIMULATION_SCHEMA,
    TUNER_SCHEMA,
    VERIFIER_POST_SCHEMA,
    VERIFIER_PRE_SCHEMA,
)


def test_analyzer_schema_accepts_valid_payload():
    payload = {
        "circuit_type": "inverting amplifier",
        "stages": [{"name": "feedback stage", "role": "sets closed-loop gain", "components": ["Rin", "Rf"]}],
        "component_roles": {"Rin": "input resistor", "Rf": "feedback resistor"},
        "tunable_params": [{"refdes": "Rf", "param": "value", "role_in_circuit": "sets gain magnitude"}],
    }
    jsonschema.validate(payload, ANALYZER_SCHEMA)


def test_simulation_schema_accepts_valid_payload():
    payload = {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}
    jsonschema.validate(payload, SIMULATION_SCHEMA)


def test_judge_schema_accepts_valid_payload():
    payload = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
    }
    jsonschema.validate(payload, JUDGE_SCHEMA)


def test_tuner_schema_accepts_valid_payload():
    payload = {
        "proposed_changes": [
            {"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k", "reasoning": "increase gain"}
        ],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    jsonschema.validate(payload, TUNER_SCHEMA)


def test_verifier_pre_schema_accepts_valid_payload():
    jsonschema.validate({"approved": True, "concerns": [], "feedback": "looks reasonable"}, VERIFIER_PRE_SCHEMA)


def test_verifier_post_schema_accepts_valid_payload():
    jsonschema.validate(
        {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "gain now passes"},
        VERIFIER_POST_SCHEMA,
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_schemas.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.schemas'`.

- [ ] **Step 3: Implement `src/analogcoder/schemas.py`**

```python
ANALYZER_SCHEMA = {
    "type": "object",
    "properties": {
        "circuit_type": {"type": "string"},
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "components": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "role", "components"],
            },
        },
        "component_roles": {"type": "object", "additionalProperties": {"type": "string"}},
        "tunable_params": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refdes": {"type": "string"},
                    "param": {"type": "string"},
                    "role_in_circuit": {"type": "string"},
                },
                "required": ["refdes", "param", "role_in_circuit"],
            },
        },
    },
    "required": ["circuit_type", "stages", "component_roles", "tunable_params"],
}

SIMULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "measurements": {"type": "object", "additionalProperties": {"type": "number"}},
        "status": {"enum": ["success", "convergence_failure", "error"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["measurements", "status", "warnings"],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_pass": {"type": "boolean"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "target": {"type": "string"},
                    "actual": {"type": "number"},
                    "pass": {"type": "boolean"},
                    "margin": {"type": "number"},
                },
                "required": ["name", "target", "actual", "pass", "margin"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["overall_pass", "criteria", "summary"],
}

TUNER_SCHEMA = {
    "type": "object",
    "properties": {
        "proposed_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refdes": {"type": "string"},
                    "param": {"type": "string"},
                    "old_value": {"type": "string"},
                    "new_value": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["refdes", "param", "old_value", "new_value", "reasoning"],
            },
        },
        "overall_reasoning": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["proposed_changes", "overall_reasoning", "confidence"],
}

VERIFIER_PRE_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "feedback": {"type": "string"},
    },
    "required": ["approved", "concerns", "feedback"],
}

VERIFIER_POST_SCHEMA = {
    "type": "object",
    "properties": {
        "improved": {"type": "boolean"},
        "regressed_criteria": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"enum": ["keep", "rollback"]},
        "feedback": {"type": "string"},
    },
    "required": ["improved", "regressed_criteria", "recommendation", "feedback"],
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_schemas.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/schemas.py tests/unit/test_schemas.py
git commit -m "feat: add JSON schemas for all five agent outputs"
```

---

## Task 7: Claude Agent SDK helper (`run_agent`)

**Files:**
- Create: `src/analogcoder/agents/__init__.py`
- Create: `src/analogcoder/agents/_sdk_utils.py`
- Test: `tests/unit/test_sdk_utils.py`

**Interfaces:**
- Produces: `AgentExecutionError(RuntimeError)`; `async run_agent(system_prompt: str, user_prompt: str, output_schema: dict, mcp_servers: dict | None = None, allowed_tools: list[str] | None = None) -> dict`. This is the single chokepoint every one of the 5 agent modules (Tasks 8–12) uses to call the Claude Agent SDK, so it must be independently mockable by patching `analogcoder.agents._sdk_utils.query`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_sdk_utils.py
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from analogcoder.agents._sdk_utils import AgentExecutionError, run_agent


def _result_message(structured_output=None, is_error=False):
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        structured_output=structured_output,
    )


async def _fake_query_success(prompt, options):
    yield _result_message(structured_output={"ok": True})


async def _fake_query_error(prompt, options):
    yield _result_message(is_error=True)


async def _fake_query_no_result_message(prompt, options):
    return
    yield  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_run_agent_returns_structured_output():
    with patch("analogcoder.agents._sdk_utils.query", _fake_query_success):
        result = await run_agent("system prompt", "user prompt", {"type": "object"})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_run_agent_raises_on_error_result():
    with patch("analogcoder.agents._sdk_utils.query", _fake_query_error):
        with pytest.raises(AgentExecutionError):
            await run_agent("system prompt", "user prompt", {"type": "object"})


@pytest.mark.asyncio
async def test_run_agent_raises_when_no_result_message():
    with patch("analogcoder.agents._sdk_utils.query", _fake_query_no_result_message):
        with pytest.raises(AgentExecutionError):
            await run_agent("system prompt", "user prompt", {"type": "object"})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_sdk_utils.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents'`.

- [ ] **Step 3: Implement the helper**

```python
# src/analogcoder/agents/__init__.py
```
(empty file)

```python
# src/analogcoder/agents/_sdk_utils.py
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query


class AgentExecutionError(RuntimeError):
    """Raised when an agent query errors out or returns no structured output."""


async def run_agent(
    system_prompt: str,
    user_prompt: str,
    output_schema: dict,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
) -> dict:
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        output_format={"type": "json_schema", "schema": output_schema},
        mcp_servers=mcp_servers or {},
        allowed_tools=allowed_tools or [],
    )

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, ResultMessage):
            if message.is_error or message.structured_output is None:
                raise AgentExecutionError(
                    f"agent query failed: subtype={message.subtype} errors={message.errors}"
                )
            return message.structured_output

    raise AgentExecutionError("agent query stream ended without a ResultMessage")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_sdk_utils.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/__init__.py src/analogcoder/agents/_sdk_utils.py tests/unit/test_sdk_utils.py
git commit -m "feat: add mockable Claude Agent SDK query helper"
```

---

## Task 8: Netlist Analysis Agent

**Files:**
- Create: `src/analogcoder/agents/analyzer.py`
- Test: `tests/unit/test_analyzer_agent.py`

**Interfaces:**
- Consumes: `run_agent` (Task 7), `ANALYZER_SCHEMA` (Task 6).
- Produces: `async analyze_netlist(netlist_text: str) -> dict` matching `ANALYZER_SCHEMA`. Consumed by Task 14 (orchestrator, called once and cached) and Task 16 (CLI wiring).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_analyzer_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.analyzer import analyze_netlist


@pytest.mark.asyncio
async def test_analyze_netlist_calls_run_agent_with_netlist_text():
    fake_result = {
        "circuit_type": "inverting amplifier",
        "stages": [],
        "component_roles": {},
        "tunable_params": [],
    }
    with patch("analogcoder.agents.analyzer.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await analyze_netlist("Rin in vminus 1k\n.end\n")

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "Rin in vminus 1k" in kwargs["user_prompt"]
    assert kwargs["output_schema"]["required"] == ["circuit_type", "stages", "component_roles", "tunable_params"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_analyzer_agent.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.analyzer'`.

- [ ] **Step 3: Implement `src/analogcoder/agents/analyzer.py`**

```python
from analogcoder.agents._sdk_utils import run_agent
from analogcoder.schemas import ANALYZER_SCHEMA

ANALYZER_SYSTEM_PROMPT = """You are a senior analog IC design engineer. Given a SPICE
netlist, identify the circuit type, break it into functional stages, explain the role
of each component, and list which components/parameters are safe to tune without
changing the circuit's topology. Respond only via the structured output schema."""


async def analyze_netlist(netlist_text: str) -> dict:
    user_prompt = f"Analyze this SPICE netlist:\n\n{netlist_text}"
    return await run_agent(
        system_prompt=ANALYZER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=ANALYZER_SCHEMA,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_analyzer_agent.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/analyzer.py tests/unit/test_analyzer_agent.py
git commit -m "feat: add netlist analysis agent"
```

---

## Task 9: Simulation Agent

**Files:**
- Create: `src/analogcoder/agents/simulator_agent.py`
- Test: `tests/unit/test_simulator_agent.py`

**Interfaces:**
- Consumes: `run_agent` (Task 7), `SIMULATION_SCHEMA` (Task 6), `SimulatorBackend` (Task 4).
- Produces: `async simulate(netlist_path: str, control_block: str, backend: SimulatorBackend) -> dict` matching `SIMULATION_SCHEMA`. Consumed by Task 14 (orchestrator) and Task 16 (CLI wiring).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_simulator_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.simulator_agent import simulate
from analogcoder.simulators.base import RawSimResult, SimulatorBackend


class FakeBackend(SimulatorBackend):
    def run(self, netlist_path, testbench_config):
        return RawSimResult(status="success", measurements={"gain_db": 20.0}, raw_log="ok", warnings=[])


@pytest.mark.asyncio
async def test_simulate_calls_run_agent_with_netlist_path_and_control_block():
    fake_result = {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}
    with patch(
        "analogcoder.agents.simulator_agent.run_agent", new=AsyncMock(return_value=fake_result)
    ) as mock_run:
        result = await simulate("benchmarks/inverting_amp/netlist.cir", ".control\nac dec 10 1 1meg\n.endc", FakeBackend())

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "benchmarks/inverting_amp/netlist.cir" in kwargs["user_prompt"]
    assert "ac dec 10 1 1meg" in kwargs["user_prompt"]
    assert kwargs["allowed_tools"] == ["mcp__simulation__run_simulation"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_simulator_agent.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.simulator_agent'`.

- [ ] **Step 3: Implement `src/analogcoder/agents/simulator_agent.py`**

```python
import json
from dataclasses import asdict

from claude_agent_sdk import create_sdk_mcp_server, tool

from analogcoder.agents._sdk_utils import run_agent
from analogcoder.schemas import SIMULATION_SCHEMA
from analogcoder.simulators.base import SimulatorBackend

SIMULATION_SYSTEM_PROMPT = """You are a SPICE simulation specialist. You are given a
netlist file path and a target spec's control block (analysis + measure directives).
Call the run_simulation tool to execute the simulation. If it reports a
convergence_failure, you may retry by adjusting the .options portion of the control
block (e.g. gmin stepping, method=gear), up to 2 extra attempts, before reporting
the final result via the structured output schema. Never modify component values."""


def _build_simulation_tool(backend: SimulatorBackend, netlist_path: str):
    @tool(
        "run_simulation",
        "Run the netlist through the configured simulator backend",
        {"control_block": str},
    )
    async def _run(args):
        result = backend.run(netlist_path, {"control_block": args["control_block"]})
        return {"content": [{"type": "text", "text": json.dumps(asdict(result))}]}

    return _run


async def simulate(netlist_path: str, control_block: str, backend: SimulatorBackend) -> dict:
    sim_tool = _build_simulation_tool(backend, netlist_path)
    server = create_sdk_mcp_server("simulation", tools=[sim_tool])
    user_prompt = f"Netlist path: {netlist_path}\nControl block:\n{control_block}"
    return await run_agent(
        system_prompt=SIMULATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=SIMULATION_SCHEMA,
        mcp_servers={"simulation": server},
        allowed_tools=["mcp__simulation__run_simulation"],
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_simulator_agent.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/simulator_agent.py tests/unit/test_simulator_agent.py
git commit -m "feat: add simulation agent wrapping SimulatorBackend as an SDK tool"
```

---

## Task 10: Judge Agent

**Files:**
- Create: `src/analogcoder/agents/judge.py`
- Test: `tests/unit/test_judge_agent.py`

**Interfaces:**
- Consumes: `run_agent` (Task 7), `JUDGE_SCHEMA` (Task 6), `evaluate_criteria` (Task 5), `Criterion` (Task 3).
- Produces: `async judge_measurements(measurements: dict, criteria: list[Criterion]) -> dict` matching `JUDGE_SCHEMA`. Consumed by Task 14 (orchestrator) and Task 16 (CLI wiring).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_judge_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.judge import judge_measurements
from analogcoder.spec import Criterion


@pytest.mark.asyncio
async def test_judge_measurements_calls_run_agent_with_measurements_and_criteria():
    fake_result = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
    }
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]

    with patch("analogcoder.agents.judge.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await judge_measurements({"gain_db": 20.0}, criteria)

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["allowed_tools"] == ["mcp__judge__evaluate_criteria"]
    assert "gain_db" in kwargs["user_prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_judge_agent.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.judge'`.

- [ ] **Step 3: Implement `src/analogcoder/agents/judge.py`**

```python
import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from analogcoder.agents._sdk_utils import run_agent
from analogcoder.judge_tools import evaluate_criteria
from analogcoder.schemas import JUDGE_SCHEMA
from analogcoder.spec import Criterion

JUDGE_SYSTEM_PROMPT = """You are an analog circuit judge. You are given simulation
measurements and a list of pass/fail criteria. Call the evaluate_criteria tool to
compute results precisely, then report them via the structured output schema. Do
not compute pass/fail comparisons yourself; always use the tool."""


def _build_judge_tool(criteria: list[Criterion]):
    @tool(
        "evaluate_criteria",
        "Compare measurements against target criteria",
        {"measurements": dict},
    )
    async def _evaluate(args):
        result = evaluate_criteria(args["measurements"], criteria)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return _evaluate


async def judge_measurements(measurements: dict, criteria: list[Criterion]) -> dict:
    judge_tool = _build_judge_tool(criteria)
    server = create_sdk_mcp_server("judge", tools=[judge_tool])
    user_prompt = f"Measurements: {measurements}\nCriteria: {criteria}"
    return await run_agent(
        system_prompt=JUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=JUDGE_SCHEMA,
        mcp_servers={"judge": server},
        allowed_tools=["mcp__judge__evaluate_criteria"],
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_judge_agent.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/judge.py tests/unit/test_judge_agent.py
git commit -m "feat: add judge agent wrapping evaluate_criteria as an SDK tool"
```

---

## Task 11: Tuning Agent

**Files:**
- Create: `src/analogcoder/agents/tuner.py`
- Test: `tests/unit/test_tuner_agent.py`

**Interfaces:**
- Consumes: `run_agent` (Task 7), `TUNER_SCHEMA` (Task 6).
- Produces: `async propose_tuning(analysis: dict, judge_result: dict, history: list[dict], rejection_feedback: str | None) -> dict` matching `TUNER_SCHEMA`. Consumed by Task 14 (orchestrator).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tuner_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.tuner import propose_tuning


@pytest.mark.asyncio
async def test_propose_tuning_includes_history_and_rejection_feedback_in_prompt():
    fake_result = {
        "proposed_changes": [
            {"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k", "reasoning": "increase gain"}
        ],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await propose_tuning(
            analysis={"circuit_type": "inverting amplifier"},
            judge_result={"overall_pass": False},
            history=[{"outer_iter": 1, "recommendation": "rollback"}],
            rejection_feedback="last proposal changed a fixed component",
        )

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "rollback" in kwargs["user_prompt"]
    assert "last proposal changed a fixed component" in kwargs["user_prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_tuner_agent.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.tuner'`.

- [ ] **Step 3: Implement `src/analogcoder/agents/tuner.py`**

```python
from analogcoder.agents._sdk_utils import run_agent
from analogcoder.schemas import TUNER_SCHEMA

TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Given the
circuit's structural analysis, the judge's pass/fail verdict, the history of past
tuning attempts in this run, and (if present) feedback on why your last proposal
was rejected, propose specific component parameter changes to fix the failing
criteria. Only propose changes to parameters listed in tunable_params. Respond via
the structured output schema."""


async def propose_tuning(
    analysis: dict,
    judge_result: dict,
    history: list[dict],
    rejection_feedback: str | None,
) -> dict:
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result: {judge_result}\n"
        f"Past attempts this run: {history}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=TUNER_SCHEMA,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_tuner_agent.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/tuner.py tests/unit/test_tuner_agent.py
git commit -m "feat: add tuning agent"
```

---

## Task 12: Verifier Agent (pre-review and post-verification)

**Files:**
- Create: `src/analogcoder/agents/verifier.py`
- Test: `tests/unit/test_verifier_agent.py`

**Interfaces:**
- Consumes: `run_agent` (Task 7), `VERIFIER_PRE_SCHEMA`, `VERIFIER_POST_SCHEMA` (Task 6).
- Produces: `async verify_pre(analysis: dict, judge_result: dict, proposal: dict) -> dict` matching `VERIFIER_PRE_SCHEMA`; `async verify_post(prev_judge_result: dict, new_judge_result: dict, applied_changes: list[dict]) -> dict` matching `VERIFIER_POST_SCHEMA`. Both consumed by Task 14 (orchestrator).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_verifier_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.verifier import verify_post, verify_pre


@pytest.mark.asyncio
async def test_verify_pre_calls_run_agent_with_proposal():
    fake_result = {"approved": True, "concerns": [], "feedback": "reasonable"}
    with patch("analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await verify_pre(
            analysis={"circuit_type": "inverting amplifier"},
            judge_result={"overall_pass": False},
            proposal={"proposed_changes": [{"refdes": "Rf", "param": "value", "new_value": "11k"}]},
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["approved", "concerns", "feedback"]


@pytest.mark.asyncio
async def test_verify_post_calls_run_agent_with_before_after_judge_results():
    fake_result = {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "gain fixed"}
    with patch("analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await verify_post(
            prev_judge_result={"overall_pass": False},
            new_judge_result={"overall_pass": True},
            applied_changes=[{"refdes": "Rf", "param": "value", "new_value": "11k"}],
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["improved", "regressed_criteria", "recommendation", "feedback"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_verifier_agent.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.agents.verifier'`.

- [ ] **Step 3: Implement `src/analogcoder/agents/verifier.py`**

```python
from analogcoder.agents._sdk_utils import run_agent
from analogcoder.schemas import VERIFIER_POST_SCHEMA, VERIFIER_PRE_SCHEMA

VERIFIER_SYSTEM_PROMPT = """You are a skeptical senior reviewer for analog circuit
tuning decisions. You check whether a proposed or applied change is justified by
the circuit analysis and simulation results, and whether it could cause unintended
side effects on other criteria."""


async def verify_pre(analysis: dict, judge_result: dict, proposal: dict) -> dict:
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result before tuning: {judge_result}\n"
        f"Proposed changes: {proposal}\n"
        "Decide whether to approve this proposal before it is applied."
    )
    return await run_agent(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VERIFIER_PRE_SCHEMA,
    )


async def verify_post(prev_judge_result: dict, new_judge_result: dict, applied_changes: list[dict]) -> dict:
    user_prompt = (
        f"Judge result before tuning: {prev_judge_result}\n"
        f"Judge result after applying and re-simulating: {new_judge_result}\n"
        f"Applied changes: {applied_changes}\n"
        "Decide whether the change should be kept or rolled back."
    )
    return await run_agent(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=VERIFIER_POST_SCHEMA,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_verifier_agent.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/verifier.py tests/unit/test_verifier_agent.py
git commit -m "feat: add verifier agent with pre-review and post-verification modes"
```

---

## Task 13: Run state (netlist versioning + history log)

**Files:**
- Create: `src/analogcoder/state.py`
- Test: `tests/unit/test_state.py`

**Interfaces:**
- Produces: `RunState(run_dir: str)` with `push_netlist_version(text: str) -> str`, `current_netlist_path() -> str`, `rollback() -> str`, `log_event(step: str, data: dict) -> None`. Consumed by Task 14 (orchestrator) and Task 16 (CLI).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_state.py
import json
import os

import pytest

from analogcoder.state import RunState


def test_push_netlist_version_writes_versioned_files(tmp_path):
    state = RunState(run_dir=str(tmp_path))

    v0_path = state.push_netlist_version("* v0\n.end\n")
    v1_path = state.push_netlist_version("* v1\n.end\n")

    assert os.path.basename(v0_path) == "netlist_v0.cir"
    assert os.path.basename(v1_path) == "netlist_v1.cir"
    assert state.current_netlist_path() == v1_path
    with open(v1_path) as f:
        assert f.read() == "* v1\n.end\n"


def test_rollback_returns_to_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path))
    v0_path = state.push_netlist_version("* v0\n.end\n")
    state.push_netlist_version("* v1\n.end\n")

    restored_path = state.rollback()

    assert restored_path == v0_path
    assert state.current_netlist_path() == v0_path


def test_rollback_raises_when_no_previous_version(tmp_path):
    state = RunState(run_dir=str(tmp_path))
    state.push_netlist_version("* v0\n.end\n")

    with pytest.raises(ValueError):
        state.rollback()


def test_log_event_appends_jsonl(tmp_path):
    state = RunState(run_dir=str(tmp_path))
    state.log_event("judge", {"overall_pass": False})
    state.log_event("judge", {"overall_pass": True})

    with open(state.history_path) as f:
        lines = [json.loads(line) for line in f]

    assert lines[0] == {"step": "judge", "overall_pass": False}
    assert lines[1] == {"step": "judge", "overall_pass": True}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_state.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.state'`.

- [ ] **Step 3: Implement `src/analogcoder/state.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_state.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/state.py tests/unit/test_state.py
git commit -m "feat: add RunState for netlist versioning and history logging"
```

---

## Task 14: Orchestrator loop

**Files:**
- Create: `src/analogcoder/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `RunState` (Task 13), `apply_changes` (Task 2).
- Produces: `OrchestratorAgents` dataclass with fields `analyze, simulate, judge, tune, verify_pre, verify_post` (all async callables); `async run_orchestration(initial_netlist_text: str, spec, state: RunState, agents: OrchestratorAgents) -> dict` returning `{"status": "PASS"|"FAIL", "final_netlist_path": str, "iterations_used": int, "final_criteria": list, "failure_reason"?: str}`. `spec` only needs a `.criteria` attribute for this task (the real `TargetSpec` from Task 3 satisfies this). Consumed by Task 16 (CLI) and Task 17 (integration test).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_orchestrator.py
from types import SimpleNamespace

import pytest

from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.state import RunState

PASS_JUDGE = {"overall_pass": True, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}]}
FAIL_JUDGE = {"overall_pass": False, "criteria": [{"name": "gain", "target": ">=19.5", "actual": 18.0, "pass": False, "margin": -1.5}]}

FAKE_SPEC = SimpleNamespace(criteria=[])
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}


def make_agents(**overrides):
    async def default_analyze(netlist_text):
        return {"circuit_type": "inverting amplifier"}

    async def default_simulate(netlist_text, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(analysis, judge_result, history, rejection_feedback):
        return FAKE_PROPOSAL

    async def default_verify_pre(analysis, judge_result, proposal):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    defaults = dict(
        analyze=default_analyze,
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
    )
    defaults.update(overrides)
    return OrchestratorAgents(**defaults)


@pytest.mark.asyncio
async def test_immediate_pass_returns_pass_on_first_iteration(tmp_path):
    agents = make_agents()
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1


@pytest.mark.asyncio
async def test_fail_then_pass_after_tuning(tmp_path):
    judge_calls = {"count": 0}

    async def judge_fails_then_passes(measurements, spec):
        judge_calls["count"] += 1
        return FAIL_JUDGE if judge_calls["count"] == 1 else PASS_JUDGE

    agents = make_agents(judge=judge_fails_then_passes)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert len(state.netlist_versions) == 2  # v0 initial + v1 after applied tuning


@pytest.mark.asyncio
async def test_prereview_always_rejected_fails_run(tmp_path):
    async def always_reject(analysis, judge_result, proposal):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_pre=always_reject)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


async def _async(value):
    return value


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
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 2


@pytest.mark.asyncio
async def test_max_iterations_exhausted_fails_run(tmp_path):
    agents = make_agents(judge=lambda m, s: _async(FAIL_JUDGE), verify_post=lambda p, n, c: _async(
        {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no progress"}
    ))
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_orchestrator.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.orchestrator'`.

- [ ] **Step 3: Implement `src/analogcoder/orchestrator.py`**

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.netlist import apply_changes
from analogcoder.state import RunState

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3


@dataclass
class OrchestratorAgents:
    analyze: Callable
    simulate: Callable
    judge: Callable
    tune: Callable
    verify_pre: Callable
    verify_post: Callable


def _final_result(status: str, state: RunState, iterations_used: int, judge_result: dict, failure_reason: str | None = None) -> dict:
    result = {
        "status": status,
        "final_netlist_path": state.current_netlist_path(),
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"],
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


async def run_orchestration(initial_netlist_text: str, spec, state: RunState, agents: OrchestratorAgents) -> dict:
    state.push_netlist_version(initial_netlist_text)
    analysis = await agents.analyze(initial_netlist_text)
    state.log_event("analysis", analysis)

    tuning_history: list[dict] = []
    judge_result: dict = {}

    for outer_iter in range(1, MAX_OUTER_ITERATIONS + 1):
        with open(state.current_netlist_path()) as f:
            netlist_text = f.read()

        sim_result = await agents.simulate(netlist_text, spec)
        state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

        judge_result = await agents.judge(sim_result["measurements"], spec)
        state.log_event("judge", {"outer_iter": outer_iter, **judge_result})

        if judge_result["overall_pass"]:
            return _final_result("PASS", state, outer_iter, judge_result)

        approved_proposal = None
        rejection_feedback = None
        for retry in range(1, MAX_TUNING_RETRIES + 1):
            proposal = await agents.tune(analysis, judge_result, tuning_history, rejection_feedback)
            state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

            review = await agents.verify_pre(analysis, judge_result, proposal)
            state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

            if review["approved"]:
                approved_proposal = proposal
                break
            rejection_feedback = review["feedback"]

        if approved_proposal is None:
            return _final_result(
                "FAIL", state, outer_iter, judge_result, failure_reason="tuning proposal repeatedly rejected"
            )

        new_netlist_text = apply_changes(netlist_text, approved_proposal["proposed_changes"])
        state.push_netlist_version(new_netlist_text)

        new_sim_result = await agents.simulate(new_netlist_text, spec)
        state.log_event("simulation", {"outer_iter": outer_iter, "post_tuning": True, **new_sim_result})

        new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
        state.log_event("judge", {"outer_iter": outer_iter, "post_tuning": True, **new_judge_result})

        post_review = await agents.verify_post(judge_result, new_judge_result, approved_proposal["proposed_changes"])
        state.log_event("verify_post", {"outer_iter": outer_iter, **post_review})

        tuning_history.append({
            "outer_iter": outer_iter,
            "proposal": approved_proposal,
            "recommendation": post_review["recommendation"],
        })

        if post_review["recommendation"] == "rollback":
            state.rollback()
            judge_result = new_judge_result
            continue

        if new_judge_result["overall_pass"]:
            return _final_result("PASS", state, outer_iter, new_judge_result)

        judge_result = new_judge_result

    return _final_result("FAIL", state, MAX_OUTER_ITERATIONS, judge_result, failure_reason="max iterations reached")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_orchestrator.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: add deterministic orchestration loop"
```

---

## Task 15: Report generator

**Files:**
- Create: `src/analogcoder/report.py`
- Test: `tests/unit/test_report.py`

**Interfaces:**
- Consumes: the `dict` shape returned by `run_orchestration` (Task 14).
- Produces: `write_result_json(run_dir: str, result: dict) -> str`, `write_report_md(run_dir: str, result: dict) -> str`. Consumed by Task 16 (CLI).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_report.py
import json
import os

from analogcoder.report import write_report_md, write_result_json

SAMPLE_RESULT = {
    "status": "PASS",
    "final_netlist_path": "runs/abc123/netlist_v1.cir",
    "iterations_used": 2,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
}

SAMPLE_FAIL_RESULT = {
    "status": "FAIL",
    "final_netlist_path": "runs/abc123/netlist_v3.cir",
    "iterations_used": 10,
    "final_criteria": [{"name": "gain", "target": ">=19.5", "actual": 15.0, "pass": False, "margin": -4.5}],
    "failure_reason": "max iterations reached",
}


def test_write_result_json(tmp_path):
    path = write_result_json(str(tmp_path), SAMPLE_RESULT)
    assert os.path.basename(path) == "result.json"
    with open(path) as f:
        assert json.load(f) == SAMPLE_RESULT


def test_write_report_md_includes_status_and_criteria(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_RESULT)
    with open(path) as f:
        content = f.read()
    assert "PASS" in content
    assert "gain" in content
    assert "[PASS] gain" in content


def test_write_report_md_includes_failure_reason(tmp_path):
    path = write_report_md(str(tmp_path), SAMPLE_FAIL_RESULT)
    with open(path) as f:
        content = f.read()
    assert "max iterations reached" in content
    assert "[FAIL] gain" in content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_report.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.report'`.

- [ ] **Step 3: Implement `src/analogcoder/report.py`**

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
        f"**Final netlist:** `{result['final_netlist_path']}`",
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

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_report.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/report.py tests/unit/test_report.py
git commit -m "feat: add result.json and report.md generation"
```

---

## Task 16: CLI entrypoint

**Files:**
- Create: `src/analogcoder/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 2–15 (`load_spec`, `RunState`, `NgspiceBackend`, `run_orchestration`/`OrchestratorAgents`, `write_result_json`/`write_report_md`, all five agent modules).
- Produces: `build_arg_parser() -> argparse.ArgumentParser`, `main() -> None` (registered as the `analogcoder` console script), `async _run(args) -> dict` (the testable core, separated from `sys.exit`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli.py
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.cli import _run, build_arg_parser


def test_arg_parser_requires_netlist_and_spec():
    parser = build_arg_parser()
    args = parser.parse_args(["--netlist", "n.cir", "--spec", "s.yaml"])
    assert args.netlist == "n.cir"
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    netlist_path = tmp_path / "netlist.cir"
    netlist_path.write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\nanalyses: [\"ac\"]\ncontrol_block: |\n  .control\n  .endc\ncriteria: []\n"
    )

    fake_result = {
        "status": "PASS",
        "final_netlist_path": str(tmp_path / "runs" / "r1" / "netlist_v0.cir"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--netlist", str(netlist_path), "--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)):
        result = await _run(args)

    assert result == fake_result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_cli.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.cli'`.

- [ ] **Step 3: Implement `src/analogcoder/cli.py`**

```python
import argparse
import asyncio
import os
import sys
import uuid

from analogcoder.agents.analyzer import analyze_netlist
from analogcoder.agents.judge import judge_measurements
from analogcoder.agents.simulator_agent import simulate as agent_simulate
from analogcoder.agents.tuner import propose_tuning
from analogcoder.agents.verifier import verify_post, verify_pre
from analogcoder.orchestrator import OrchestratorAgents, run_orchestration
from analogcoder.report import write_report_md, write_result_json
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec
from analogcoder.state import RunState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analogcoder")
    parser.add_argument("--netlist", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--simulator", choices=["ngspice"], default="ngspice")
    parser.add_argument("--run-dir", default=None)
    return parser


async def _run(args) -> dict:
    with open(args.netlist) as f:
        netlist_text = f.read()
    spec = load_spec(args.spec)

    run_dir = args.run_dir or os.path.join("runs", uuid.uuid4().hex[:8])
    state = RunState(run_dir=run_dir)
    backend = NgspiceBackend()

    async def simulate_fn(current_netlist_text, spec_arg):
        return await agent_simulate(state.current_netlist_path(), spec_arg.control_block, backend)

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.criteria)

    agents = OrchestratorAgents(
        analyze=analyze_netlist,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=propose_tuning,
        verify_pre=verify_pre,
        verify_post=verify_post,
    )

    return await run_orchestration(netlist_text, spec, state, agents)


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(_run(args))

    run_dir = os.path.dirname(result["final_netlist_path"])
    write_result_json(run_dir, result)
    write_report_md(run_dir, result)

    print(f"Status: {result['status']}")
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_cli.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/cli.py tests/unit/test_cli.py
git commit -m "feat: add CLI entrypoint wiring orchestrator, agents, and report generation"
```

---

## Task 17: End-to-end integration test

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_end_to_end.py`

**Interfaces:**
- Consumes: `run_orchestration`/`OrchestratorAgents` (Task 14), `RunState` (Task 13), `NgspiceBackend` (Task 4), `judge_measurements`/`evaluate_criteria` (Tasks 5, 10), the `benchmarks/inverting_amp` fixture (Task 4), `apply_changes` (Task 2), `load_spec` (Task 3).
- Produces: no new production code — this validates that all prior tasks wire together correctly end-to-end against real ngspice, with the LLM-backed analyze/tune/verify agents replaced by deterministic fakes (no network access needed to run this suite).

- [ ] **Step 1: Write the end-to-end test**

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
    with open(os.path.join(BENCHMARK_DIR, "netlist.cir")) as f:
        netlist_text = f.read()

    state = RunState(run_dir=str(tmp_path))
    backend = NgspiceBackend()

    # The real simulation agent needs a live netlist path on disk, which only
    # exists once the orchestrator has pushed a version — so route it through
    # state.current_netlist_path() exactly like the CLI does in Task 16.
    async def simulate_fn(current_netlist_text, spec_arg):
        return await agent_simulate(state.current_netlist_path(), spec_arg.control_block, backend)

    async def judge_fn(measurements, spec_arg):
        return await judge_measurements(measurements, spec_arg.criteria)

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
    )

    # These two calls hit the real Claude Agent SDK (simulate_fn -> agent_simulate,
    # judge_fn -> judge_measurements). If ANTHROPIC_API_KEY / SDK auth is not
    # configured in this environment, skip rather than fail the suite.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("requires a configured Claude Agent SDK credential to run live agents")

    result = await run_orchestration(netlist_text, spec, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 1
    assert result["final_criteria"][0]["pass"] is True
```

- [ ] **Step 2: Run the test to see it either pass or skip**

```bash
touch tests/integration/__init__.py
pytest tests/integration/test_end_to_end.py -v
```

Expected: if `ANTHROPIC_API_KEY` (or whatever credential the installed `claude-agent-sdk` version expects) is not set, the test SKIPs with the message above — this is expected in an offline dev environment. If credentials are configured, expected: 1 passed.

- [ ] **Step 3: Run the full test suite to confirm nothing else regressed**

```bash
pytest -q
```

Expected: all unit tests from Tasks 2–16 pass; the integration test passes or skips per Step 2.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_end_to_end.py
git commit -m "test: add end-to-end integration test for the inverting-amp benchmark"
```
