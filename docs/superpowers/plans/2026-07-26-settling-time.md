# Settling Time Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth `two_stage_opamp` testbench (`settling_time`) verifying closed-loop step-response settling time, alongside the existing `ac_loop_gain`/`psr_plus`/`psr_minus` testbenches.

**Architecture:** Pure data addition — a new self-contained netlist file (`netlist_settling.cir`, unity-gain-buffer wiring + `.tran` step) and a new `testbenches` entry in `benchmarks/two_stage_opamp/spec.yaml`. No source code changes: the multi-testbench orchestrator/simulator/judge infrastructure built for the PSR feature already handles any number of testbenches with any `.control` block content, since `TargetSpec.analyses` is purely descriptive metadata never read outside `spec.py`.

**Tech Stack:** YAML (spec), SPICE netlist text, pytest with real ngspice (no mocking, matching the existing `test_topology_swap_ngspice.py` / `test_psr_benchmark_ngspice.py` pattern).

## Global Constraints

- `settling_time_hi` and `settling_time_lo` thresholds: both `<= 0.0000012` (1.2μs — the 1μs step time plus a 200ns budget). These are validated values from a real ngspice sweep in the design spec's Validation section — do not change them without re-running it.
- `benchmarks/two_stage_opamp/netlist_settling.cir`'s `OPAMP2STAGE` subckt body must stay byte-identical to `netlist.cir`, `netlist_psr_plus.cir`, and `netlist_psr_minus.cir` at every point — this is the same cross-testbench invariant the PSR feature's tuning-application mechanism already depends on, now spanning a fourth file.
- Do not modify the orchestrator, simulator, judge, `RunState`, or `cli.py` — this feature needs none of them changed.

---

## Task 1: `settling_time` testbench and real-ngspice validation

**Files:**
- Create: `benchmarks/two_stage_opamp/netlist_settling.cir`
- Modify: `benchmarks/two_stage_opamp/spec.yaml`
- Create: `tests/unit/test_settling_benchmark_ngspice.py`

**Interfaces:**
- Consumes: `load_spec` (`src/analogcoder/spec.py`, unchanged), `NgspiceBackend` (`src/analogcoder/simulators/ngspice.py`, unchanged).
- Produces: nothing consumed by later work — this is a self-contained benchmark addition.

- [ ] **Step 1: Create the settling-time testbench netlist**

Create `benchmarks/two_stage_opamp/netlist_settling.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), generic level-1 devices.
* Settling-time testbench: closed-loop unity-gain buffer (vout wired
* directly to vinn via the Xdut instantiation below), fed a 1V step at
* vinp. Unlike the AC loop-gain testbench (which breaks feedback at AC via
* Lfb), this is a genuine closed loop - feedback factor 1, the same
* unity-loop-gain condition the AC testbench's phase_margin criterion
* targets. The OPAMP2STAGE subckt body below must stay byte-identical to
* netlist.cir, netlist_psr_plus.cir, and netlist_psr_minus.cir - tuning
* changes are applied to all four files independently and rely on that.
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
Vss vss 0 DC -2.5

Vinp vinp 0 PULSE(0 1 1u 1n 1n 10u 20u)
Xdut vinp vout vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

Note the `Xdut` line: passing `vout` for both the `vinn` and `vout` subckt
pins ties the inverting input directly to the output — this is the unity-
gain feedback connection, a real wire (not the AC-only `Lfb` approximation
the main testbench uses).

- [ ] **Step 2: Add the `settling_time` testbench to `benchmarks/two_stage_opamp/spec.yaml`**

Append this entry to the `testbenches:` list (the file already has
`ac_loop_gain`, `psr_plus`, and `psr_minus` entries — add this one after
`psr_minus`, keeping the same indentation, and do not modify the existing
three entries):

```yaml
  - name: settling_time
    netlist: netlist_settling.cir
    analyses: ["tran"]
    control_block: |
      .control
      tran 1n 6u
      meas tran t_hi_last WHEN v(vout)=1.00539 CROSS=LAST
      meas tran t_lo_last WHEN v(vout)=0.99539 CROSS=LAST
      .endc
    criteria:
      - name: settling_time_hi
        measurement: t_hi_last
        operator: "<="
        threshold: 0.0000012
        unit: s
      - name: settling_time_lo
        measurement: t_lo_last
        operator: "<="
        threshold: 0.0000012
        unit: s
