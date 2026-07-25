# PSR Verification — Design

## Problem

analogcoder currently verifies exactly one testbench per benchmark: an AC
loop-gain-breaking configuration producing `dc_gain`, `unity_gain_bandwidth`,
and `phase_margin`. There is no way to add **Power Supply Rejection (PSR)** —
a different AC analysis with the stimulus on a supply rail instead of the
input — without either bolting it onto the existing testbench (impossible;
SPICE AC analysis can't superpose an input stimulus and a supply stimulus in
one run) or running it as a disconnected, unverified side-channel.

This adds PSR as a **second and third testbench** (`psr_plus`, `psr_minus`)
alongside the existing AC loop-gain testbench, with the orchestrator
extended to keep every testbench's criteria passing together — so tuning
for PSR can never silently regress phase margin (or vice versa) the way an
early hand-swept experiment during this design's validation reproduced on
purpose (see Validation below).

Settling time (a `.tran` closed-loop testbench) and PVT-corner-aware
verification are explicitly out of scope for this spec — separate,
independently-scoped future work (see project memory
`project_psr_settling_scope` and `project_pvt_corner_future`).

## Scope (v1)

- **PSR definition**: raw open-loop supply-to-output gain in dB, matching
  how the user's company measures it — inject `AC 1` on `Vdd` (PSR+) or
  `Vss` (PSR-) with no input stimulus, read `vdb(vout)` directly. Not a
  ratio against differential gain (`Adm/Ann`) — two independent criteria,
  `psr_plus` and `psr_minus`, each with its own threshold.
- **Single nominal PVT corner**, matching the project's current scope
  everywhere else. No corner sweeping in this feature.
- **Full re-simulate-and-rejudge every iteration, across all testbenches** —
  extends the existing `verify_post` philosophy (already always re-simulates
  and re-judges the whole criteria set after a change) from one testbench to
  N. This is what catches an M6.W tuning change that fixes `psr_minus` but
  regresses `phase_margin` — verified to actually happen with this circuit,
  see Validation.
- Only `judge`/`tune`/`verify_pre`/`verify_post` stay at one LLM call per
  iteration (unchanged call count) by aggregating measurements/criteria
  across testbenches before calling them. `simulate` is called once per
  testbench (3x for this benchmark) since each is a genuinely different
  SPICE run.
- **Full netlist duplication per testbench** (not `.include`-based sharing).
  Each testbench is a self-contained `.cir` file; the `OPAMP2STAGE` subckt
  body is byte-identical across all three at every point in the run. Tuning
  changes are applied independently to each file via the existing
  `apply_changes`, relying on that invariant rather than enforcing it
  structurally.
- **`spec.yaml` becomes a list of testbenches.** This is a breaking schema
  change — no dual old/new format support. Every existing spec (both
  `inverting_amp` and `two_stage_opamp`, including
  `spec_topology_required.yaml`) migrates to the new format as part of this
  work, even though they still have exactly one testbench each.

## Architecture

### `spec.yaml` schema

```yaml
circuit_name: two_stage_opamp
testbenches:
  - name: ac_loop_gain
    netlist: netlist.cir            # path relative to this spec.yaml
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

The **canonical testbench** for this design is always `testbenches[0]`
(`ac_loop_gain` here) — the one whose netlist text is handed to the tuner,
`verify_pre`, and the area-growth baseline indexer. This is safe only
because of the byte-identical-subckt invariant above; it is not re-derived
per call.

### Testbench netlist files

`netlist_psr_plus.cir` / `netlist_psr_minus.cir` are full copies of
`netlist.cir` with only the stimulus swapped — same `Lfb`/`Cin` AC
loop-break topology, so the amp is characterized under the same AC bias
environment as the main testbench, just with the ripple source moved:

```diff
--- netlist.cir
+++ netlist_psr_plus.cir
@@
- Vdd vdd 0 DC 2.5
+ Vdd vdd 0 DC 2.5 AC 1
@@
- Vstim vstim 0 DC 0 AC 1
+ Vstim vstim 0 DC 0
```

```diff
--- netlist.cir
+++ netlist_psr_minus.cir
@@
- Vss vss 0 DC -2.5
+ Vss vss 0 DC -2.5 AC 1
@@
- Vstim vstim 0 DC 0 AC 1
+ Vstim vstim 0 DC 0
```

The `OPAMP2STAGE` subckt block, `Xdut`, and `Cload` lines are unchanged
text in all three files.

### `src/analogcoder/spec.py` (rewrite)

```python
from dataclasses import dataclass
import os
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
    netlist_path: str          # resolved absolute path
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


