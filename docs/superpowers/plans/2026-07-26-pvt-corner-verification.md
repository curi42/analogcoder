# PVT Corner Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full 45-corner (5 process × 3 voltage × 3 temperature) PVT sweep verification for `two_stage_opamp`, via a new `spec_pvt.yaml` that reuses `spec.yaml`'s netlists/thresholds unchanged, wrapping (not modifying) the existing nominal-only orchestrator loop.

**Architecture:** A new `src/analogcoder/pvt.py` module renders per-corner netlist text (process/voltage/temperature substitution, verified directly against real ngspice during planning — not left as an open question), runs each corner deterministically via `SimulatorBackend` (no LLM), and aggregates worst-case-per-criterion results using the existing deterministic `evaluate_criteria` (`judge_tools.py`) — the same function the LLM judge agent already calls as a tool, reused directly here with no LLM involved. `cli.py`'s `_run()` calls this sweep once before and once after `run_orchestration`, which itself is untouched.

**Tech Stack:** SPICE netlist text (regex-based corner substitution), Python 3, pytest with real ngspice (no mocking — matches this project's existing `test_*_ngspice.py` pattern).

## Global Constraints

- 5 process corners: `tt`, `ss`, `ff`, `sf`, `fs` (sky130's full standard set — confirmed each corner's nfet/pfet files pair by matching suffix, e.g. `.lib sf` in the vendored PDK's `models/sky130.lib.spice` includes both `nfet_01v8__sf.corner.spice` and `pfet_01v8__sf.corner.spice`).
- 3 voltage points: `1.62`, `1.8`, `1.98` (±10% of `Vdd=1.8V`).
- 3 temperature points: `-40`, `27`, `125` (°C).
- MiM capacitor RC corner is out of scope — stays fixed at `typical` (i.e. the existing `pdk_corner*.inc` MiM param block, unchanged) for every combination.
- `spec_pvt.yaml` reuses `spec.yaml`'s four testbenches (`netlist:`, `control_block:`, `criteria:`) byte-for-byte — same thresholds, no new/duplicated netlist files. `spec.yaml` and `spec_topology_required.yaml` are not modified.
- Corner sweep simulations are deterministic — call `SimulatorBackend.run()` directly, never the LLM-based simulator agent (`agents/simulator_agent.py`).
- `orchestrator.py` (`run_orchestration` and everything it calls) is not modified. Mid-loop tuning iterations stay nominal-only (`tt`/`1.8V`/`27°C`), exactly as they behave today when `spec.pvt_corners` is absent.
- A final full-PVT-sweep failure, even after the nominal-only loop converged to `PASS`, must be reported as `FAIL` with the per-corner diagnostic breakdown attached — never silently promoted to `PASS`.
- PVT-aware tuning, corner-reduction techniques, and automatic re-tuning on final-sweep failure are explicitly out of scope for this plan (deferred to a later sub-project per project memory `project-pvt-corner-future`).

---

## Task 1: `PVTCorners` schema and `spec_pvt.yaml`

**Files:**
- Modify: `src/analogcoder/spec.py`
- Create: `benchmarks/two_stage_opamp/spec_pvt.yaml`
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PVTCorners` dataclass (`process: list[str]`, `voltage: list[float]`, `temperature: list[float]`) and `TargetSpec.pvt_corners: PVTCorners | None` — later tasks read `spec.pvt_corners.process`/`.voltage`/`.temperature` and check `spec.pvt_corners is not None` to detect PVT-enabled specs.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_spec.py` (append after the existing tests, keep existing imports):

```python
def test_load_spec_without_pvt_corners_defaults_to_none(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")

    spec = load_spec(str(spec_path))

    assert spec.pvt_corners is None


def test_pvt_spec_declares_full_45_corner_sweep():
    spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")

    assert spec.pvt_corners is not None
    assert spec.pvt_corners.process == ["tt", "ss", "ff", "sf", "fs"]
    assert spec.pvt_corners.voltage == [1.62, 1.8, 1.98]
    assert spec.pvt_corners.temperature == [-40.0, 27.0, 125.0]


def test_pvt_spec_reuses_baseline_spec_testbenches_and_thresholds():
    spec = load_spec("benchmarks/two_stage_opamp/spec_pvt.yaml")
    baseline = load_spec("benchmarks/two_stage_opamp/spec.yaml")

    assert [tb.name for tb in spec.testbenches] == [tb.name for tb in baseline.testbenches]
    assert [tb.netlist_path for tb in spec.testbenches] == [tb.netlist_path for tb in baseline.testbenches]
    for tb, baseline_tb in zip(spec.testbenches, baseline.testbenches):
        assert {c.name: (c.operator, c.threshold) for c in tb.criteria} == {
            c.name: (c.operator, c.threshold) for c in baseline_tb.criteria
        }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: `test_load_spec_without_pvt_corners_defaults_to_none` FAILS with `AttributeError: 'TargetSpec' object has no attribute 'pvt_corners'`; the other two FAIL with `FileNotFoundError` (spec_pvt.yaml doesn't exist yet).

- [ ] **Step 3: Add `PVTCorners` to `spec.py`**

In `src/analogcoder/spec.py`, add the dataclass after `Criterion` and before `Testbench`:

```python
@dataclass
class PVTCorners:
    process: list[str]
    voltage: list[float]
    temperature: list[float]
```

Add `pvt_corners: PVTCorners | None = None` to `TargetSpec` (after `testbenches`):

```python
@dataclass
class TargetSpec:
    circuit_name: str
    testbenches: list[Testbench]
    pvt_corners: PVTCorners | None = None
```

Add a loader helper and wire it into `load_spec`:

```python
def _load_pvt_corners(raw: dict) -> PVTCorners | None:
    raw_pvt = raw.get("pvt_corners")
    if raw_pvt is None:
        return None
    return PVTCorners(
        process=raw_pvt["process"],
        voltage=[float(v) for v in raw_pvt["voltage"]],
        temperature=[float(t) for t in raw_pvt["temperature"]],
    )
```

In `load_spec`, change the final line:

```python
    return TargetSpec(circuit_name=raw["circuit_name"], testbenches=testbenches, pvt_corners=_load_pvt_corners(raw))
```

- [ ] **Step 4: Create `benchmarks/two_stage_opamp/spec_pvt.yaml`**

```yaml
circuit_name: two_stage_opamp
pvt_corners:
  process: [tt, ss, ff, sf, fs]
  voltage: [1.62, 1.8, 1.98]
  temperature: [-40, 27, 125]
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
        threshold: 60.0
        unit: dB
      - name: unity_gain_bandwidth
        measurement: ugbw_hz
        operator: ">="
        threshold: 1500000.0
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
        threshold: 0.0
        unit: dB

  - name: settling_time
    netlist: netlist_settling.cir
    analyses: ["tran"]
    control_block: |
      .control
      tran 1n 6u
      meas tran t_hi_last WHEN v(vout)=0.70398 CROSS=LAST
      meas tran t_lo_last WHEN v(vout)=0.69698 CROSS=LAST
      .endc
    criteria:
      - name: settling_time_hi
        measurement: t_hi_last
        operator: "<="
        threshold: 0.0000028
        unit: s
      - name: settling_time_lo
        measurement: t_lo_last
        operator: "<="
        threshold: 0.0000028
        unit: s
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: PASS (8 tests — 5 existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/spec.py benchmarks/two_stage_opamp/spec_pvt.yaml tests/unit/test_spec.py
git commit -m "feat: add PVTCorners schema and spec_pvt.yaml for two_stage_opamp"
```

---

## Task 2: `render_corner_netlist`

**Files:**
- Create: `src/analogcoder/pvt.py`
- Test: `tests/unit/test_pvt.py`

**Interfaces:**
- Consumes: nothing new (pure text transformation, no other project modules).
- Produces: `render_corner_netlist(netlist_text: str, process: str, voltage: float, temperature: float, benchmark_dir: str) -> str` — Task 5 calls this per corner per testbench.

This is a pure text-transformation function verified directly against real ngspice during planning (not an open question) — implement exactly as specified below.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_pvt.py`:

```python
from analogcoder.pvt import render_corner_netlist

NETLIST = """\
* Two-stage CMOS op-amp
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
X1 n1 vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8
Vss vss 0 DC 0
.end
"""

NETLIST_WITH_AC_STIMULUS_ON_VDD = """\
* PSR+ testbench
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
X1 n1 vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8 AC 1
Vss vss 0 DC 0
.end
"""


def test_render_corner_netlist_uses_pdk_corner_inc_unchanged_for_tt():
    rendered = render_corner_netlist(NETLIST, "tt", 1.8, 27, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner.inc"' in rendered


def test_render_corner_netlist_swaps_process_corner_include():
    rendered = render_corner_netlist(NETLIST, "ss", 1.8, 27, "/benchmarks/two_stage_opamp")

    assert '.include "/benchmarks/two_stage_opamp/pdk_corner_ss.inc"' in rendered
    assert "pdk_corner.inc" not in rendered


def test_render_corner_netlist_injects_temp_directive():
    rendered = render_corner_netlist(NETLIST, "tt", 1.8, -40, "/benchmarks/two_stage_opamp")

    assert ".temp -40" in rendered


def test_render_corner_netlist_sets_vdd_dc_value():
    rendered = render_corner_netlist(NETLIST, "tt", 1.62, 27, "/benchmarks/two_stage_opamp")

    vdd_lines = [line for line in rendered.splitlines() if line.startswith("Vdd")]
    assert vdd_lines == ["Vdd vdd 0 DC 1.62"]


def test_render_corner_netlist_preserves_trailing_ac_clause_on_vdd():
    # netlist_psr_plus.cir's Vdd line has a trailing "AC 1" - the voltage
    # substitution must only touch the DC value token, not the AC magnitude.
    rendered = render_corner_netlist(NETLIST_WITH_AC_STIMULUS_ON_VDD, "tt", 1.98, 27, "/benchmarks/two_stage_opamp")

    vdd_lines = [line for line in rendered.splitlines() if line.startswith("Vdd")]
    assert vdd_lines == ["Vdd vdd 0 DC 1.98 AC 1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analogcoder.pvt'`.

- [ ] **Step 3: Implement `render_corner_netlist`**

Create `src/analogcoder/pvt.py`:

```python
import os
import re


def render_corner_netlist(
    netlist_text: str, process: str, voltage: float, temperature: float, benchmark_dir: str
) -> str:
    """Renders netlist_text for one PVT corner: swaps which process-corner
    PDK include file is used, injects a .temp directive, and sets Vdd's DC
    value - all via absolute paths / targeted regexes, not the tuner's
    apply_changes (verified unsafe here: apply_changes's generic positional-
    token targeting would hit the AC magnitude, not the DC value, on a Vdd
    line with a trailing "AC 1" clause, e.g. netlist_psr_plus.cir)."""
    include_name = "pdk_corner.inc" if process == "tt" else f"pdk_corner_{process}.inc"
    abs_include = os.path.join(benchmark_dir, include_name)
    text = netlist_text.replace('.include "pdk_corner.inc"', f'.include "{abs_include}"')

    include_line_pattern = re.compile(r'(\.include "' + re.escape(abs_include) + r'"\n)')
    text = include_line_pattern.sub(lambda m: m.group(1) + f".temp {temperature}\n", text, count=1)

    text = re.sub(r"^(Vdd\s+\S+\s+\S+\s+DC\s+)\S+", rf"\g<1>{voltage}", text, flags=re.MULTILINE)

    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/pvt.py tests/unit/test_pvt.py
git commit -m "feat: add render_corner_netlist for PVT corner text substitution"
```

---

## Task 3: Corner-specific PDK include files

**Files:**
- Create: `benchmarks/two_stage_opamp/pdk_corner_ss.inc`
- Create: `benchmarks/two_stage_opamp/pdk_corner_ff.inc`
- Create: `benchmarks/two_stage_opamp/pdk_corner_sf.inc`
- Create: `benchmarks/two_stage_opamp/pdk_corner_fs.inc`
- Test: `tests/unit/test_pvt_corner_files_ngspice.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: four files `render_corner_netlist` (Task 2) points at for non-`tt` corners; Task 5's sweep depends on these existing and loading cleanly.

- [ ] **Step 1: Create the four corner-specific include files**

Each is byte-identical to `benchmarks/two_stage_opamp/pdk_corner.inc` except the two `nfet_01v8`/`pfet_01v8` corner-file include lines (confirmed by direct execution during planning that sky130 pairs same-suffixed nfet/pfet files, and that the mismatch/lod includes are corner-independent — unchanged in all four).

Create `benchmarks/two_stage_opamp/pdk_corner_ss.inc`:

```
* Corner-specific variant of pdk_corner.inc for the "ss" (slow-slow) process
* corner - part of the PVT corner sweep (see
* docs/superpowers/specs/2026-07-26-pvt-corner-verification-design.md).
* Byte-identical to pdk_corner.inc except the nfet_01v8/pfet_01v8 corner
* include lines below - the mismatch/lod includes are corner-independent
* (confirmed via sky130's own models/sky130.lib.spice, which references the
* identically-named mismatch file from every corner's .lib section).
.option scale=1.0u
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/lod.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__ss.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__ss.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"

* MiM capacitor (Cc/Ca compensation caps) - minimal hand-extracted param
* recipe, not the official include chain. The official chain
* (models/r+c/res_typical__cap_typical.spice ->
* models/sky130_fd_pr__model__r+c.model.spice) pulls in an unrelated
* resistor-cell family not needed here and not in the sparse-checkout;
* these ~10 scalar params were extracted directly from that chain instead.
* Capacitance: C = camimc*w*l + cpmimc*2*(w+l) (verified to ~0.5% accuracy
* against a real ngspice .op run). Fixed at "typical" for every process
* corner - MiM RC corner sweeping is out of scope for this design.
.param tol_m3=0.0
.param rm3=0.047 rcvia3=3.41
.param tc1rm3=3.424e-3 tc2rm3=-7.739e-7
.param tc1rvia3=2.366e-3 tc2rvia3=-1.025e-5
.param m3_dw=-0.025u
.param camimc=2.00e-15 cpmimc=0.19e-15
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_1.model.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_2.model.spice"
```

Create `benchmarks/two_stage_opamp/pdk_corner_ff.inc` — identical to the above except both corner-file lines read `sky130_fd_pr__nfet_01v8__ff.corner.spice` and `sky130_fd_pr__pfet_01v8__ff.corner.spice` (and the header comment's `"ss" (slow-slow)` becomes `"ff" (fast-fast)`).

Create `benchmarks/two_stage_opamp/pdk_corner_sf.inc` — identical except both corner-file lines read `sky130_fd_pr__nfet_01v8__sf.corner.spice` and `sky130_fd_pr__pfet_01v8__sf.corner.spice` (header: `"sf" (slow-fast)`).

Create `benchmarks/two_stage_opamp/pdk_corner_fs.inc` — identical except both corner-file lines read `sky130_fd_pr__nfet_01v8__fs.corner.spice` and `sky130_fd_pr__pfet_01v8__fs.corner.spice` (header: `"fs" (fast-slow)`).

- [ ] **Step 2: Write the real-ngspice smoke tests**

Create `tests/unit/test_pvt_corner_files_ngspice.py`:

```python
import os

import pytest

from analogcoder.simulators.ngspice import NgspiceBackend

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


@pytest.mark.parametrize("corner", ["ss", "ff", "sf", "fs"])
def test_corner_specific_pdk_include_loads_cleanly(tmp_path, corner):
    abs_benchmark_dir = os.path.abspath(BENCHMARK_DIR)
    include_path = os.path.join(abs_benchmark_dir, f"pdk_corner_{corner}.inc")

    smoke_path = tmp_path / "_pvt_corner_smoke_test.cir"
    smoke_path.write_text(
        f"* pdk_corner_{corner}.inc smoke test - not a real benchmark testbench\n"
        f'.include "{include_path}"\n'
        "Vdd vdd 0 DC 1.8\n"
        "Vss vss 0 DC 0\n"
        "Xn n vdd vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4\n"
        "Xp p vss vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=4\n"
        "Xc n p sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1\n"
        ".end\n"
    )

    backend = NgspiceBackend()
    result = backend.run(
        str(smoke_path),
        {"control_block": ".control\ndc Vdd 1.7 1.9 0.1\nmeas dc n_val find v(n) at=1.8\n.endc"},
    )

    assert result.status == "success"
    assert "could not find" not in result.raw_log.lower()
    assert "undefined parameter" not in result.raw_log.lower()
```

- [ ] **Step 3: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt_corner_files_ngspice.py -v`
Expected: PASS (4 tests). If any fails with `"could not find include file"`, re-check that file's two corner-line filenames match the real files under `third_party/skywater-pdk-libs-sky130_fd_pr/cells/{nfet_01v8,pfet_01v8}/` exactly (e.g. `sky130_fd_pr__nfet_01v8__ff.corner.spice`).

- [ ] **Step 4: Commit**

```bash
git add benchmarks/two_stage_opamp/pdk_corner_ss.inc benchmarks/two_stage_opamp/pdk_corner_ff.inc benchmarks/two_stage_opamp/pdk_corner_sf.inc benchmarks/two_stage_opamp/pdk_corner_fs.inc tests/unit/test_pvt_corner_files_ngspice.py
git commit -m "feat: add ss/ff/sf/fs process-corner PDK include files"
```

---

## Task 4: Corner enumeration and worst-case aggregation

**Files:**
- Modify: `src/analogcoder/pvt.py`
- Modify: `tests/unit/test_pvt.py`

**Interfaces:**
- Consumes: `Criterion` (`src/analogcoder/spec.py`), `PVTCorners` (Task 1).
- Produces: `CornerPoint` dataclass (`process: str`, `voltage: float`, `temperature: float`), `all_corners(pvt: PVTCorners) -> list[CornerPoint]`, `worst_case_measurements(corners: list[CornerPoint], per_corner_measurements: list[dict], criteria: list[Criterion]) -> tuple[dict, dict]` — Task 5's `run_full_pvt_sweep` calls both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_pvt.py` (add `from analogcoder.spec import Criterion` and extend the existing `from analogcoder.pvt import render_corner_netlist` import line to also import `CornerPoint`, `all_corners`, `worst_case_measurements`):

```python
from analogcoder.pvt import CornerPoint, all_corners, render_corner_netlist, worst_case_measurements
from analogcoder.spec import Criterion, PVTCorners
```

```python
def test_all_corners_produces_full_cross_product():
    pvt = PVTCorners(process=["tt", "ss"], voltage=[1.62, 1.98], temperature=[-40, 125])

    corners = all_corners(pvt)

    assert len(corners) == 8  # 2 * 2 * 2
    assert CornerPoint(process="tt", voltage=1.62, temperature=-40) in corners
    assert CornerPoint(process="ss", voltage=1.98, temperature=125) in corners


def test_worst_case_measurements_picks_minimum_for_gte_criterion():
    corners = [
        CornerPoint(process="tt", voltage=1.8, temperature=27),
        CornerPoint(process="ss", voltage=1.62, temperature=-40),
    ]
    per_corner_measurements = [{"phase_margin_deg": 62.88}, {"phase_margin_deg": 37.12}]
    criteria = [Criterion(name="phase_margin", measurement="phase_margin_deg", operator=">=", threshold=60.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert measurements == {"phase_margin_deg": 37.12}
    assert worst_corners["phase_margin"]["process"] == "ss"
    assert worst_corners["phase_margin"]["value"] == 37.12


def test_worst_case_measurements_picks_maximum_for_lte_criterion():
    corners = [
        CornerPoint(process="tt", voltage=1.8, temperature=27),
        CornerPoint(process="ff", voltage=1.98, temperature=125),
    ]
    per_corner_measurements = [{"psr_minus_db": -1.43}, {"psr_minus_db": 0.5}]
    criteria = [Criterion(name="psr_minus", measurement="psr_minus_db", operator="<=", threshold=0.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert measurements == {"psr_minus_db": 0.5}
    assert worst_corners["psr_minus"]["process"] == "ff"


def test_worst_case_measurements_skips_criterion_missing_from_all_corners():
    corners = [CornerPoint(process="tt", voltage=1.8, temperature=27)]
    per_corner_measurements = [{"gain_db": 71.09}]
    criteria = [Criterion(name="phase_margin", measurement="phase_margin_deg", operator=">=", threshold=60.0)]

    measurements, worst_corners = worst_case_measurements(corners, per_corner_measurements, criteria)

    assert measurements == {}
    assert worst_corners == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -v`
Expected: FAIL — `ImportError: cannot import name 'CornerPoint' from 'analogcoder.pvt'`.

- [ ] **Step 3: Implement `CornerPoint`, `all_corners`, `worst_case_measurements`**

In `src/analogcoder/pvt.py`, add imports and the new code (keep the existing `render_corner_netlist`):

```python
from dataclasses import dataclass

from analogcoder.spec import Criterion, PVTCorners


@dataclass(frozen=True)
class CornerPoint:
    process: str
    voltage: float
    temperature: float


def all_corners(pvt: PVTCorners) -> list[CornerPoint]:
    return [
        CornerPoint(process=p, voltage=v, temperature=t)
        for p in pvt.process
        for v in pvt.voltage
        for t in pvt.temperature
    ]


def worst_case_measurements(
    corners: list[CornerPoint], per_corner_measurements: list[dict], criteria: list[Criterion]
) -> tuple[dict, dict]:
    """For each criterion, finds the worst-case value across
    per_corner_measurements (parallel to corners) - the minimum observed
    value if the criterion's operator is ">=" or ">", the maximum
    otherwise. Returns (worst_case_measurements, worst_case_corners), where
    worst_case_corners maps each criterion's name to the corner (plus the
    value) that produced its worst case, for diagnostics. A criterion whose
    measurement never appears in any corner's results is skipped (not an
    error here - evaluate_criteria's caller is responsible for treating a
    missing measurement as a failure)."""
    measurements: dict[str, float] = {}
    worst_corners: dict[str, dict] = {}
    for criterion in criteria:
        values_with_corner = [
            (m[criterion.measurement], corner)
            for m, corner in zip(per_corner_measurements, corners)
            if criterion.measurement in m
        ]
        if not values_with_corner:
            continue
        if criterion.operator in (">=", ">"):
            value, corner = min(values_with_corner, key=lambda vc: vc[0])
        else:
            value, corner = max(values_with_corner, key=lambda vc: vc[0])
        measurements[criterion.measurement] = value
        worst_corners[criterion.name] = {
            "process": corner.process,
            "voltage": corner.voltage,
            "temperature": corner.temperature,
            "value": value,
        }
    return measurements, worst_corners
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt.py -v`
Expected: PASS (9 tests — 5 from Task 2 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/pvt.py tests/unit/test_pvt.py
git commit -m "feat: add PVT corner enumeration and worst-case aggregation"
```

---

## Task 5: `run_full_pvt_sweep`

**Files:**
- Modify: `src/analogcoder/pvt.py`
- Test: `tests/unit/test_pvt_sweep_ngspice.py`

**Interfaces:**
- Consumes: `render_corner_netlist`, `all_corners`, `worst_case_measurements` (this file, Tasks 2 & 4), `evaluate_criteria` (`src/analogcoder/judge_tools.py`), `SimulatorBackend` (`src/analogcoder/simulators/base.py`), `TargetSpec` (`src/analogcoder/spec.py`).
- Produces: `run_full_pvt_sweep(netlist_texts: dict[str, str], spec, sim_backend) -> dict` returning `{"overall_pass": bool, "criteria": [...], "summary": str, "worst_case_corners": dict}` (the same shape `evaluate_criteria` returns, plus `worst_case_corners`) — Task 6's `cli.py` wiring calls this.

- [ ] **Step 1: Write the failing real-ngspice test**

Create `tests/unit/test_pvt_sweep_ngspice.py`:

```python
import os

from analogcoder.pvt import run_full_pvt_sweep
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import PVTCorners, load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def test_run_full_pvt_sweep_against_a_small_representative_corner_set():
    # Full 45-corner sweep is exercised manually (see the design spec's
    # Testing section) - this test uses a small, fast, real-ngspice subset
    # (2 corners x 4 testbenches = 8 real simulations) to verify the whole
    # render -> run -> aggregate -> evaluate pipeline end to end.
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec_pvt.yaml"))
    spec.pvt_corners = PVTCorners(process=["tt", "ss"], voltage=[1.8], temperature=[27])

    netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            netlist_texts[tb.name] = f.read()

    result = run_full_pvt_sweep(netlist_texts, spec, NgspiceBackend())

    assert "overall_pass" in result
    assert len(result["criteria"]) == len(spec.all_criteria)
    # phase_margin is one of the criteria this small corner set actually
    # covers (ac_loop_gain testbench) - its worst-case corner must be
    # either tt or ss (the two corners this test swept), never a corner
    # outside the sweep.
    phase_margin_corner = result["worst_case_corners"]["phase_margin"]
    assert phase_margin_corner["process"] in ("tt", "ss")


def test_run_full_pvt_sweep_with_single_point_corner_matches_nominal_baseline():
    # A 1-corner "sweep" (one process, one voltage, one temperature value)
    # is a degenerate but valid case - no special-casing needed in
    # run_full_pvt_sweep. At tt/1.8V/27C, this reduces to the as-committed
    # miller_basic topology baseline - not the post-tuning/post-topology-
    # swap result. The sky130 PDK migration design spec's Validation section
    # documents this baseline failing phase_margin by design (34.56 deg <
    # 60 deg threshold), which is precisely what triggers the orchestrator's
    # topology-swap mechanism during a real tuning run. So overall_pass must
    # be False here.
    spec = load_spec(os.path.join(BENCHMARK_DIR, "spec_pvt.yaml"))
    spec.pvt_corners = PVTCorners(process=["tt"], voltage=[1.8], temperature=[27])

    netlist_texts = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            netlist_texts[tb.name] = f.read()

    result = run_full_pvt_sweep(netlist_texts, spec, NgspiceBackend())

    assert result["overall_pass"] is False
    phase_margin_result = next(c for c in result["criteria"] if c["name"] == "phase_margin")
    assert phase_margin_result["pass"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt_sweep_ngspice.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_full_pvt_sweep' from 'analogcoder.pvt'`.

- [ ] **Step 3: Implement `run_full_pvt_sweep`**

In `src/analogcoder/pvt.py`, add imports and the function (keep everything already in the file from Tasks 2 & 4):

```python
import tempfile

from analogcoder.judge_tools import evaluate_criteria
```

```python
def run_full_pvt_sweep(netlist_texts: dict[str, str], spec, sim_backend) -> dict:
    """Runs spec.pvt_corners' full cross product against every testbench,
    directly via sim_backend (no LLM agent involved - corner variation is
    purely mechanical). Returns the worst-case-per-criterion result in the
    same shape evaluate_criteria() returns, plus a worst_case_corners
    breakdown mapping each criterion's name to the corner that produced its
    worst-case value, for diagnostics."""
    benchmark_dir = os.path.dirname(spec.canonical.netlist_path)
    corners = all_corners(spec.pvt_corners)

    combined_measurements: dict[str, float] = {}
    combined_worst_corners: dict[str, dict] = {}
    for tb in spec.testbenches:
        netlist_text = netlist_texts[tb.name]
        per_corner_measurements = []
        for corner in corners:
            rendered = render_corner_netlist(
                netlist_text, corner.process, corner.voltage, corner.temperature, benchmark_dir
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                netlist_path = os.path.join(tmpdir, "corner.cir")
                with open(netlist_path, "w") as f:
                    f.write(rendered)
                result = sim_backend.run(netlist_path, {"control_block": tb.control_block})
            per_corner_measurements.append(result.measurements)

        tb_measurements, tb_worst_corners = worst_case_measurements(corners, per_corner_measurements, tb.criteria)
        combined_measurements.update(tb_measurements)
        combined_worst_corners.update(tb_worst_corners)

    evaluation = evaluate_criteria(combined_measurements, spec.all_criteria)
    return {**evaluation, "worst_case_corners": combined_worst_corners}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_pvt_sweep_ngspice.py -v`
Expected: PASS (2 tests). This runs 8 + 4 = 12 real ngspice simulations; expect it to take a few seconds, not minutes.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/pvt.py tests/unit/test_pvt_sweep_ngspice.py
git commit -m "feat: add run_full_pvt_sweep, the deterministic corner-sweep orchestration wrapper"
```

---

## Task 6: Wire the sweep into `cli.py`

**Files:**
- Modify: `src/analogcoder/cli.py`
- Modify: `tests/unit/test_cli.py` (an existing file — this task adds to it, following the file's existing `_run()`-wiring test pattern)

**Interfaces:**
- Consumes: `run_full_pvt_sweep` (Task 5), `RunState.current_netlist_texts` (`src/analogcoder/state.py`, unchanged), `run_orchestration` (`src/analogcoder/orchestrator.py`, unchanged).
- Produces: nothing consumed by later tasks — this is the final integration point.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cli.py` (no new import needed — patch `run_full_pvt_sweep` by its string path, matching the file's existing `patch("analogcoder.cli.run_orchestration", ...)` style; keep the existing `from analogcoder.cli import _build_agent_backend, _run, build_arg_parser` import as-is):

```python
@pytest.mark.asyncio
async def test_run_skips_pvt_sweep_when_spec_has_no_pvt_corners(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r3")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {}, "run_dir": str(tmp_path / "runs" / "r3"),
        "iterations_used": 1, "final_criteria": [],
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep") as mock_sweep,
    ):
        result = await _run(args)

    mock_sweep.assert_not_called()
    assert result["status"] == "PASS"
    assert "pvt_sweep" not in result


@pytest.mark.asyncio
async def test_run_overrides_pass_to_fail_when_final_pvt_sweep_fails(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r4")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r4"), "iterations_used": 1, "final_criteria": [],
    }
    fake_final_sweep = {
        "overall_pass": False, "criteria": [], "summary": "one or more criteria failed",
        "worst_case_corners": {"gain": {"process": "tt", "voltage": 1.8, "temperature": 27, "value": 5.0}},
    }

    # run_orchestration is mocked out entirely, so it never calls
    # state.push_netlist_version - RunState.current_netlist_texts() then
    # naturally returns {} (no versions tracked), which is fine here since
    # run_full_pvt_sweep is also mocked and ignores its netlist_texts arg.
    with (
        patch("analogcoder.cli.run_orchestration", new=AsyncMock(return_value=fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_final_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2  # baseline sweep + final sweep
    assert result["status"] == "FAIL"
    assert "pvt_sweep" in result
    assert result["pvt_sweep"]["worst_case_corners"]["gain"]["process"] == "tt"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: the two new tests FAIL — `AttributeError: <module 'analogcoder.cli'> does not have the attribute 'run_full_pvt_sweep'` (from `patch("analogcoder.cli.run_full_pvt_sweep")`, since it isn't imported into `cli.py` yet). The existing tests in this file still PASS.

- [ ] **Step 3: Wire `run_full_pvt_sweep` into `cli.py`**

In `src/analogcoder/cli.py`, add the import (alongside the existing `analogcoder.*` imports near the top):

```python
from analogcoder.pvt import run_full_pvt_sweep
```

In `_run`, change the final section from:

```python
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
```

to:

```python
    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=propose_topology_fn,
    )

    if spec.pvt_corners is not None:
        baseline_sweep = run_full_pvt_sweep(initial_netlist_texts, spec, sim_backend)
        state.log_event("pvt_baseline_sweep", baseline_sweep)

    result = await run_orchestration(initial_netlist_texts, spec, state, agents)

    if spec.pvt_corners is not None:
        final_netlist_texts = state.current_netlist_texts()
        final_sweep = run_full_pvt_sweep(final_netlist_texts, spec, sim_backend)
        state.log_event("pvt_final_sweep", final_sweep)
        result["pvt_sweep"] = final_sweep
        if not final_sweep["overall_pass"]:
            result["status"] = "FAIL"
            result["failure_reason"] = f"final PVT sweep failed: {final_sweep['summary']}"

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: PASS (8 tests — 6 existing + 2 new).

- [ ] **Step 5: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/cli.py tests/unit/test_cli.py
git commit -m "feat: wire full PVT sweep into cli.py before and after orchestration"
```

---

## Task 7: Full suite run and manual end-to-end validation

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: nothing — this is the plan's final gate.

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: all tests PASS. This includes every test in the project, confirming nothing in Tasks 1-6 regressed the sky130 migration, PSR/settling-time, or topology-swap features.

- [ ] **Step 2: Verify no stray files were left in the benchmark directory**

Run: `git status --short benchmarks/two_stage_opamp/`
Expected: empty (clean).

---

## Post-plan manual validation (not automated)

The full 45-corner × 4-testbench = 180-simulation sweep is exercised here for
the first time, matching this project's established pattern (PSR/settling-time,
sky130 migration) of a real end-to-end run after the automated suite passes:

```bash
.venv/bin/analogcoder --spec benchmarks/two_stage_opamp/spec_pvt.yaml --run-dir runs/pvt_verification_1
```

Check `runs/pvt_verification_1/result.json` for the `pvt_sweep` key and its
`worst_case_corners` breakdown. **A `FAIL` here is expected and does not mean
the implementation is broken** — real corner data gathered during planning
(e.g. `ss`/`1.62V`/`-40°C` dropped `unity_gain_bandwidth` to ~954kHz, well
below the 1.5MHz threshold; `ff`/`1.98V`/`125°C` collapsed `dc_gain` to
~5dB) already shows the current sizing's margins do not survive the real
corner range. Confirm the run completes cleanly (no crashes, no timeouts)
and produces a coherent per-criterion worst-case breakdown — that is what
this plan is responsible for. Fixing the circuit to actually pass PVT is
out of scope, deferred to the corner-reduction/auto-retuning sub-project
(see project memory `project-pvt-corner-future`).
