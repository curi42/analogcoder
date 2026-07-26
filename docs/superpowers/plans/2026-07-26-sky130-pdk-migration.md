# sky130 PDK Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `benchmarks/two_stage_opamp/` from generic ngspice level-1 devices to the SkyWater sky130 PDK, in place, with both `topologies.py` topologies re-validated and all seven criteria's thresholds re-derived from real measurements.

**Architecture:** A shared `pdk_corner.inc` (already committed) centralizes all PDK/corner data; every testbench netlist and both `topologies.py` subckt bodies `.include` it. `area_limits.py` and `NgspiceBackend` both need small, independent fixes uncovered while writing this plan (see Task 1 and Task 2) before the sky130 netlist content can be trusted to run or be area-gated correctly. Everything else is data replacement — device lines, supply values, thresholds — no other source module changes.

**Tech Stack:** SPICE netlist text (sky130 PDK primitives), YAML (spec), Python 3, pytest with real ngspice (no mocking — matches this project's existing `test_*_ngspice.py` pattern).

## Global Constraints

- All four `benchmarks/two_stage_opamp/*.cir` files' `.subckt OPAMP2STAGE ... .ends OPAMP2STAGE` bodies must stay byte-identical to each other at every point in this plan — copy the exact same text into all four files in Task 5, don't retype it.
- `pdk_corner.inc` requires `.option scale=1.0u` before any device instantiation (already present) — without it, `W=`/`L=` values are read as meters, not microns.
- The self-biased reference's `Rdeg=20k` / `Rstart=3Meg` values are empirically validated to land in this reference topology's usable current band (it is bimodal — see the design spec's Known Limitations). Do not change them as part of this plan.
- `miller_nulling_resistor`'s `Rz=220000` (220kΩ) is the empirically-validated peak value (design spec's Nulling-resistor validation section) — do not substitute the generic-device benchmark's old `Rz=500`.
- Re-derived thresholds (exact values, from the design spec's Re-derived thresholds table):
  - `dc_gain` `>= 60.0` dB (`spec.yaml`), `>= 70.0` dB (`spec_topology_required.yaml`, unchanged)
  - `unity_gain_bandwidth` `>= 1500000.0` Hz (`spec.yaml`), `>= 2500000.0` Hz (`spec_topology_required.yaml`)
  - `phase_margin` `>= 60.0` deg (`spec.yaml`, unchanged value), `>= 62.0` deg (`spec_topology_required.yaml`)
  - `psr_plus` `<= -10.0` dB (unchanged)
  - `psr_minus` `<= 0.0` dB (re-derived from `-8.0`)
  - `settling_time_hi` / `settling_time_lo` `<= 0.0000028` s (re-derived from `0.0000012`)
- `area_limits.py`'s fix must not change behavior for non-`X`-prefixed refdes (`M`/`C`/`R`, the generic-device benchmarks) or for `X`-prefixed instances that aren't PDK primitives (e.g. `Xdut ... OPAMP2STAGE`, a subckt instantiation) — both must remain exactly as they behave today.

---

## Task 1: Fix `NgspiceBackend` relative-include resolution

**Why this is first:** `NgspiceBackend.run()` copies the netlist into a private `tempfile.TemporaryDirectory()` before invoking ngspice, and calls `subprocess.run([...])` without a `cwd=` argument. Verified directly with real ngspice (see design spec) that ngspice resolves relative `.include` paths against the **process's current working directory**, not the directory containing the file being read. Since the subprocess inherits whatever CWD the Python process happens to have (not the temp dir, and not `benchmarks/two_stage_opamp/`), every `.include "pdk_corner.inc"` this migration adds would fail outside of manually `cd`-ing first. This must be fixed before any sky130 netlist can be trusted to run via the real backend (used by every later task's real-ngspice tests, and by the orchestrator itself).

**Files:**
- Modify: `src/analogcoder/simulators/ngspice.py`
- Test: `tests/unit/test_ngspice_backend.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `NgspiceBackend.run(netlist_path, testbench_config)` now resolves relative `.include` paths in `netlist_path`'s file (and anything it transitively includes) against `netlist_path`'s own directory, regardless of the calling process's CWD. No signature change.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_ngspice_backend.py` (append after the existing tests, keep the existing `import os` and `from analogcoder.simulators.ngspice import NgspiceBackend` at the top):

```python
def test_ngspice_backend_resolves_relative_includes_against_netlist_directory(tmp_path):
    # NgspiceBackend copies the netlist into its own private temp directory
    # before invoking ngspice. A relative .include path in the netlist must
    # still resolve against the ORIGINAL netlist's directory (where a
    # sibling .inc file actually lives), not the process's CWD (pytest's
    # CWD here is the repo root, unrelated to tmp_path) and not the
    # backend's private temp copy location.
    included = tmp_path / "shared.inc"
    included.write_text("* shared include\n.param unused_param=1\n")

    netlist = tmp_path / "netlist.cir"
    netlist.write_text(
        "* test\n"
        '.include "shared.inc"\n'
        "R1 in 0 1k\n"
        "V1 in 0 DC 1\n"
        ".end\n"
    )

    backend = NgspiceBackend()
    result = backend.run(str(netlist), {"control_block": ".control\nop\nprint v(in)\n.endc"})

    assert result.status == "success"
    assert "could not find include file" not in result.raw_log.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_ngspice_backend.py::test_ngspice_backend_resolves_relative_includes_against_netlist_directory -v`
Expected: FAIL — `result.status == "error"` and `raw_log` contains `"Could not find include file shared.inc"`.

- [ ] **Step 3: Fix `NgspiceBackend.run`**

In `src/analogcoder/simulators/ngspice.py`, the `run` method currently reads:

```python
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

            try:
                proc = subprocess.run(
                    [self.ngspice_bin, "-b", deck_path],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
```

Change it to compute the original netlist's directory and pass it as `cwd`:

```python
    def run(self, netlist_path: str, testbench_config: dict) -> RawSimResult:
        with open(netlist_path) as f:
            lines = f.readlines()

        netlist_dir = os.path.dirname(os.path.abspath(netlist_path))

        body = [ln for ln in lines if ln.strip().lower() != ".end"]
        control_block = testbench_config["control_block"]
        deck = "".join(body) + "\n" + control_block + "\n.end\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            deck_path = os.path.join(tmpdir, "deck.cir")
            with open(deck_path, "w") as f:
                f.write(deck)

            try:
                proc = subprocess.run(
                    [self.ngspice_bin, "-b", deck_path],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=netlist_dir,
                )
```

Leave everything else in the method (the `except` blocks, measurement parsing, status detection) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_ngspice_backend.py -v`
Expected: PASS (5 tests — the 4 existing plus this new one). The existing 4 tests must still pass unchanged — they don't use `.include`, so `cwd` doesn't affect them.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/simulators/ngspice.py tests/unit/test_ngspice_backend.py
git commit -m "fix: resolve NgspiceBackend relative includes against the netlist's own directory"
```

---

## Task 2: Fix `area_limits.py` device-type classification for sky130 primitives

**Files:**
- Modify: `src/analogcoder/area_limits.py`
- Test: `tests/unit/test_area_limits.py`

**Interfaces:**
- Consumes: `Component` (`src/analogcoder/netlist.py`) — `component.ctype` (refdes first letter, uppercased), `component.value` (the last positional token — for an `X`-prefixed sky130 instantiation line, this is the subckt/model name, e.g. `sky130_fd_pr__nfet_01v8`).
- Produces: `allowed_multiplier_for(ctype, baseline_value)` unchanged signature; `_tier_baseline_value` and `check_area_growth` now classify `X`-prefixed components by their instantiated subckt/model name instead of leaving them unconstrained.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_area_limits.py` (append after the existing tests, keep existing imports):

```python
SKY130_NETLIST = (
    "* sky130-style test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "X6 vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8\n"
    "Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1\n"
    "Xdut vinp vinn vout vdd vss OPAMP2STAGE\n"
    ".ends AMP\n"
    ".end\n"
)


def test_check_area_growth_classifies_sky130_fet_by_subckt_name():
    baseline = index_baseline_components(SKY130_NETLIST)
    # sky130 netlists write W/L as bare microns with no unit suffix (relying
    # on pdk_corner.inc's ".option scale=1.0u"), so both old_value and
    # new_value here are unitless too, matching what a real tuner proposal
    # against this netlist would emit - mixing in a "u" suffix on only one
    # side would silently corrupt the ratio (30e-6 / 8 instead of 30 / 8).
    # X6's baseline W=8 is far above every TRANSISTOR_TIERS boundary (those
    # are expressed in meters, e.g. 30e-6) once misread as unitless, so it
    # always lands in the strictest (1.5x) tier - 8->30 is 3.75x, which
    # exceeds even that. This only rejects if X6 is correctly classified as
    # a transistor (ctype "M") from its sky130_fd_pr__nfet_01v8 subckt name,
    # not its "X" refdes prefix.
    changes = [{"refdes": "X6", "param": "W", "old_value": "8", "new_value": "30"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False
    assert "X6" in feedback


def test_check_area_growth_classifies_sky130_mim_cap_by_subckt_name():
    baseline = index_baseline_components(SKY130_NETLIST)
    # Xcc's "value" positional token is the subckt name
    # ("sky130_fd_pr__cap_mim_m3_1"), not a numeric literal - area growth
    # must be judged on its w= param instead, the same way a transistor's
    # is judged on W=. w: 12.05->50 is a real ~4.1x growth, which must be
    # rejected once Xcc is correctly classified as a capacitor (ctype "C").
    changes = [{"refdes": "Xcc", "param": "w", "old_value": "12.05", "new_value": "50"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False
    assert "Xcc" in feedback


def test_check_area_growth_still_treats_subckt_instantiation_as_unconstrained():
    # Xdut instantiates a whole op-amp subckt (OPAMP2STAGE), not a sky130
    # PDK primitive - its "value" doesn't contain "fet" or "cap", so it must
    # remain unconstrained, exactly like today's behavior for any X-prefixed
    # non-PDK-primitive instance.
    baseline = index_baseline_components(SKY130_NETLIST)
    changes = [{"refdes": "Xdut", "param": "value", "old_value": "OPAMP2STAGE", "new_value": "OTHERAMP"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -v`
Expected: `test_check_area_growth_classifies_sky130_fet_by_subckt_name` and `test_check_area_growth_classifies_sky130_mim_cap_by_subckt_name` both FAIL (`approved` is `True` in both — today `X6`/`Xcc`'s ctype is `"X"`, `TIERS_BY_CTYPE.get("X")` is `None`, so `allowed_multiplier_for` returns `None` and neither change is ever rejected). `test_check_area_growth_still_treats_subckt_instantiation_as_unconstrained` passes already (it doesn't yet depend on the fix) — that's fine, it's here to pin down behavior the fix must not break.

- [ ] **Step 3: Implement the classification fix**

In `src/analogcoder/area_limits.py`, add a classification helper and use it in both places that currently read `component.ctype` directly:

```python
_SKY130_CTYPE_MARKERS: list[tuple[str, str]] = [
    ("fet", "M"),
    ("cap", "C"),
    ("res", "R"),
]


def _classify_ctype(component: Component) -> str:
    """Effective device-type for tiering. Generic-device refdes prefixes
    (M/C/R) pass through unchanged. An X-prefixed sky130 PDK primitive
    instantiation carries its subckt/model name as component.value (the
    last positional token on the line) - classify by that name instead,
    since sky130 transistors and MiM caps are both X-prefixed and would
    otherwise be indistinguishable from an unconstrained subckt
    instantiation like "Xdut ... OPAMP2STAGE"."""
    if component.ctype != "X" or component.value is None:
        return component.ctype
    lowered = component.value.lower()
    for marker, ctype in _SKY130_CTYPE_MARKERS:
        if marker in lowered:
            return ctype
    return component.ctype
```

Update `_tier_baseline_value` to use it. This needs a third branch beyond the original `component.ctype == "M"` check: an `X`-prefixed sky130 MiM cap's `component.value` is its subckt name (`sky130_fd_pr__cap_mim_m3_1`), not a numeric literal — like a transistor's `W=`, its size dimension has to come from a param (`w=`), not `.value`:

```python
def _tier_baseline_value(component: Component) -> float | None:
    """The dimension used to pick a size tier: baseline W for transistors
    (L rarely varies in this project); for a sky130 MiM cap (X-prefixed,
    classified as "C"), baseline w= (its "value" is a subckt name, not a
    numeric literal, so it can't be used directly); the component's own
    value for every other C/R."""
    ctype = _classify_ctype(component)
    if ctype == "M":
        w = component.params.get("W")
        return parse_spice_value(w) if w is not None else None
    if component.ctype == "X" and ctype == "C":
        w = component.params.get("w")
        return parse_spice_value(w) if w is not None else None
    if component.value is not None:
        return parse_spice_value(component.value)
    return None
```

Update `check_area_growth` to pass the classified ctype to `allowed_multiplier_for` instead of the raw one, and guard the `_tier_baseline_value` call against `ValueError` (it can now raise one — see below):

```python
        try:
            tier_baseline = _tier_baseline_value(component)
        except ValueError:
            continue
        if tier_baseline is None:
            continue
        allowed = allowed_multiplier_for(_classify_ctype(component), tier_baseline)
```

This is the only change to `check_area_growth` — everything else in the function stays the same.

Why the `try`/`except` is needed now (it wasn't before this fix): a generic-device `R`/`C` component whose `.value` is genuinely not numeric (e.g. a subckt instantiation like `Xdut ... OPAMP2STAGE`, tested by `test_check_area_growth_still_treats_subckt_instantiation_as_unconstrained` below) still falls through to the `component.value is not None: return parse_spice_value(component.value)` branch, since its classified ctype is `"X"` (no `"fet"`/`"cap"`/`"res"` marker matched) — and `parse_spice_value("OPAMP2STAGE")` raises `ValueError`. Before this task, `_tier_baseline_value` was never called with such a component reaching that branch in a way that raised (the old code only took the `component.ctype == "M"` branch or the `component.value` branch for real R/C components with numeric values). This guard matches the existing philosophy already documented in `area_limits.py` for the `changes`-loop `parse_spice_value` calls: an unparseable baseline value means "can't judge area impact, don't block on it," not a crash.

One consequence worth noting, not a bug to fix here: `TRANSISTOR_TIERS`/`CAPACITOR_TIERS` boundaries (`30e-6`, `80e-6`, `3e-12`, `10e-12`, ...) are calibrated for the generic-device benchmark's explicit-unit-suffix values (`"40u"` → `4e-5`, `"2p"` → `2e-12`). sky130 netlists write `W=`/`w=` as bare microns (no `"u"` suffix, relying on `pdk_corner.inc`'s `.option scale=1.0u`), so `parse_spice_value("8")` returns raw `8.0`, not `8e-6`. Every sky130 device's tier-selection value is therefore always far above every tier boundary, and always lands in the strictest (unbounded, `1.5x`) tier — this is safe (the strictest limit, not a loophole) and the *ratio* computation (`new_value / baseline_value`) is unaffected, since both sides use the same bare-micron convention consistently. Recalibrating the tier boundaries for sky130's unit convention is out of this plan's scope (not part of the approved design spec) — flag it as a follow-up if a future change needs sky130 devices to have tiered rather than uniformly-strict limits.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -v`
Expected: PASS (all tests, existing + 3 new).

- [ ] **Step 5: Run the full unit suite to confirm no regression**

Run: `.venv/bin/python -m pytest tests/unit -v -k "not ngspice"`
Expected: PASS (this excludes the real-ngspice tests, which are covered separately in later tasks; `area_limits.py` has no other consumers whose behavior should change).

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/area_limits.py tests/unit/test_area_limits.py
git commit -m "fix: classify sky130 X-prefixed devices by subckt name in area_limits"
```

---

## Task 3: Extend `pdk_corner.inc` with the MiM capacitor parameter block

**Files:**
- Modify: `benchmarks/two_stage_opamp/pdk_corner.inc`
- Test: `tests/unit/test_pdk_corner_ngspice.py` (new)

**Interfaces:**
- Consumes: `NgspiceBackend` (fixed in Task 1).
- Produces: `pdk_corner.inc` now provides everything needed to instantiate `sky130_fd_pr__nfet_01v8`, `sky130_fd_pr__pfet_01v8`, and `sky130_fd_pr__cap_mim_m3_1`/`_2` from a single `.include "pdk_corner.inc"` — this is what Task 5's testbench netlists rely on.

- [ ] **Step 1: Update `pdk_corner.inc`**

Replace the full content of `benchmarks/two_stage_opamp/pdk_corner.inc` with:

```
* Single swap point for PVT corner / PDK model data used by every
* two_stage_opamp testbench netlist. All four testbench files (.cir) and
* both topologies.py subckt bodies .include this ONE file rather than
* each vendoring their own copy of the PDK include chain - swapping to a
* different corner (or a different PDK entirely, e.g. a company-internal
* corner.inc delivered as an HSPICE .inc file) means editing only this
* file, not every netlist.
*
* Currently: SkyWater sky130 nfet_01v8/pfet_01v8, tt (typical) corner,
* vendored via git submodule at third_party/skywater-pdk-libs-sky130_fd_pr
* (sparse-checked-out to models/, cells/nfet_01v8/, cells/pfet_01v8/,
* cells/cap_mim_m3/, tech/ only - see
* docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md).
.option scale=1.0u
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/lod.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"

* MiM capacitor (Cc/Ca compensation caps) - minimal hand-extracted param
* recipe, not the official include chain. The official chain
* (models/r+c/res_typical__cap_typical.spice ->
* models/sky130_fd_pr__model__r+c.model.spice) pulls in an unrelated
* resistor-cell family not needed here and not in the sparse-checkout;
* these ~10 scalar params were extracted directly from that chain instead.
* Capacitance: C = camimc*w*l + cpmimc*2*(w+l) (verified to ~0.5% accuracy
* against a real ngspice .op run).
.param tol_m3=0.0
.param rm3=0.047 rcvia3=3.41
.param tc1rm3=3.424e-3 tc2rm3=-7.739e-7
.param tc1rvia3=2.366e-3 tc2rvia3=-1.025e-5
.param m3_dw=-0.025u
.param camimc=2.00e-15 cpmimc=0.19e-15
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_1.model.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_2.model.spice"
```

- [ ] **Step 2: Write the real-ngspice smoke test**

Create `tests/unit/test_pdk_corner_ngspice.py`:

```python
import os

from analogcoder.simulators.ngspice import NgspiceBackend

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def test_pdk_corner_inc_loads_nfet_pfet_and_mim_cap_cleanly():
    # Written alongside the real benchmark files (not tmp_path) so the
    # relative `.include "pdk_corner.inc"` here, and pdk_corner.inc's own
    # relative "../../third_party/..." includes, resolve exactly the way
    # they do for the real testbench netlists: NgspiceBackend runs ngspice
    # with cwd set to the netlist's own directory (Task 1's fix).
    smoke_path = os.path.join(BENCHMARK_DIR, "_pdk_corner_smoke_test.cir")
    with open(smoke_path, "w") as f:
        f.write(
            "* pdk_corner.inc smoke test - not a real benchmark testbench\n"
            '.include "pdk_corner.inc"\n'
            "Vdd vdd 0 DC 1.8\n"
            "Vss vss 0 DC 0\n"
            "Xn n vdd vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4\n"
            "Xp p vss vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=4\n"
            "Xc n p sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1\n"
            ".end\n"
        )

    try:
        backend = NgspiceBackend()
        result = backend.run(smoke_path, {"control_block": ".control\nop\nprint v(n) v(p)\n.endc"})
    finally:
        os.remove(smoke_path)

    assert result.status == "success"
    assert "could not find" not in result.raw_log.lower()
    assert "undefined parameter" not in result.raw_log.lower()
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_pdk_corner_ngspice.py -v`
Expected: PASS. If it fails with `"could not find include file"`, re-check Task 1 landed and `pdk_corner.inc`'s paths are relative to `benchmarks/two_stage_opamp/` (three levels: repo root → `third_party/...`, i.e. `../../third_party/...` from `benchmarks/two_stage_opamp/`).

- [ ] **Step 4: Commit**

```bash
git add benchmarks/two_stage_opamp/pdk_corner.inc tests/unit/test_pdk_corner_ngspice.py
git commit -m "feat: add MiM capacitor include recipe to pdk_corner.inc"
```

---

## Task 4: Migrate `topologies.py` to sky130

**Files:**
- Modify: `src/analogcoder/topologies.py`
- Test: `tests/unit/test_topologies.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `TOPOLOGY_LIBRARY["miller_basic"].subckt_body` and `TOPOLOGY_LIBRARY["miller_nulling_resistor"].subckt_body` are the exact device-line text Task 5 copies into all four benchmark netlists — copy verbatim, do not retype.

- [ ] **Step 1: Update `test_topologies.py`'s expectations first (TDD against the new refdes set)**

Replace the full content of `tests/unit/test_topologies.py`:

```python
from analogcoder.netlist import parse_netlist
from analogcoder.topologies import TOPOLOGY_LIBRARY, Topology

_SELF_BIAS_REFDES = {"Xp3", "Xp4", "Xn1", "Xn2", "Rdeg", "Rstart"}
_INPUT_PAIR_REFDES = {"X1", "X2", "X3", "X4", "X5", "X6", "X7"}
_MIM_CAP_REFDES = {"Xcc", "Xca"}


def test_library_has_exactly_the_v1_entries():
    assert set(TOPOLOGY_LIBRARY.keys()) == {"miller_basic", "miller_nulling_resistor"}
    for topology_id, topology in TOPOLOGY_LIBRARY.items():
        assert isinstance(topology, Topology)
        assert topology.id == topology_id


def test_miller_basic_body_has_expected_components():
    body = TOPOLOGY_LIBRARY["miller_basic"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    refdes = {c.refdes for c in parsed.subckts["TEST"].components}
    assert refdes == _SELF_BIAS_REFDES | _INPUT_PAIR_REFDES | _MIM_CAP_REFDES


def test_miller_nulling_resistor_body_has_rz_in_series_with_cc():
    body = TOPOLOGY_LIBRARY["miller_nulling_resistor"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    subckt = parsed.subckts["TEST"]
    refdes = {c.refdes for c in subckt.components}
    assert refdes == _SELF_BIAS_REFDES | _INPUT_PAIR_REFDES | _MIM_CAP_REFDES | {"Rz"}
    cc = next(c for c in subckt.components if c.refdes == "Xcc")
    rz = next(c for c in subckt.components if c.refdes == "Rz")
    assert cc.nodes[1] == rz.nodes[0]  # Xcc's second node feeds directly into Rz's first node
    assert rz.nodes[1] == "vout"
    assert rz.value == "220000"


def test_miller_nulling_resistor_addresses_phase_margin():
    assert "phase_margin" in TOPOLOGY_LIBRARY["miller_nulling_resistor"].addresses
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_topologies.py -v`
Expected: FAIL — `TOPOLOGY_LIBRARY`'s bodies still have the old generic-device refdes set (`Iref`, `M9`, `M1`...`M7`, `Cc`, `Ca`).

- [ ] **Step 3: Replace both subckt bodies in `topologies.py`**

Replace the full content of `src/analogcoder/topologies.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str  # lines between ".subckt NAME ports" and ".ends NAME"
    addresses: list[str]  # criterion names this is known to help; informational only, used in the tuner prompt


TOPOLOGY_LIBRARY: dict[str, Topology] = {
    "miller_basic": Topology(
        id="miller_basic",
        description="Standard two-stage Miller-compensated CMOS op-amp (sky130), no nulling resistor.",
        addresses=[],
        subckt_body="""\
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
""",
    ),
    "miller_nulling_resistor": Topology(
        id="miller_nulling_resistor",
        description=(
            "Two-stage Miller-compensated CMOS op-amp (sky130) with a nulling resistor Rz "
            "(220kOhm, empirically validated - see the design spec's Rz sweep) in series "
            "with Cc, cancelling the right-half-plane zero. On this sizing, improves phase "
            "margin AND unity-gain bandwidth simultaneously relative to no-Rz, rather than "
            "the usual bandwidth-for-phase-margin trade-off."
        ),
        addresses=["phase_margin"],
        subckt_body="""\
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA cczz sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Rz   cczz vout 220000
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
""",
    ),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_topologies.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/topologies.py tests/unit/test_topologies.py
git commit -m "feat: migrate both two_stage_opamp topologies to sky130"
```

---

## Task 5: Migrate the four benchmark netlist files to sky130

**Files:**
- Modify: `benchmarks/two_stage_opamp/netlist.cir`
- Modify: `benchmarks/two_stage_opamp/netlist_psr_plus.cir`
- Modify: `benchmarks/two_stage_opamp/netlist_psr_minus.cir`
- Modify: `benchmarks/two_stage_opamp/netlist_settling.cir`

**Interfaces:**
- Consumes: `TOPOLOGY_LIBRARY["miller_basic"].subckt_body` (Task 4) — copy the exact text below, which matches it, into all four files' `.subckt OPAMP2STAGE ... .ends OPAMP2STAGE` blocks. `pdk_corner.inc` (Task 3).
- Produces: four sky130 netlists Task 6/7's tests and threshold updates validate against.

No test file in this task — Task 6 and Task 7's real-ngspice tests are what validate these four files (splitting the netlist content from the threshold/assertion updates would leave this task with nothing of its own to verify; bundling them here would make Task 6/7 too large to review independently of the threshold reasoning). Run the ad-hoc real-ngspice check in Step 5 below to confirm each file works before moving on.

- [ ] **Step 1: Replace `netlist.cir`**

Replace the full content of `benchmarks/two_stage_opamp/netlist.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), SkyWater sky130 PDK.
* AC loop-gain testbench: Lfb blocks the DC feedback path at AC (closes the
* loop for DC biasing only), Cin injects the AC stimulus into the inverting
* input. Reading vdb(vout)/vp(vout) with this topology gives loop gain and
* loop phase directly - phase starts near 180 degrees at DC (stable negative
* feedback) and phase margin is the phase value at the frequency where the
* loop gain crosses 0 dB.
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8
Vss vss 0 DC 0

Vinp vinp 0 DC 0.55
Lfb vout vinn 1e6
Cin vstim vinn 1
Vstim vstim 0 DC 0 AC 1

Xdut vinp vinn vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

- [ ] **Step 2: Replace `netlist_psr_plus.cir`**

Replace the full content of `benchmarks/two_stage_opamp/netlist_psr_plus.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), SkyWater sky130 PDK.
* PSR+ testbench: AC=1 injected on Vdd, no input stimulus, same AC loop-break
* (Lfb) topology as the main AC testbench so the amp sees the same AC bias
* environment. Reading vdb(vout) with this topology gives supply-to-output
* gain directly. The OPAMP2STAGE subckt body below must stay byte-identical
* to netlist.cir, netlist_psr_minus.cir, and netlist_settling.cir - tuning
* changes are applied to all four files independently and rely on that.
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8 AC 1
Vss vss 0 DC 0

Vinp vinp 0 DC 0.55
Lfb vout vinn 1e6
Cin vstim vinn 1
Vstim vstim 0 DC 0

Xdut vinp vinn vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

- [ ] **Step 3: Replace `netlist_psr_minus.cir`**

Replace the full content of `benchmarks/two_stage_opamp/netlist_psr_minus.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), SkyWater sky130 PDK.
* PSR- testbench, redefined for a hard-grounded Vss: AC=1 injected directly
* on the Vss (ground) node itself - a GND-bounce test, not a separate
* negative-rail PSR test (the original +-2.5V split-supply benchmark's Vss
* was a real rail to stimulate; here Vss=0V is hard ground, so there is no
* separate rail - this measures how much ground noise/bounce couples to
* vout). Same AC loop-break (Lfb) topology as the main AC testbench. The
* OPAMP2STAGE subckt body below must stay byte-identical to netlist.cir,
* netlist_psr_plus.cir, and netlist_settling.cir - tuning changes are
* applied to all four files independently and rely on that.
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8
Vss vss 0 DC 0 AC 1

Vinp vinp 0 DC 0.55
Lfb vout vinn 1e6
Cin vstim vinn 1
Vstim vstim 0 DC 0

Xdut vinp vinn vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

- [ ] **Step 4: Replace `netlist_settling.cir`**

Replace the full content of `benchmarks/two_stage_opamp/netlist_settling.cir`:

```
* Two-stage CMOS op-amp (Miller-compensated), SkyWater sky130 PDK.
* Settling-time testbench: closed-loop unity-gain buffer (vout wired
* directly to vinn via the Xdut instantiation below), fed a 0.3V step at
* vinp centered near the 0.55V input common-mode point. Unlike the AC
* loop-gain testbench (which breaks feedback at AC via Lfb), this is a
* genuine closed loop - feedback factor 1, the same unity-loop-gain
* condition the AC testbench's phase_margin criterion targets. The
* OPAMP2STAGE subckt body below must stay byte-identical to netlist.cir,
* netlist_psr_plus.cir, and netlist_psr_minus.cir - tuning changes are
* applied to all four files independently and rely on that.
.include "pdk_corner.inc"

.subckt OPAMP2STAGE vinp vinn vout vdd vss
Xp3 pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xp4 nbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=2
Xn1 pbias nbias vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=2
Xn2 nbias nbias degn vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
Rdeg degn vss 20k
Rstart vdd nbias 3Meg

X1   n1   vinn tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X2   outA vinp tail vdd sky130_fd_pr__pfet_01v8 L=0.5 W=8
X3   n1   n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X4   outA n1   vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=4
X5   tail pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=15

X6   vout outA vss vss sky130_fd_pr__nfet_01v8 L=0.5 W=8
X7   vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30

Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1
Xca outA 0    sky130_fd_pr__cap_mim_m3_1 w=6.88 l=6.88 mf=1
.ends OPAMP2STAGE

Vdd vdd 0 DC 1.8
Vss vss 0 DC 0

Vinp vinp 0 PULSE(0.4 0.7 1u 1n 1n 10u 20u)
Xdut vinp vout vout vdd vss OPAMP2STAGE
Cload vout 0 2p
.end
```

- [ ] **Step 5: Ad-hoc real-ngspice check (not a committed test — Task 6/7 add the real tests)**

Run this from the repo root to confirm all four files parse and simulate cleanly before moving on:

```bash
.venv/bin/python -c "
from analogcoder.simulators.ngspice import NgspiceBackend
backend = NgspiceBackend()
files_and_controls = [
    ('benchmarks/two_stage_opamp/netlist.cir', '.control\nset units=degrees\nac dec 20 1 100meg\nmeas ac gain_db find vdb(vout) at=1\nmeas ac ugbw_hz when vdb(vout)=0\nmeas ac phase_margin_deg find vp(vout) when vdb(vout)=0\n.endc'),
    ('benchmarks/two_stage_opamp/netlist_psr_plus.cir', '.control\nac dec 20 1 100meg\nmeas ac psr_plus_db find vdb(vout) at=1\n.endc'),
    ('benchmarks/two_stage_opamp/netlist_psr_minus.cir', '.control\nac dec 20 1 100meg\nmeas ac psr_minus_db find vdb(vout) at=1\n.endc'),
    ('benchmarks/two_stage_opamp/netlist_settling.cir', '.control\ntran 1n 6u\nmeas tran t_hi_last WHEN v(vout)=0.70398 CROSS=LAST\nmeas tran t_lo_last WHEN v(vout)=0.69698 CROSS=LAST\n.endc'),
]
for path, control in files_and_controls:
    result = backend.run(path, {'control_block': control})
    print(path, result.status, result.measurements)
"
```

Expected: all four print `success` with measurements close to (within ~1%): `netlist.cir` → `gain_db≈71.09`, `ugbw_hz≈2079550`, `phase_margin_deg≈34.56`; `netlist_psr_plus.cir` → `psr_plus_db≈-15.40`; `netlist_psr_minus.cir` → `psr_minus_db≈-1.43`; `netlist_settling.cir` → `t_hi_last≈2.4666e-6`, `t_lo_last≈2.2610e-6`. If any file errors, re-check its `.subckt OPAMP2STAGE` block is byte-identical to the other three and that `.include "pdk_corner.inc"` is present.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/two_stage_opamp/netlist.cir benchmarks/two_stage_opamp/netlist_psr_plus.cir benchmarks/two_stage_opamp/netlist_psr_minus.cir benchmarks/two_stage_opamp/netlist_settling.cir
git commit -m "feat: migrate two_stage_opamp benchmark netlists to sky130"
```

---

## Task 6: Update `spec.yaml` thresholds and its real-ngspice tests

**Files:**
- Modify: `benchmarks/two_stage_opamp/spec.yaml`
- Modify: `tests/unit/test_psr_benchmark_ngspice.py`
- Modify: `tests/unit/test_settling_benchmark_ngspice.py`

**Interfaces:**
- Consumes: the four sky130 netlists (Task 5).
- Produces: `spec.yaml`'s criteria now carry the re-derived thresholds every later validation (including the final full-suite run and the manual end-to-end run) depends on.

- [ ] **Step 1: Update `spec.yaml`'s criteria**

In `benchmarks/two_stage_opamp/spec.yaml`, make these four edits (control blocks are unchanged — only the values listed change):

`ac_loop_gain` testbench's `criteria` list — change `dc_gain`'s threshold from `70.0` to `60.0`, and `unity_gain_bandwidth`'s threshold from `20000000.0` to `1500000.0` (`phase_margin`'s `60.0` is unchanged):

```yaml
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
```

`psr_minus` testbench's criterion — change the threshold from `-8.0` to `0.0`:

```yaml
    criteria:
      - name: psr_minus
        measurement: psr_minus_db
        operator: "<="
        threshold: 0.0
        unit: dB
```

`settling_time` testbench — change both the `.meas` band-edge voltages in `control_block` (the step target changed from 1V to 0.7V) and both criteria's thresholds from `0.0000012` to `0.0000028`:

```yaml
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

`psr_plus`'s criterion (`-10.0`) is unchanged — leave it as-is.

- [ ] **Step 2: Update `test_psr_benchmark_ngspice.py`**

Replace the full content of `tests/unit/test_psr_benchmark_ngspice.py`:

```python
import os

from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "two_stage_opamp")


def _load_two_stage_opamp_spec():
    return load_spec(os.path.join(BENCHMARK_DIR, "spec.yaml"))


def test_spec_declares_four_testbenches_with_expected_criteria():
    spec = _load_two_stage_opamp_spec()

    assert [tb.name for tb in spec.testbenches] == [
        "ac_loop_gain", "psr_plus", "psr_minus", "settling_time",
    ]

    psr_plus = next(tb for tb in spec.testbenches if tb.name == "psr_plus")
    assert psr_plus.criteria[0].measurement == "psr_plus_db"
    assert psr_plus.criteria[0].operator == "<="
    assert psr_plus.criteria[0].threshold == -10.0

    psr_minus = next(tb for tb in spec.testbenches if tb.name == "psr_minus")
    assert psr_minus.criteria[0].measurement == "psr_minus_db"
    assert psr_minus.criteria[0].operator == "<="
    assert psr_minus.criteria[0].threshold == 0.0


def test_baseline_netlist_matches_validated_psr_measurements():
    # Real ngspice measurements recorded in
    # docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md's
    # Validation section for the sky130 miller_basic subckt. This test
    # exists to catch unintentional drift in the committed .cir files - not
    # to re-derive the thresholds.
    spec = _load_two_stage_opamp_spec()
    backend = NgspiceBackend()

    psr_plus = next(tb for tb in spec.testbenches if tb.name == "psr_plus")
    result = backend.run(psr_plus.netlist_path, {"control_block": psr_plus.control_block})
    assert result.status == "success"
    assert -15.6 <= result.measurements["psr_plus_db"] <= -15.2

    psr_minus = next(tb for tb in spec.testbenches if tb.name == "psr_minus")
    result = backend.run(psr_minus.netlist_path, {"control_block": psr_minus.control_block})
    assert result.status == "success"
    assert -1.6 <= result.measurements["psr_minus_db"] <= -1.3


def test_psr_plus_and_psr_minus_subckt_bodies_match_main_testbench():
    # Enforces the invariant this whole feature depends on: tuning changes
    # applied independently to each testbench file only stay consistent if
    # the OPAMP2STAGE subckt text is byte-identical across all four.
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

(Two changes from the original: the psr_minus threshold assertion now expects `0.0`; the measured-value ranges are re-centered on the sky130 numbers; and `test_psr_plus_and_psr_minus_subckt_bodies_match_main_testbench` now also checks `settling_time`, matching what `test_settling_benchmark_ngspice.py`'s equivalent check already independently verifies — keeping both is intentional redundancy across the two test files, not a bug.)

- [ ] **Step 3: Update `test_settling_benchmark_ngspice.py`**

In `tests/unit/test_settling_benchmark_ngspice.py`, replace the `test_baseline_netlist_matches_validated_settling_measurements` function body:

```python
def test_baseline_netlist_matches_validated_settling_measurements():
    # These are the real ngspice measurements recorded in
    # docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md's
    # Validation section for the sky130 miller_basic subckt (Cc MiM cap,
    # w=12.05/l=12.05). This test exists to catch unintentional drift in
    # the committed .cir file - not to re-derive the thresholds.
    spec = _load_two_stage_opamp_spec()
    settling = next(tb for tb in spec.testbenches if tb.name == "settling_time")
    backend = NgspiceBackend()

    result = backend.run(settling.netlist_path, {"control_block": settling.control_block})

    assert result.status == "success"
    assert 2.4e-6 <= result.measurements["t_hi_last"] <= 2.55e-6
    assert 2.2e-6 <= result.measurements["t_lo_last"] <= 2.32e-6
```

Leave the rest of the file (`test_spec_declares_four_testbenches_including_settling_time`, `test_settling_subckt_body_matches_other_three_testbenches`, the module-level `_load_two_stage_opamp_spec` helper) unchanged — none of them hardcode the old thresholds or measurement values.

- [ ] **Step 4: Run the updated tests**

Run: `.venv/bin/python -m pytest tests/unit/test_psr_benchmark_ngspice.py tests/unit/test_settling_benchmark_ngspice.py -v`
Expected: PASS (6 tests total — 3 in each file).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/two_stage_opamp/spec.yaml tests/unit/test_psr_benchmark_ngspice.py tests/unit/test_settling_benchmark_ngspice.py
git commit -m "feat: re-derive two_stage_opamp spec.yaml thresholds for sky130"
```

---

## Task 7: Update `spec_topology_required.yaml` thresholds and the topology-swap test

**Files:**
- Modify: `benchmarks/two_stage_opamp/spec_topology_required.yaml`
- Modify: `tests/unit/test_topology_swap_ngspice.py`

**Interfaces:**
- Consumes: the four sky130 netlists (Task 5), `TOPOLOGY_LIBRARY` (Task 4).
- Produces: nothing consumed by later tasks — this is the plan's proof that the topology-swap mechanism has a genuine trigger-and-resolve path on the sky130 netlists.

- [ ] **Step 1: Update `spec_topology_required.yaml`**

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
        threshold: 2500000.0
        unit: Hz
      - name: phase_margin
        measurement: phase_margin_deg
        operator: ">="
        threshold: 62.0
        unit: deg
```

- [ ] **Step 2: Update `test_topology_swap_ngspice.py`'s assertions**

In `tests/unit/test_topology_swap_ngspice.py`, replace both test function bodies (leave `_run_topology` and the imports unchanged):

```python
def test_miller_basic_topology_cannot_meet_phase_margin_spec(tmp_path):
    # miller_basic is the two_stage_opamp benchmark's original topology. Real
    # ngspice measures its phase margin at 34.56 deg (see the sky130 PDK
    # migration design spec's Validation section) - far below the 62 deg
    # threshold required by spec_topology_required.yaml, and four rounds of
    # real parameter search (documented in that spec) never found a
    # parameter-only combination that closes this gap without regressing PSR
    # or gain below threshold. This is the whole reason topology-swap tuning
    # exists. This test proves the starting topology genuinely fails it.
    result = _run_topology("miller_basic", tmp_path)

    assert result.status == "success"
    assert result.measurements["phase_margin_deg"] < 62.0


def test_miller_nulling_resistor_topology_meets_all_criteria(tmp_path):
    # miller_nulling_resistor adds a nulling resistor (Rz=220kOhm, empirically
    # validated - see the design spec's Rz sweep) in series with Cc,
    # cancelling the right-half-plane zero. It should meet all three
    # spec_topology_required.yaml criteria simultaneously (not just phase
    # margin, which is the one it targets) - and on this sizing, actually
    # improves unity-gain bandwidth too rather than trading it away.
    result = _run_topology("miller_nulling_resistor", tmp_path)

    assert result.status == "success"
    assert result.measurements["phase_margin_deg"] >= 62.0
    assert result.measurements["ugbw_hz"] >= 2_500_000.0
    assert result.measurements["gain_db"] >= 70.0
```

- [ ] **Step 3: Run the updated tests**

Run: `.venv/bin/python -m pytest tests/unit/test_topology_swap_ngspice.py -v`
Expected: PASS (2 tests).

- [ ] **Step 4: Commit**

```bash
git add benchmarks/two_stage_opamp/spec_topology_required.yaml tests/unit/test_topology_swap_ngspice.py
git commit -m "feat: re-derive spec_topology_required.yaml thresholds for sky130"
```

---

## Task 8: Full suite run and final housekeeping

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: nothing — this is the plan's final gate before manual end-to-end validation.

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: all tests PASS. This includes every test in the project, not just the ones touched by this plan — confirms Task 1's `NgspiceBackend` change and Task 2's `area_limits.py` change didn't regress the generic-device benchmarks (`inverting_amp`, and any lingering generic-device-specific test).

- [ ] **Step 2: Confirm the git submodule and `.gitmodules` are committed**

Run: `git log --oneline -- .gitmodules third_party/skywater-pdk-libs-sky130_fd_pr`
Expected: shows the commit from the design-spec phase of this project (`docs: add sky130 PDK migration design spec for two_stage_opamp`) — the submodule should already be committed; this step is a sanity check, not a new commit.

- [ ] **Step 3: Verify no stray scratch files were left in the benchmark directory**

Run: `git status --short benchmarks/two_stage_opamp/`
Expected: empty (clean) — confirms Task 3's smoke test cleaned up `_pdk_corner_smoke_test.cir` via its `finally` block, and no other ad-hoc files leaked in.

---

## Post-plan manual validation (not automated)

Mirrors the pattern established for the PSR/settling-time features — run the real end-to-end orchestration once this plan lands, to see the topology-swap mechanism actually trigger on real (not synthetic) criteria:

```bash
.venv/bin/analogcoder --spec benchmarks/two_stage_opamp/spec.yaml --run-dir runs/sky130_migration_1
```

Expected: `phase_margin` fails on the first simulate/judge pass (main topology measures ~34.56°, threshold is 60°); after `TOPOLOGY_SWITCH_THRESHOLD` (3) consecutive tuning rollbacks, the orchestrator swaps to `miller_nulling_resistor`; the run should reach `"status": "PASS"` in `runs/sky130_migration_1/result.json` with all seven criteria passing (per the design spec's Validation table for the nulling-resistor topology). Check `history.jsonl` for a `topology_swap: true` event and confirm `verify_post` didn't flag any post-swap regression across the four testbenches.

Separately, run `--spec benchmarks/two_stage_opamp/spec_topology_required.yaml` (which starts from and only ever validates `netlist.cir`'s single `ac_loop_gain`-equivalent criteria) to confirm the harder-threshold spec also reaches PASS only after a topology swap.