def load_spec(path: str) -> TargetSpec:
    with open(path) as f:
        raw = yaml.safe_load(f)

    spec_dir = os.path.dirname(os.path.abspath(path))
    testbenches = []
    for tb in raw["testbenches"]:
        criteria = [
            Criterion(
                name=c["name"],
                measurement=c["measurement"],
                operator=c["operator"],
                threshold=float(c["threshold"]),
                unit=c.get("unit"),
            )
            for c in tb["criteria"]
        ]
        testbenches.append(
            Testbench(
                name=tb["name"],
                netlist_path=os.path.join(spec_dir, tb["netlist"]),
                analyses=tb["analyses"],
                control_block=tb["control_block"],
                criteria=criteria,
            )
        )

    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches)
```

### `src/analogcoder/state.py` (rewrite `RunState` for multi-testbench)

Netlist versioning becomes per-testbench, but always advances in lockstep —
every `push_netlist_version` call writes one file per testbench at the same
version number, and `rollback` pops all testbenches together. This keeps
the byte-identical-subckt invariant intact by construction: there is no way
to advance one testbench's version without the others.

```python
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

### `src/analogcoder/orchestrator.py` (modify)

`run_orchestration`'s first parameter changes from `initial_netlist_text:
str` to `initial_netlist_texts: dict[str, str]`. Every place that currently
reads/writes a single netlist text now operates on the dict, applying the
same per-testbench operation to each entry:

- `state.push_netlist_version(initial_netlist_texts)` (was: single text).
- `baseline_components = index_baseline_components(initial_netlist_texts[spec.canonical.name])`
  — canonical only, per Scope.
- `topology_swap_available = len(parse_netlist(initial_netlist_texts[spec.canonical.name]).subckts) == 1`.
- Each outer iteration: `netlist_texts = state.current_netlist_texts()`;
  `sim_result = await agents.simulate(netlist_texts, spec)` (fan-out to N
  testbenches happens inside the `simulate_fn` closure in `cli.py`, not
  here — orchestrator.py stays testbench-count-agnostic beyond "the dict has
  N entries").
- `agents.tune(analysis, judge_result, tuning_history, rejection_feedback, netlist_texts[spec.canonical.name])`
  and `agents.verify_pre(..., netlist_texts[spec.canonical.name])` — canonical
  only.
- Applying an approved proposal:
  ```python
  new_netlist_texts = {
      name: apply_changes(text, approved_proposal["proposed_changes"])
      for name, text in netlist_texts.items()
  }
  state.push_netlist_version(new_netlist_texts)
  ```
- Topology swap path: same per-testbench-dict transform using
  `apply_topology_swap`, with `subckt_name` derived once from the canonical
  text (identical name in all three files).
- `_final_result`: `"final_netlist_paths": state.current_netlist_paths()`
  (dict, replaces the singular `"final_netlist_path"`), plus
  `"run_dir": state.run_dir"` so `cli.main()` no longer needs to derive the
  run directory from a netlist path.

No change to `check_area_growth`, `apply_changes`, `apply_topology_swap`,
`judge_measurements`, or any agent module's internals — all of them already
operate on a single netlist/measurement dict and are simply called against
the canonical text or the merged dict, per above.

### `src/analogcoder/cli.py` (modify)

- Remove `--netlist` argument entirely; `--spec` is now the only circuit
  input, and `Testbench.netlist_path` (resolved relative to the spec file)
  supplies every `.cir` path.
- `_run`: build `initial_netlist_texts = {tb.name: open(tb.netlist_path).read() for tb in spec.testbenches}`;
  `state = RunState(run_dir=run_dir, testbench_names=[tb.name for tb in spec.testbenches])`.