```

The full file should now have four `testbenches` entries: `ac_loop_gain`,
`psr_plus`, `psr_minus`, `settling_time`.

- [ ] **Step 3: Write the real-ngspice validation test**

This mirrors the existing non-skip-gated real-ngspice pattern already used
for the PSR testbenches (`tests/unit/test_psr_benchmark_ngspice.py`) and
the topology-swap benchmark (`tests/unit/test_topology_swap_ngspice.py`).

Create `tests/unit/test_settling_benchmark_ngspice.py`:

```python
import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def _load_two_stage_opamp_spec():
    return load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))


def test_spec_declares_four_testbenches_including_settling_time():
    spec = _load_two_stage_opamp_spec()

    assert [tb.name for tb in spec.testbenches] == [
        "ac_loop_gain", "psr_plus", "psr_minus", "settling_time",
    ]

    settling = next(tb for tb in spec.testbenches if tb.name == "settling_time")
    assert {c.name: (c.measurement, c.operator, c.threshold) for c in settling.criteria} == {
        "settling_time_hi": ("t_hi_last", "<=", 0.0000012),
        "settling_time_lo": ("t_lo_last", "<=", 0.0000012),
    }


def test_baseline_netlist_matches_validated_settling_measurements():
    # These are the real ngspice-46 measurements recorded in
    # docs/superpowers/specs/2026-07-26-settling-time-design.md's Validation
    # section for the unmodified benchmark netlist (Cc=2p baseline):
    # t_hi_last ~= 1.02823e-6, t_lo_last ~= 1.03563e-6 (both well inside the
    # 1.2e-6 threshold). This test exists to catch unintentional drift in
    # the committed .cir file - not to re-derive the thresholds.
    spec = _load_two_stage_opamp_spec()
    settling = next(tb for tb in spec.testbenches if tb.name == "settling_time")
    backend = NgspiceBackend()

    result = backend.run(settling.netlist_path, {"control_block": settling.control_block})

    assert result.status == "success"
    assert 1.0e-6 <= result.measurements["t_hi_last"] <= 1.1e-6
    assert 1.0e-6 <= result.measurements["t_lo_last"] <= 1.1e-6


def test_settling_subckt_body_matches_other_three_testbenches():
    # Enforces the invariant this whole multi-testbench feature depends on:
    # tuning changes applied independently to each testbench file only stay
    # consistent if the OPAMP2STAGE subckt text is byte-identical across all
    # four two_stage_opamp testbenches.
    spec = _load_two_stage_opamp_spec()
    bodies = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            text = f.read()
        start = text.index(".subckt OPAMP2STAGE")
        end = text.index(".ends OPAMP2STAGE") + len(".ends OPAMP2STAGE")
        bodies[tb.name] = text[start:end]

    assert (
        bodies["ac_loop_gain"]
        == bodies["psr_plus"]
        == bodies["psr_minus"]
        == bodies["settling_time"]
    )
```

- [ ] **Step 4: Run the new test to verify it passes against the real files**

Run: `.venv/bin/python -m pytest tests/unit/test_settling_benchmark_ngspice.py -v`
Expected: PASS (3 tests). If `test_baseline_netlist_matches_validated_settling_measurements`
fails, re-check `netlist_settling.cir` against Step 1 exactly — the most
likely error is a typo in the `PULSE(...)` parameters or the `Xdut` pin
wiring (`vout vout` for the last two positional pins, not `vinn vout`).

- [ ] **Step 5: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: All tests pass (145 — the 142 from the PSR feature plus these 3 new ones).

- [ ] **Step 6: Commit**

```bash
git add benchmarks/two_stage_opamp/netlist_settling.cir benchmarks/two_stage_opamp/spec.yaml tests/unit/test_settling_benchmark_ngspice.py
git commit -m "feat: add settling-time testbench to the two_stage_opamp benchmark"
```

---

## Post-plan manual validation (not automated)

This feature's real end-to-end proof is deferred and combined with the
PSR feature's own deferred end-to-end validation, per explicit user
request — run both together in one real orchestration pass once this task
lands:

```bash
.venv/bin/analogcoder --spec benchmarks/two_stage_opamp/spec.yaml --run-dir runs/psr_and_settling_validation_1
```

Check `runs/psr_and_settling_validation_1/result.json` for all 7 criteria
(dc_gain, unity_gain_bandwidth, phase_margin, psr_plus, psr_minus,
settling_time_hi, settling_time_lo) passing, and `history.jsonl` for
`area_check` events and any `verify_post` rollback showing a change to one
testbench's criterion caught a regression in another's.