- `simulate_fn` fans out to the simulator agent once per testbench and
  merges the results:
  ```python
  async def simulate_fn(netlist_texts, spec_arg):
      merged_measurements = {}
      by_testbench = {}
      paths = state.current_netlist_paths()
      for tb in spec_arg.testbenches:
          result = await agent_simulate(paths[tb.name], tb.control_block, sim_backend, agent_backend)
          merged_measurements.update(result["measurements"])
          by_testbench[tb.name] = result
      return {"measurements": merged_measurements, "by_testbench": by_testbench}
  ```
  (`by_testbench` is carried through only for `state.log_event` visibility
  in `history.jsonl` — orchestrator.py doesn't need to read it.)
- `judge_fn` aggregates criteria across testbenches before calling the
  unchanged `judge_measurements`:
  ```python
  async def judge_fn(measurements, spec_arg):
      return await judge_measurements(measurements, spec_arg.all_criteria, agent_backend)
  ```
- `tune_fn`/`verify_pre_fn` are unchanged (already just forward whatever
  netlist text they're given — orchestrator.py now gives them the canonical
  text).
- `main()`: use `result["run_dir"]` instead of
  `os.path.dirname(result["final_netlist_path"])`.

### `src/analogcoder/report.py` (modify)

`write_report_md` iterates `result["final_netlist_paths"]` (dict) instead
of printing a single path:

```python
lines.append("**Final netlists:**")
for name, path in result["final_netlist_paths"].items():
    lines.append(f"- {name}: `{path}`")
```

### Benchmark migration

`benchmarks/inverting_amp/spec.yaml` and
`benchmarks/two_stage_opamp/spec_topology_required.yaml` both move to the
one-testbench-in-a-list form (same `analyses`/`control_block`/`criteria`
content, just nested under a single `testbenches` entry) — no behavior
change for either, just schema compliance.

## Validation

Before fixing thresholds, the PSR testbenches above were hand-built and run
against the real, currently-committed `netlist.cir` with real ngspice
(ngspice-46), confirming both the measurement approach and the thresholds
are meaningful:

**True baseline (`netlist.cir` unmodified, all 5 criteria):**

| Criterion | Value | vs. threshold |
|---|---|---|
| `dc_gain` | 87.03 dB | PASS (≥70) |
| `unity_gain_bandwidth` | 44.33 MHz | PASS (≥20M) |
| `phase_margin` | 50.33° | **FAIL** (≥60°, fails by design, same as today) |
| `psr_plus` | -15.12 dB | PASS (≤-10) |
| `psr_minus` | -3.36 dB | **FAIL** (≤-8) |

So at baseline, two independent criteria need real tuning (`phase_margin`
and `psr_minus`), and one (`psr_plus`) already passes but is not
guaranteed to stay passing — which is exactly the property needed to
validate cross-testbench regression protection.

**Confirmed regression scenario** (the concern that motivated this
feature): increasing `M6.W` (the natural fix for `psr_minus` — it
monotonically improves from -3.36 dB at 40µ to -9.26 dB at 80µ, the
maximum growth the existing area gate allows from a 40µ baseline) while
leaving `Cc` untouched drives `phase_margin` from already-failing further
down (e.g. `M6.W=80µ` alone: `phase_margin` only 60.87°, uncomfortably
tight; combined with an `M7.W` change for `psr_plus`, `phase_margin` drops
to 52.79° and `psr_plus` gets *worse*, not better, at -5.45 dB). A
single-testbench-only rollback check would not have caught this.

**Confirmed a jointly-passing solution exists** within the area gate's
limits, proving the chosen thresholds are tuning-achievable and not
secretly requiring a topology swap: `Cc=3.3p, M6.W=70µ, M7.W=45-50µ` (all
within the area gate's allowed growth from their respective baselines)
yields `dc_gain=90.7dB`, `UGBW=28.2MHz`, `phase_margin=61.6-63.1°`,
`psr_plus=-11.6 to -14.0dB`, `psr_minus=-10.0 to -11.9dB` — all five
criteria passing simultaneously with real margin.

## Testing

1. `tests/unit/test_spec.py` (extend/rewrite) — `load_spec` parses the new
   `testbenches` list, resolves each `netlist` path relative to the spec
   file's directory, `TargetSpec.canonical` returns `testbenches[0]`,
   `TargetSpec.all_criteria` flattens criteria across testbenches.
2. `tests/unit/test_state.py` (extend) — `push_netlist_version` writes one
   file per testbench at a shared version number; `current_netlist_texts`
   reads back the latest version of each; `rollback` pops all testbenches
   together and raises if any testbench lacks a previous version (keeping
   them in lockstep — never partially rolls back).
3. `tests/unit/test_orchestrator.py` (extend, mocked agents) — a mocked
   multi-testbench spec (2 testbenches is enough to exercise the fan-out
   without duplicating the real 3-testbench benchmark): tuning changes are
   applied to every testbench's netlist text; `verify_post` rollback
   restores every testbench's previous version, not just one; the canonical
   testbench's text (not a merged/concatenated one) is what's passed to
   `tune`/`verify_pre`.
4. `tests/unit/test_cli.py` (extend if it exists, else new) —
   `simulate_fn`'s merge behavior: measurements from N testbenches combine
   into one dict with no key collisions (using the real 3-testbench
   `two_stage_opamp` measurement names as the fixture); `judge_fn` receives
   the flattened criteria list from all testbenches.
5. `tests/integration/` — real-ngspice run of the migrated
   `benchmarks/two_stage_opamp` (3 testbenches, thresholds from Validation
   above) is the end-to-end proof, run manually post-implementation the
   same way prior benchmark features were validated in this project (not
   part of the automated suite, per existing convention for real-backend
   tests).
