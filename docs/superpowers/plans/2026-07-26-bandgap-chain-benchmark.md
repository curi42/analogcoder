# Bandgap Chain Benchmark (Part 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `benchmarks/bandgap/` — a five-block Kuijk bandgap reference chain
that measures whether the tuner changes the *correct block* in a multi-block
circuit — plus the three `area_limits` fixes the benchmark exposes.

**Architecture:** Six flat `.subckt` definitions (`ERRAMP`, `TRIMAMP`, `BUF_N`,
`BUF_P`, `BGR_CORE`, `BANDGAP`) duplicated byte-identically across five
testbench `.cir` files, which differ only in their top-level harness. All
device sizing and every threshold in this plan is **measured**, not derived —
see the "Part 2 — as built" section of
`docs/superpowers/specs/2026-07-26-bandgap-benchmark-and-scoped-refdes-design.md`
for the measurements and for the assumptions they disproved.

**Tech Stack:** ngspice, SkyWater sky130 PDK (git submodule, sparse-checkout),
Python 3 / pytest.

## Global Constraints

- **The six subckt definitions are byte-identical in all five `.cir` files.**
  `orchestrator._apply_to_all` applies one tuning proposal to every testbench
  netlist, and `apply_changes` silently skips a refdes it cannot find. A
  definition that differs between files diverges silently.
- **Every capacitor is a MOS cap, never MiM.** nfet caps (D/S/B on `vss`,
  gate on the signal node) where a rail reference works; a pfet cap (D/S/B in
  its own nwell) only where the cap must float. This is a user requirement
  about their real process, not a stylistic choice.
- **Every amplifier is a folded cascode plus a common-source output stage.**
  This is the structure the target design uses, and it is also what makes
  1.62 V operation possible — a plain mirror load leaves the input pair no
  saturation margin at a 1.2 V common mode. Do not "simplify" an amp back to
  a 5T OTA.
- **Never guess a threshold.** Every number in `spec.yaml` comes from the
  measured table in the design spec. If a step here disagrees with a
  measurement you take, report it rather than adjusting the number silently.
- Python: no new runtime dependencies. Tests go in `tests/unit/` except
  real-ngspice tests, which go in `tests/integration/` only if they need
  credentials — ngspice tests live in `tests/unit/` alongside the existing
  `test_psr_benchmark_ngspice.py`, following that file's skip-gating pattern.

---

### Task 1: sky130 PNP and poly-resistor availability

**Files:**
- Modify: `.git/modules/third_party/skywater-pdk-libs-sky130_fd_pr/info/sparse-checkout` (via `git sparse-checkout set`)
- Create: `benchmarks/bandgap/pdk_corner.inc`
- Create: `benchmarks/bandgap/pdk_corner_ss.inc`, `pdk_corner_ff.inc`, `pdk_corner_sf.inc`, `pdk_corner_fs.inc`
- Test: `tests/unit/test_bandgap_devices_ngspice.py`

**Interfaces:**
- Produces: `benchmarks/bandgap/pdk_corner*.inc`, each defining
  `sky130_fd_pr__nfet_01v8`, `sky130_fd_pr__pfet_01v8`,
  `sky130_fd_pr__pnp_05v5_W3p40L3p40` and `sky130_fd_pr__res_high_po` for one
  process corner. Task 4's netlists `.include "pdk_corner.inc"`.

- [ ] **Step 1: Add the two cells to the submodule sparse-checkout**

```bash
cd third_party/skywater-pdk-libs-sky130_fd_pr
git sparse-checkout set cells/cap_mim_m3 cells/nfet_01v8 cells/pfet_01v8 \
    cells/pnp_05v5 cells/res_high_po models tech
ls cells/   # expect: cap_mim_m3 nfet_01v8 pfet_01v8 pnp_05v5 res_high_po
cd ../..
```

- [ ] **Step 2: Write the failing device test**

Create `tests/unit/test_bandgap_devices_ngspice.py`:

```python
import os
import shutil
import subprocess
import tempfile

import pytest

BENCH = os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")


def _run(deck: str) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "probe.cir")
        with open(path, "w") as f:
            f.write(deck)
        proc = subprocess.run(
            ["ngspice", "-b", path], capture_output=True, text=True, timeout=120,
            cwd=os.path.abspath(BENCH),
        )
    values = {}
    for line in (proc.stdout + proc.stderr).splitlines():
        parts = line.strip().split("=")
        if len(parts) == 2:
            try:
                values[parts[0].strip()] = float(parts[1].strip())
            except ValueError:
                pass
    return values


DECK = """* device probe
.include "pdk_corner.inc"
Ie1 0 e1 DC 5u
Xq1 0 0 e1 0 sky130_fd_pr__pnp_05v5_W3p40L3p40
Ie8 0 e8 DC 5u
Xq8 0 0 e8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8
Ir 0 rt DC 1u
Xr1 rt 0 0 sky130_fd_pr__res_high_po w=1 l=10
.control
op
let dvbe_mv = (v(e1)-v(e8))*1e3
let rval = v(rt)/1u
print dvbe_mv rval
.endc
.end
"""


def test_pnp_instance_multiplier_gives_the_ptat_delta_vbe():
    # VT*ln(8) at 27C is 53.78mV; the measured 54.59mV includes the model's
    # emitter resistance and ise non-ideality. The point of the assertion is
    # that "m=8" scales emitter area at all - the subckt's own "mult"
    # parameter does NOT (it only scales mismatch terms, which are zero
    # without Monte Carlo), and using it silently yields zero delta-Vbe and a
    # bandgap core with no PTAT current.
    values = _run(DECK)

    assert values["dvbe_mv"] == pytest.approx(54.59, abs=2.0)


def test_pnp_delta_vbe_is_zero_without_an_area_ratio():
    deck = DECK.replace("sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8",
                        "sky130_fd_pr__pnp_05v5_W3p40L3p40 mult=8")

    values = _run(deck)

    assert abs(values["dvbe_mv"]) < 0.001


def test_res_high_po_sheet_and_head_resistance():
    # 317.4*(l+0.247)/0.999 + 345.83/1.1548 = 3554 ohm for w=1, l=10. The
    # ~300 ohm head term is not negligible for the core's 10.9k R1.
    values = _run(DECK)

    assert values["rval"] == pytest.approx(3554.0, rel=0.02)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_bandgap_devices_ngspice.py -v`
Expected: FAIL — `benchmarks/bandgap/pdk_corner.inc` does not exist, so ngspice
emits no measurements and the `KeyError`/assertion fails.

- [ ] **Step 4: Write `benchmarks/bandgap/pdk_corner.inc`**

```
* Single swap point for PVT corner / PDK model data used by every bandgap
* testbench netlist. All five testbench files .include this ONE file, so
* swapping corner (or PDK) means editing only this file.
*
* Currently: SkyWater sky130 tt (typical) corner - nfet_01v8, pfet_01v8,
* pnp_05v5 and res_high_po - vendored via the git submodule at
* third_party/skywater-pdk-libs-sky130_fd_pr.
*
* nonfet.spice carries dkispp5x/dkbfpp5x, the PNP's process-corner knobs
* (bf varies 0.46x to 1.79x across corners, a real stress on the core), plus
* the junction-cap multipliers the pnp model references. The tt corner's
* nonfet data lives under models/corners/wafer/, not models/corners/tt/.
.option scale=1.0u
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parameters/lod.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/corners/wafer/nonfet.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__tt.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"

* Vertical substrate PNP. The three *_slope params select nominal (no Monte
* Carlo); without them the model's mismatch expressions are undefined.
.param sky130_fd_pr__pnp_05v5_W3p40L3p40__bf_slope = 0.0
.param sky130_fd_pr__pnp_05v5_W3p40L3p40__is_slope = 0.0
.param sky130_fd_pr__pnp_05v5_W3p40L3p40__xti_slope = 0.0
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pnp_05v5/sky130_fd_pr__pnp_05v5_W3p40L3p40.model.spice"

* High-sheet poly resistor (rsheet 317.4 ohm/sq, tc1 -4.3e-4, tc2 12e-6).
* These seven scalars are extracted rather than pulled in via the official
* chain (models/sky130_fd_pr__model__linear.model.spice), which .includes the
* unrelated res_xhigh_po cell family that is not in the sparse-checkout -
* the same hand-extraction precedent the MiM cap already sets in
* benchmarks/two_stage_opamp/pdk_corner.inc. Resistor process corner is held
* at typical for every FET corner: Rp and R1 are the same material, so the
* ratio that sets TC cancels it to first order.
.param sky130_fd_pr__res_high_po__slope_spectre = 0.0
.param sky130_fd_pr__res_high_po__con_slope_spectre = 0.0
.param sky130_fd_pr__res_high_po__var = 0.0
.param tc1rpolybody = 0.514e-3
.param tc2rpolybody = 0.122e-5
.param crpf_precision = 1.06e-04
.param crpfsw_precision_1_1 = 5.04e-11
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/parasitics/sky130_fd_pr__model__parasitic__res_po.model.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/res_high_po/sky130_fd_pr__res_high_po.model.spice"
```

- [ ] **Step 5: Write the four corner variants**

Each is a copy of `pdk_corner.inc` with exactly three lines changed: the two
FET corner includes and the `nonfet.spice` path. Replace the header's second
paragraph with:

```
* Corner-specific variant of pdk_corner.inc for the "<CORNER>" process corner
* - part of the PVT corner sweep. Byte-identical to pdk_corner.inc except the
* nfet_01v8/pfet_01v8/nonfet corner include lines; the mismatch and lod
* includes are corner-independent.
```

and substitute, for `<CORNER>` in `ss`, `ff`, `sf`, `fs`:

```
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/models/corners/<CORNER>/nonfet.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/nfet_01v8/sky130_fd_pr__nfet_01v8__<CORNER>.corner.spice"
.include "../../third_party/skywater-pdk-libs-sky130_fd_pr/cells/pfet_01v8/sky130_fd_pr__pfet_01v8__<CORNER>.corner.spice"
```

Note `wafer` → `<CORNER>` for nonfet: only `tt` uses `wafer`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_bandgap_devices_ngspice.py -v`
Expected: 3 passed.

- [ ] **Step 7: Verify every corner include actually loads**

```bash
for c in "" _ss _ff _sf _fs; do
  printf '* probe\n.include "pdk_corner%s.inc"\nVd d 0 DC 1.8\nXr r 0 0 sky130_fd_pr__res_high_po w=1 l=10\nIr 0 r DC 1u\nXq 0 0 e 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8\nIe 0 e DC 5u\n.control\nop\nprint v(r) v(e)\n.endc\n.end\n' "$c" > /tmp/c.cir
  echo "== corner${c:-_tt} =="; (cd benchmarks/bandgap && ngspice -b /tmp/c.cir 2>&1 | grep -E "^v\(|rror")
done
```

Expected: every corner prints two finite voltages and no error.

- [ ] **Step 8: Commit**

```bash
git add third_party benchmarks/bandgap tests/unit/test_bandgap_devices_ngspice.py
git commit -m "feat: vendor sky130 pnp_05v5 and res_high_po for the bandgap benchmark"
```

---

### Task 2: make the area gate's size tiers unit-correct

**Files:**
- Modify: `src/analogcoder/netlist.py` (add `.option scale` parsing)
- Modify: `src/analogcoder/area_limits.py` (`_tier_baseline_value`, new geometry tiers)
- Test: `tests/unit/test_area_limits.py`

**Interfaces:**
- Consumes: `Component` from `netlist.py` (Task 0 / already on master).
- Produces: `netlist.netlist_scale(text) -> float`; `Component.geometry_scale`
  populated by `parse_netlist`; `area_limits.SKY130_GEOMETRY_TIERS`.

**Why this task exists.** On the only benchmark that uses a real PDK, the size
tiers are inert. `benchmarks/two_stage_opamp/netlist.cir` sets
`.option scale=1.0u` and writes bare geometry (`W=30`), so
`parse_spice_value("30")` returns `30.0` — thirty *metres* — and every device
falls past `30e-6` and `80e-6` into the unbounded 1.5× tier. Verified on
master:

```
X7     ctype=M  tier_baseline=30.0     allowed=1.5
Xcc    ctype=C  tier_baseline=12.05    allowed=1.5
```

`Xcc` is worse than mis-tiered: it compares a MiM cap's *width in µm* against
tiers denominated in *farads*.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_area_limits.py`:

```python
SCALED_NETLIST = (
    ".option scale=1.0u\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "X7 vout pbias vdd vdd sky130_fd_pr__pfet_01v8 L=0.5 W=30\n"
    "Xcc outA vout sky130_fd_pr__cap_mim_m3_1 w=12.05 l=12.05 mf=1\n"
    "XRz vout nz 0 sky130_fd_pr__res_high_po w=1 l=15\n"
    ".ends AMP\n"
)


def test_scale_option_is_applied_to_sky130_geometry():
    # Without this, W=30 under ".option scale=1.0u" is read as 30 metres and
    # every sky130 device lands in the unbounded 1.5x tier, making the whole
    # tier table inert on the only PDK-backed benchmarks in the repo.
    components = index_baseline_components(SCALED_NETLIST)

    assert _tier_baseline_value(components["AMP.X7"]) == pytest.approx(30e-6)


def test_sky130_transistor_gets_its_real_tier_not_the_fallback():
    components = index_baseline_components(SCALED_NETLIST)

    allowed = allowed_multiplier_for(_classify_ctype(components["AMP.X7"]),
                                     _tier_baseline_value(components["AMP.X7"]))

    assert allowed == 2.0


def test_sky130_mim_cap_is_tiered_by_geometry_not_by_farads():
    components = index_baseline_components(SCALED_NETLIST)

    assert _tier_baseline_value(components["AMP.Xcc"]) == pytest.approx(12.05e-6)


def test_x_prefixed_resistor_is_tiered_by_length_instead_of_falling_through():
    # A sky130 resistor's positional "value" is its subckt NAME, so the old
    # code raised ValueError here and check_area_growth swallowed it, leaving
    # the resistor silently unconstrained.
    components = index_baseline_components(SCALED_NETLIST)

    assert _tier_baseline_value(components["AMP.XRz"]) == pytest.approx(15e-6)


def test_area_gate_rejects_an_oversized_sky130_resistor_growth():
    components = index_baseline_components(SCALED_NETLIST)

    ok, feedback = check_area_growth(
        components,
        [{"refdes": "AMP.XRz", "param": "l", "new_value": "90"}],
    )

    assert ok is False
    assert "XRz" in feedback


def test_area_gate_allows_a_within_tier_sky130_resistor_growth():
    components = index_baseline_components(SCALED_NETLIST)

    ok, _ = check_area_growth(
        components,
        [{"refdes": "AMP.XRz", "param": "l", "new_value": "30"}],
    )

    assert ok is True
```

Make sure `pytest`, `_tier_baseline_value`, `_classify_ctype`,
`allowed_multiplier_for`, `index_baseline_components` and `check_area_growth`
are imported at the top of the file (add whichever are missing).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -v`
Expected: the six new tests FAIL — `30.0 != 3e-05`, `1.5 != 2.0`,
`ValueError: not a valid SPICE numeric literal: 'sky130_fd_pr__res_high_po'`
(surfacing directly from `_tier_baseline_value`), and the gate accepting a 6×
growth.

- [ ] **Step 3: Parse `.option scale` in `netlist.py`**

Add near the other module-level regexes:

```python
_SCALE_RE = re.compile(r"^\s*\.option[s]?\b.*?\bscale\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def netlist_scale(text: str) -> float:
    """The `.option scale=` multiplier applied to device geometry, or 1.0 when
    the deck sets none. sky130 decks in this repo set `scale=1.0u` and then
    write bare geometry (`W=30` meaning 30um); without this, any code reading
    W/L as an absolute size is off by six orders of magnitude."""
    match = _SCALE_RE.search(text)
    if not match:
        return 1.0
    try:
        return parse_spice_value(match.group(1))
    except ValueError:
        return 1.0
```

`netlist_scale` calls `parse_spice_value`, which is defined at the bottom of
the module — that is fine, the call happens at runtime.

Add the field to `Component`:

```python
    geometry_scale: float = 1.0
```

and populate it in `parse_netlist`. Compute the scale once before the loop and
set it on every component:

```python
def parse_netlist(text: str) -> ParsedNetlist:
    top_components: list[Component] = []
    subckts: dict[str, Subckt] = {}
    current_subckt: Subckt | None = None
    scale = netlist_scale(text)
    ...
        component = _parse_component_line(line)
        component.geometry_scale = scale
```

- [ ] **Step 4: Add geometry tiers and rewrite `_tier_baseline_value`**

In `area_limits.py`, add after `RESISTOR_TIERS`:

```python
# Tiers for an X-prefixed sky130 primitive, keyed on the GEOMETRY dimension in
# metres (W for a transistor, w for a MiM cap, l for a poly resistor) rather
# than on a value in ohms/farads. A subckt-instantiated primitive has no
# numeric value to tier on - its positional value is the subckt name - so the
# device-value tiers above simply do not apply to it.
SKY130_GEOMETRY_TIERS: list[SizeTier] = [
    SizeTier(max_value=25e-6, allowed_multiplier=3.0),
    SizeTier(max_value=50e-6, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
```

The 25 µm boundary is not arbitrary: `BUF_P.Xcl` has a 20 µm baseline and the
`spec_seed_buf0_droop.yaml` fix needs to grow it to 50 µm (2.5×), which only
the 3.0× tier permits. `TRIMAMP.XRz` (15 µm baseline) and `ERRAMP`'s devices
land in the same tier; `X7` at 30 µm lands in the 2.0× tier.

Replace `_tier_baseline_value` in full with:

```python
# The geometry dimension each X-prefixed sky130 primitive is tiered on.
_SKY130_GEOMETRY_PARAM: dict[str, str] = {"M": "W", "C": "w", "R": "l"}


def _tier_baseline_value(component: Component) -> float | None:
    """The dimension used to pick a size tier.

    An X-prefixed sky130 primitive is tiered on geometry scaled by the deck's
    `.option scale`: W for a transistor, w for a MiM cap, l for a poly
    resistor (its length sets both its resistance and its area). Its
    positional `value` is the subckt NAME, so there is nothing else to tier on
    - reading it raised ValueError, which check_area_growth swallowed, leaving
    the device silently unconstrained.

    A generic (non-X) transistor is still tiered on W; every other generic
    component on its own value, which is already an absolute quantity."""
    ctype = _classify_ctype(component)
    if component.ctype == "X":
        param = _SKY130_GEOMETRY_PARAM.get(ctype)
        if param is None:
            return None
        raw = component.params.get(param)
        return parse_spice_value(raw) * component.geometry_scale if raw is not None else None
    if ctype == "M":
        w = component.params.get("W")
        return parse_spice_value(w) * component.geometry_scale if w is not None else None
    if component.value is not None:
        return parse_spice_value(component.value)
    return None
```

Import `parse_spice_value` from `netlist` if it is not already imported there.

- [ ] **Step 5: Route the geometry tiers into the tier lookup**

`allowed_multiplier_for` selects tiers by ctype, which cannot distinguish an
X-prefixed primitive from a generic one. Give it the component instead:

```python
def allowed_multiplier_for(ctype: str, baseline_value: float, is_sky130: bool = False) -> float | None:
    tiers = SKY130_GEOMETRY_TIERS if is_sky130 else TIERS_BY_CTYPE.get(ctype)
    if tiers is None:
        return None
    for tier in tiers:
        if tier.max_value is None or baseline_value < tier.max_value:
            return tier.allowed_multiplier
    return tiers[-1].allowed_multiplier
```

and in `check_area_growth` pass `is_sky130=(component.ctype == "X")`. Keeping
the third parameter optional preserves the existing
`allowed_multiplier_for("M", 20e-6) == 3.0` tests unchanged.

Adjust the two new tests' expectations if and only if a measurement
contradicts them: `X7` W=30 µm → `30e-6 < 50e-6` → 2.0×;
`XRz` l=15 µm → `15e-6 < 50e-6` → 2.0×, so `l` 15→30 (2.0×) is allowed and
15→90 (6.0×) is rejected. Both new tests above assume exactly this.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. If a pre-existing `test_orchestrator.py` or
`test_area_limits.py` case now behaves differently, that is a real behaviour
change worth reporting — do not silence it by widening the tiers.

- [ ] **Step 7: Commit**

```bash
git add src/analogcoder/netlist.py src/analogcoder/area_limits.py tests/unit/test_area_limits.py
git commit -m "fix: tier sky130 device sizes by scaled geometry instead of raw tokens"
```

---

### Task 3: constrain the PNP emitter-area multiplier

**Files:**
- Modify: `src/analogcoder/area_limits.py`
- Test: `tests/unit/test_area_limits.py`

**Interfaces:**
- Consumes: `_classify_ctype`, `SKY130_GEOMETRY_TIERS` (Task 2).
- Produces: `"Q"` as a classified ctype, `PNP_TIERS`.

- [ ] **Step 1: Write the failing tests**

```python
PNP_NETLIST = (
    ".option scale=1.0u\n"
    ".subckt CORE vbgout vdd vss\n"
    "Xq1 0 0 na 0 sky130_fd_pr__pnp_05v5_W3p40L3p40\n"
    "Xq8 0 0 ne8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8\n"
    ".ends CORE\n"
)


def test_pnp_is_classified_rather_than_falling_through_to_unconstrained():
    components = index_baseline_components(PNP_NETLIST)

    assert _classify_ctype(components["CORE.Xq8"]) == "Q"


def test_pnp_emitter_multiplier_growth_is_bounded():
    # m is an emitter-area count, not a length: m=8 -> m=24 triples the PNP's
    # area. Left unclassified it was completely unconstrained.
    components = index_baseline_components(PNP_NETLIST)

    ok, feedback = check_area_growth(
        components, [{"refdes": "CORE.Xq8", "param": "m", "new_value": "24"}]
    )

    assert ok is False
    assert "Xq8" in feedback


def test_pnp_emitter_multiplier_small_growth_is_allowed():
    components = index_baseline_components(PNP_NETLIST)

    ok, _ = check_area_growth(
        components, [{"refdes": "CORE.Xq8", "param": "m", "new_value": "12"}]
    )

    assert ok is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -k pnp -v`
Expected: FAIL — `_classify_ctype` returns `"X"`, and both growth checks
return `True` because `allowed_multiplier_for` finds no tiers.

- [ ] **Step 3: Add the marker and tier**

```python
_SKY130_CTYPE_MARKERS: list[tuple[str, str]] = [
    ("fet", "M"),
    ("cap", "C"),
    ("res", "R"),
    ("pnp", "Q"),
]
```

`"pnp"` goes last because the list is scanned in order and the earlier markers
are more specific to the device families that already work; no sky130 pnp
model name contains `fet`, `cap` or `res`, so order is not load-bearing here —
but keep the existing three ahead of it so their behaviour cannot change.

```python
# A bipolar's tuning knob is its emitter-area multiplier m, a count rather
# than a length, so it gets one flat tier instead of a size-graded table.
PNP_TIERS: list[SizeTier] = [SizeTier(max_value=None, allowed_multiplier=2.0)]
```

Register it and give it a geometry param:

```python
TIERS_BY_CTYPE: dict[str, list[SizeTier]] = {
    "M": TRANSISTOR_TIERS,
    "C": CAPACITOR_TIERS,
    "R": RESISTOR_TIERS,
    "Q": PNP_TIERS,
}

_SKY130_GEOMETRY_PARAM: dict[str, str] = {"M": "W", "C": "w", "R": "l", "Q": "m"}
```

`m` is a count, not a length, so it must **not** be multiplied by
`geometry_scale`. Guard the scaling in `_tier_baseline_value`'s X branch:

```python
        scale = 1.0 if ctype == "Q" else component.geometry_scale
        return parse_spice_value(raw) * scale if raw is not None else None
```

In `allowed_multiplier_for`, a `"Q"` must use `PNP_TIERS` rather than
`SKY130_GEOMETRY_TIERS` even though it is X-prefixed:

```python
    if ctype == "Q":
        tiers = PNP_TIERS
    elif is_sky130:
        tiers = SKY130_GEOMETRY_TIERS
    else:
        tiers = TIERS_BY_CTYPE.get(ctype)
```

A `Xq1` with no `m=` token has no baseline for `m`; `_tier_baseline_value`
returns `None` and `check_area_growth` already treats that as unconstrained,
which is correct — there is nothing to grow from.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/area_limits.py tests/unit/test_area_limits.py
git commit -m "feat: bound PNP emitter-area multiplier growth in the area gate"
```

---

### Task 4: the bandgap netlists

**Files:**
- Create: `benchmarks/bandgap/netlist.cir` (the `dc_tc` testbench — this is
  `testbenches[0]`, i.e. `TargetSpec.canonical`)
- Create: `benchmarks/bandgap/netlist_startup.cir`
- Create: `benchmarks/bandgap/netlist_psrr.cir`
- Create: `benchmarks/bandgap/netlist_settling.cir`
- Create: `benchmarks/bandgap/netlist_loops.cir`
- Test: `tests/unit/test_bandgap_benchmark_ngspice.py`

**Interfaces:**
- Consumes: `benchmarks/bandgap/pdk_corner.inc` (Task 1).
- Produces: five netlists whose subckt definition block is byte-identical.
  Task 5's `spec.yaml` names them and supplies their control blocks.

- [ ] **Step 1: Write the failing benchmark test**

Create `tests/unit/test_bandgap_benchmark_ngspice.py`:

```python
import os
import shutil

import pytest

from analogcoder.netlist import resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend

BENCH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")
)

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")

CONTROL = {
    "netlist.cir": """.control
dc temp -40 125 1
meas dc vmax MAX v(vbgout)
meas dc vmin MIN v(vbgout)
meas dc vbgout_v FIND v(vbgout) AT=27
meas dc vbg0_v FIND v(vbg0) AT=27
meas dc vbg1_v FIND v(vbg1) AT=27
meas dc idd FIND i(Vdd) AT=27
let tc_ppm_per_c = (vmax-vmin)/(vbgout_v*165)*1e6
let iq_ua = -1e6*idd
print vbgout_v vbg0_v vbg1_v iq_ua tc_ppm_per_c
.endc""",
}


def _run(name, control, tmp_path):
    with open(os.path.join(BENCH, name)) as f:
        text = resolve_includes(f.read(), BENCH)
    path = tmp_path / name
    path.write_text(text)
    return NgspiceBackend().run(str(path), {"control_block": control})


def test_dc_tc_testbench_reproduces_the_measured_nominal_operating_point(tmp_path):
    result = _run("netlist.cir", CONTROL["netlist.cir"], tmp_path)

    assert result.status == "success"
    m = result.measurements
    assert m["vbgout_v"] == pytest.approx(1.2399, abs=0.010)
    assert m["vbg1_v"] == pytest.approx(1.2009, abs=0.010)
    assert m["vbg0_v"] == pytest.approx(0.5003, abs=0.005)
    assert m["tc_ppm_per_c"] == pytest.approx(36.3, abs=5.0)
    assert m["iq_ua"] == pytest.approx(213.1, abs=20.0)


def test_every_testbench_defines_the_same_blocks(tmp_path):
    # orchestrator._apply_to_all pushes one tuning proposal into every
    # testbench netlist, and apply_changes silently skips a refdes it cannot
    # find - so a definition that drifts between files diverges in silence.
    def blocks(name):
        with open(os.path.join(BENCH, name)) as f:
            lines = f.read().splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.lower().startswith(".subckt"))
        end = max(i for i, ln in enumerate(lines) if ln.lower().startswith(".ends"))
        return "\n".join(lines[start : end + 1])

    reference = blocks("netlist.cir")
    for name in ("netlist_startup.cir", "netlist_psrr.cir",
                 "netlist_settling.cir", "netlist_loops.cir"):
        assert blocks(name) == reference, name


def test_all_five_blocks_are_addressable_by_scoped_refdes():
    from analogcoder.netlist import parse_netlist

    with open(os.path.join(BENCH, "netlist.cir")) as f:
        parsed = parse_netlist(f.read())

    assert set(parsed.subckts) == {
        "ERRAMP", "TRIMAMP", "BUF_N", "BUF_P", "BGR_CORE", "BANDGAP"
    }
    # All four amps declare Xt/X1/X2/Xcc/XRz - the whole point of scoping.
    assert {c.refdes for c in parsed.subckts["BUF_P"].components} >= {"Xt", "X1", "Xcc", "XRz", "Xcl"}
    assert {c.refdes for c in parsed.subckts["BUF_N"].components} >= {"Xt", "X1", "Xcc", "XRz", "Xcl"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_bandgap_benchmark_ngspice.py -v`
Expected: FAIL — the netlist files do not exist.

- [ ] **Step 3: Write the shared definition block**

This block is identical in all five files. Write it once, then paste it
verbatim into each. Do not reformat it per file — Step 1's
`test_every_testbench_defines_the_same_blocks` compares the text.

Every amplifier is a **folded cascode first stage plus a common-source output
stage**, matching the target design. Every sizing number below is measured;
the reasoning for the non-obvious ones is in the design spec's
"Part 2 revision" section.

```
* ===================== amplifier blocks =====================
* Every amplifier is a folded cascode first stage plus a common-source output
* stage, Miller-compensated with a nulling resistor - the structure actually
* used in the target design.
*
* The fold is what makes 1.62V operation possible. With a plain PMOS mirror
* load the input pair's drain sits at vdd - |Vgs_p| (~0.5V measured), leaving
* Vds(input) = Vdd - Vcm - |Vgs_p| + Vgs_n, which is at or below zero once
* Vcm reaches 1.2V. In a folded cascode that same drain node sits at
* vdd - |Vdsat| (~0.2V below the rail), because the PMOS cascode gate pcas is
* biased a full |Vgs| below vdd. That recovers ~0.5V of input headroom.
*
* Polarity convention, uniform across all four: vinp sits on X2, the input
* device whose drain feeds the OUTPUT branch. vinp up -> X2 steals current
* from that branch -> outA moves down -> the inverting CS stage moves vout up.
* So the port named vinp really is non-inverting at vout.

.subckt ERRAMP vinp vinn vout vdd vss nbias ncas pbias pcas
* ERRAMP's input common mode is Vbe, which falls to 0.549V at 125C, so its
* tail node (Vcm - Vgs_n) is the tightest headroom in the whole chain. The
* tail runs at ~1uA (L=4 quarters the mirror ratio) into a wide input pair,
* which is what keeps Vgs_n low enough: at 4uA with W=16 the tail node
* measured 35mV at 125C and the loop latched to the rail; at 1uA with W=48 it
* measures 128-175mV across every process corner.
Xt   tail nbias vss  vss sky130_fd_pr__nfet_01v8 L=4 W=4
X1   nx   vinn tail  vss sky130_fd_pr__nfet_01v8 L=1 W=48
X2   ny   vinp tail  vss sky130_fd_pr__nfet_01v8 L=1 W=48
Xp1  nx   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=2 W=8
Xp2  ny   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=2 W=8
Xc1  np   pcas  nx   vdd sky130_fd_pr__pfet_01v8 L=1 W=8
Xc2  outA pcas  ny   vdd sky130_fd_pr__pfet_01v8 L=1 W=8
Xn1  np   ncas  nr   vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xn2  outA ncas  ns   vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xm1  nr   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xm2  ns   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=4
X6   vout outA  vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=20
X7   vout nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xcc  nz   outA  nz   nz  sky130_fd_pr__pfet_01v8 L=40 W=40
XRz  vout nz 0 sky130_fd_pr__res_high_po w=1 l=40
.ends ERRAMP

.subckt TRIMAMP vinp vinn vout vdd vss nbias ncas pbias pcas
Xt   tail nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X1   nx   vinn tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
X2   ny   vinp tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
Xp1  nx   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xp2  ny   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc1  np   pcas  nx   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc2  outA pcas  ny   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xn1  np   ncas  nr   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xn2  outA ncas  ns   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm1  nr   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm2  ns   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X6   vout outA  vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
X7   vout nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xcc  nz   outA  nz   nz  sky130_fd_pr__pfet_01v8 L=40 W=40
XRz  vout nz 0 sky130_fd_pr__res_high_po w=1 l=15
.ends TRIMAMP

.subckt BUF_N vinp vinn vout vdd vss nbias ncas pbias pcas
Xt   tail nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X1   nx   vinn tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
X2   ny   vinp tail  vss sky130_fd_pr__nfet_01v8 L=1 W=20
Xp1  nx   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xp2  ny   pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc1  np   pcas  nx   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xc2  outA pcas  ny   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xn1  np   ncas  nr   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xn2  outA ncas  ns   vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm1  nr   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xm2  ns   np    vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
X6   vout outA  vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
X7   vout nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=8
Xcc  nz   outA  nz   nz  sky130_fd_pr__pfet_01v8 L=50 W=50
XRz  vout nz 0 sky130_fd_pr__res_high_po w=1 l=40
Xcl  vss  vout  vss  vss sky130_fd_pr__nfet_01v8 L=20 W=20
.ends BUF_N

* Complementary fold: PMOS input pair (vbg0 ~0.5V is below any NMOS pair's
* reach), NMOS folding sinks, NMOS cascodes, PMOS cascoded mirror on top,
* NMOS common-source output.
.subckt BUF_P vinp vinn vout vdd vss nbias ncas pbias pcas
Xt   tail pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=24
X1   nx   vinn tail  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
X2   ny   vinp tail  vdd sky130_fd_pr__pfet_01v8 L=1 W=40
Xn1  nx   nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xn2  ny   nbias vss  vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xc1  np   ncas  nx   vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xc2  outA ncas  ny   vss sky130_fd_pr__nfet_01v8 L=1 W=16
Xp1  np   pcas  nr   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xp2  outA pcas  ns   vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xm1  nr   np    vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
Xm2  ns   np    vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=16
X6   vout outA  vss  vss sky130_fd_pr__nfet_01v8 L=1 W=20
X7   vout pbias vdd  vdd sky130_fd_pr__pfet_01v8 L=1 W=24
Xcc  nz   outA  nz   nz  sky130_fd_pr__pfet_01v8 L=40 W=40
XRz  vout nz 0 sky130_fd_pr__res_high_po w=1 l=15
Xcl  vss  vout  vss  vss sky130_fd_pr__nfet_01v8 L=20 W=20
.ends BUF_P

* ===================== bandgap core =====================
.subckt BGR_CORE vbgout ampout mpgate vdd vss nbias ncas pbias pcas
Xmpout vbgout mpgate vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=10

XRpa vbgout na 0 sky130_fd_pr__res_high_po w=1 l=324.74
XRpb vbgout nb 0 sky130_fd_pr__res_high_po w=1 l=324.74
Xq1  0 0 na  0 sky130_fd_pr__pnp_05v5_W3p40L3p40
XR1  nb ne8 0  sky130_fd_pr__res_high_po w=1 l=33.12
Xq8  0 0 ne8 0 sky130_fd_pr__pnp_05v5_W3p40L3p40 m=8

Xerr nb na ampout vdd vss nbias ncas pbias pcas ERRAMP

* Cascode bias chain, every leg a scaled copy of the core's OWN PTAT current
* so the whole chain shares one zero-current degenerate state. ncas is a
* NARROWER diode than nbias, so it sits a larger Vgs above vss and leaves the
* bottom mirror its Vdsat; pcas is narrower than pbias for the same reason on
* the top side, which is what puts the fold node within a Vdsat of vdd.
Xb1 nbias  ampout vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=4
Xb2 nbias  nbias  vss vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xb3 ncas   ampout vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=4
Xb4 ncas   ncas   vss vss sky130_fd_pr__nfet_01v8 L=1 W=1
Xb5 pbias  nbias  vss vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xb6 pbias  pbias  vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=4
Xb7 pcas   nbias  vss vss sky130_fd_pr__nfet_01v8 L=1 W=4
Xb8 pcas   pcas   vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=1

Xsu_r nsu    vss    vdd vdd sky130_fd_pr__pfet_01v8 L=20 W=0.42
Xsu_d nsu    vbgout vss vss sky130_fd_pr__nfet_01v8 L=1  W=2
Xsu_i ampout nsu    vss vss sky130_fd_pr__nfet_01v8 L=1  W=2
* A trickle into nbias, always on. Without it the bias chain collapses with
* the core, and then every CS output stage has its NMOS sink off while its
* PMOS is fully on - so the amp output pins HIGH, the core PMOS stays off,
* and the startup pull-down (W=2) cannot outfight a W=20 PMOS. This failure
* mode did not exist before the amps gained CS output stages.
Xsu_b nbias  vss    vdd vdd sky130_fd_pr__pfet_01v8 L=20 W=0.42

Xcc vss ampout vss vss sky130_fd_pr__nfet_01v8 L=20 W=20
.ends BGR_CORE

.subckt BANDGAP vbgout vbg0 vbg1 vtop vfb vdd vss ampout mpgate trm_i b1_i b0_i
Xcore vbgout ampout mpgate vdd vss nbias ncas pbias pcas BGR_CORE
Xtrim vbgout trm_i vtop vdd vss nbias ncas pbias pcas TRIMAMP

XRl4 vtop vfb  0 sky130_fd_pr__res_high_po w=1 l=68.6
XRl3 vfb  vt12 0 sky130_fd_pr__res_high_po w=1 l=23.4
XRl2 vt12 vt05 0 sky130_fd_pr__res_high_po w=1 l=439.6
XRl1 vt05 vss  0 sky130_fd_pr__res_high_po w=1 l=313.6

Xb1 vt12 b1_i vbg1 vdd vss nbias ncas pbias pcas BUF_N
Xb0 vt05 b0_i vbg0 vdd vss nbias ncas pbias pcas BUF_P
.ends BANDGAP
```

- [ ] **Step 4: Write `netlist.cir` (dc_tc, the canonical testbench)**

Header, then `.include "pdk_corner.inc"`, then the Step 3 block, then:

```
Vdd vdd 0 DC 1.8
Vss vss 0 DC 0
Xdut vbgout vbg0 vbg1 vtop vfb vdd vss ampout ampout vfb vbg1 vbg0 BANDGAP
.end
```

Header comment for this file:

```
* Kuijk bandgap reference chain, SkyWater sky130 PDK.
* dc_tc testbench: a temperature sweep yields the temperature coefficient and
* every DC output at AT=27 in one analysis, which is why there is no separate
* dc_out testbench. Line regulation is covered by the corner grid's voltage
* axis rather than by its own sweep.
* All four feedback loops are closed here: each break port pair is passed the
* same node.
```

- [ ] **Step 5: Write the other four testbench files**

Same header/include/definition-block structure; only the harness after the
definitions differs.

`netlist_startup.cir` harness:

```
Vdd vdd 0 PWL(0 0 100n 1.8 1 1.8)
Vss vss 0 DC 0
Xdut vbgout vbg0 vbg1 vtop vfb vdd vss ampout ampout vfb vbg1 vbg0 BANDGAP
.end
```

with header note: `* startup testbench: a 100ns Vdd ramp, so the measured time
is the circuit's own startup delay rather than the ramp's.`

`netlist_psrr.cir` harness:

```
Vdd vdd 0 DC 1.8 AC 1
Vss vss 0 DC 0
Xdut vbgout vbg0 vbg1 vtop vfb vdd vss ampout ampout vfb vbg1 vbg0 BANDGAP
.end
```

with header note: `* psrr testbench: AC stimulus on Vdd, measured on vbgout
only. On the buffered outputs PSRR collapses to -0.5dB at sf/1.71V/-40C for
input-headroom reasons, so a threshold there would assert nothing.`

`netlist_settling.cir` harness:

```
Vdd vdd 0 DC 1.8
Vss vss 0 DC 0
Xdut vbgout vbg0 vbg1 vtop vfb vdd vss ampout ampout vfb vbg1 vbg0 BANDGAP
Istep0 vbg0 0 PULSE(0 10u 1u 1n 1n 30n 10u)
Istep1 vbg1 0 PULSE(0 10u 1u 1n 1n 30n 10u)
.end
```

with header note: `* settling testbench: a 10uA/30ns charge kick on each
buffered output at t=1us. The metric is droop and residual error, not a
threshold-crossing time - an absolute crossing level produced NO measurement
at 14 of 45 corners, because the DC output level moves.`

`netlist_loops.cir` harness:

```
Vdd vdd 0 DC 1.8
Vss vss 0 DC 0
Xdut vbgout vbg0 vbg1 vtop vfb vdd vss ampout mpgate trm_i b1_i b0_i BANDGAP

Vsc mpgate ampout DC 0 AC 0
Vst trm_i  vfb    DC 0 AC 0
Vs1 b1_i   vbg1   DC 0 AC 0
Vs0 b0_i   vbg0   DC 0 AC 0
.end
```

with header note:

```
* amp_loops testbench: all four feedback loops broken by SERIES VOLTAGE
* INJECTION. Every break point in this circuit drives a MOS gate only, so a
* "DC 0 AC 1" source in series is an exact unidirectional break with no
* reactive elements, and the loop gain is just the ratio of the two sides.
*
* Do NOT go back to two_stage_opamp's Lfb/Cin harness here. A 1MH inductor is
* a 6.3Mohm open at 1Hz - ample against that op-amp's ~100k output impedance,
* but not against a folded cascode's tens of megohms, so the loop was never
* actually broken. Raising it to 1GH restores the open and then spreads the
* matrix over ~20 decades: measured result was gains of +189dB and -108dB with
* 63 of 225 corner measurements missing entirely.
*
* The control block runs four ac analyses and uses "alter @Vsrc[acmag]" to
* enable ONE injection source at a time - driving all four at once would
* cross-couple through vbgout and the ladder.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_bandgap_benchmark_ngspice.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add benchmarks/bandgap tests/unit/test_bandgap_benchmark_ngspice.py
git commit -m "feat: add the five-block Kuijk bandgap chain benchmark netlists"
```

---

### Task 5: spec files and their measured thresholds

**Files:**
- Create: `benchmarks/bandgap/spec.yaml`
- Create: `benchmarks/bandgap/spec_pvt.yaml`
- Create: `benchmarks/bandgap/spec_seed_tc.yaml`
- Create: `benchmarks/bandgap/spec_seed_trim_pm.yaml`
- Create: `benchmarks/bandgap/spec_seed_buf0_droop.yaml`
- Test: `tests/unit/test_bandgap_spec.py`

**Interfaces:**
- Consumes: the five netlists from Task 4.
- Produces: `TargetSpec` loadable specs; `spec.yaml` passes at nominal,
  the three `spec_seed_*` files each fail at nominal in exactly one block.

- [ ] **Step 1: Write the failing spec test**

Create `tests/unit/test_bandgap_spec.py`:

```python
import os
import shutil

import pytest

from analogcoder.judge_tools import evaluate_criteria
from analogcoder.netlist import resolve_includes
from analogcoder.simulators.ngspice import NgspiceBackend
from analogcoder.spec import load_spec

BENCH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "bandgap")
)

pytestmark = pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not on PATH")


def _simulate_all(spec, tmp_path):
    measurements = {}
    for tb in spec.testbenches:
        with open(tb.netlist_path) as f:
            text = resolve_includes(f.read(), os.path.dirname(tb.netlist_path))
        path = tmp_path / os.path.basename(tb.netlist_path)
        path.write_text(text)
        result = NgspiceBackend().run(str(path), {"control_block": tb.control_block})
        assert result.status == "success", (tb.name, result.raw_log[-2000:])
        measurements.update(result.measurements)
    return measurements


def test_baseline_spec_passes_at_nominal(tmp_path):
    spec = load_spec(os.path.join(BENCH, "spec.yaml"))

    verdict = evaluate_criteria(_simulate_all(spec, tmp_path), spec.all_criteria)

    assert verdict["overall_pass"] is True, verdict


def test_tc_seed_fails_at_nominal(tmp_path):
    # The coupled seed: Rp/R1 sets TC but also moves vbgout and vbg1, so the
    # tuner cannot fix this one in isolation. Measured: l=324.74 -> 36.30ppm
    # (fails), l=321.3 -> 29.30ppm (passes) with vbgout 1.2389 -> 1.2334.
    spec = load_spec(os.path.join(BENCH, "spec_seed_tc.yaml"))

    verdict = evaluate_criteria(_simulate_all(spec, tmp_path), spec.all_criteria)

    assert verdict["overall_pass"] is False
    failed = {c["name"] for c in verdict["criteria"] if not c["pass"]}
    assert failed == {"temperature_coefficient"}


def test_trim_pm_seed_fails_at_nominal(tmp_path):
    # Seeded so the tuner has exactly one correct move: raise TRIMAMP.XRz's
    # length. Measured: l=15 -> 81.1 deg (fails), l=25 -> 98.2 (passes),
    # l=90 -> 88.3 (overshoot loses margin again). Every other criterion
    # already passes, so a proposal aimed at another block is a targeting miss.
    spec = load_spec(os.path.join(BENCH, "spec_seed_trim_pm.yaml"))

    verdict = evaluate_criteria(_simulate_all(spec, tmp_path), spec.all_criteria)

    assert verdict["overall_pass"] is False
    failed = {c["name"] for c in verdict["criteria"] if not c["pass"]}
    assert failed == {"trim_phase_margin"}


def test_buf0_droop_seed_fails_at_nominal_and_is_local_to_buf_p(tmp_path):
    # Measured: BUF_P.Xcl 20x20 -> 19.93mV droop (fails <=15), 50x50 -> 12.50mV
    # (passes). Growing BUF_N.Xcl instead moves only vbg1's droop (24.12 ->
    # 23.97mV) and leaves vbg0 untouched, so this criterion really does
    # localise to one block.
    spec = load_spec(os.path.join(BENCH, "spec_seed_buf0_droop.yaml"))

    verdict = evaluate_criteria(_simulate_all(spec, tmp_path), spec.all_criteria)

    assert verdict["overall_pass"] is False
    failed = {c["name"] for c in verdict["criteria"] if not c["pass"]}
    assert failed == {"vbg0_droop"}


def test_pvt_spec_declares_the_full_supply_axis():
    spec = load_spec(os.path.join(BENCH, "spec_pvt.yaml"))

    assert spec.pvt_corners.voltage == [1.62, 1.8, 1.98]
    assert len(spec.pvt_corners.process) == 5
    assert len(spec.pvt_corners.temperature) == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_bandgap_spec.py -v`
Expected: FAIL — the spec files do not exist.

- [ ] **Step 3: Write `benchmarks/bandgap/spec.yaml`**

Thresholds are set loosely around the measured 45-corner ranges recorded in
the design spec; nominal values are in parentheses.

```yaml
circuit_name: bandgap
testbenches:
  - name: dc_tc
    netlist: netlist.cir
    analyses: ["dc"]
    control_block: |
      .control
      dc temp -40 125 1
      meas dc vmax MAX v(vbgout)
      meas dc vmin MIN v(vbgout)
      meas dc vbgout_v FIND v(vbgout) AT=27
      meas dc vbg0_v FIND v(vbg0) AT=27
      meas dc vbg1_v FIND v(vbg1) AT=27
      meas dc idd FIND i(Vdd) AT=27
      let tc_ppm_per_c = (vmax-vmin)/(vbgout_v*165)*1e6
      let iq_ua = -1e6*idd
      print vbgout_v vbg0_v vbg1_v iq_ua tc_ppm_per_c
      .endc
    criteria:
      - name: vbgout_min
        measurement: vbgout_v
        operator: ">="
        threshold: 1.20
        unit: V
      - name: vbgout_max
        measurement: vbgout_v
        operator: "<="
        threshold: 1.28
        unit: V
      - name: vbg0_min
        measurement: vbg0_v
        operator: ">="
        threshold: 0.475
        unit: V
      - name: vbg0_max
        measurement: vbg0_v
        operator: "<="
        threshold: 0.525
        unit: V
      - name: vbg1_min
        measurement: vbg1_v
        operator: ">="
        threshold: 1.14
        unit: V
      - name: vbg1_max
        measurement: vbg1_v
        operator: "<="
        threshold: 1.26
        unit: V
      - name: temperature_coefficient
        measurement: tc_ppm_per_c
        operator: "<="
        threshold: 70.0
        unit: ppm/C
      - name: quiescent_current
        measurement: iq_ua
        operator: "<="
        threshold: 300.0
        unit: uA

  - name: startup
    netlist: netlist_startup.cir
    analyses: ["tran"]
    control_block: |
      .control
      tran 20n 100u uic
      meas tran t_up WHEN v(vbgout)=1.15 RISE=1
      let startup_time = t_up
      print startup_time
      .endc
    criteria:
      - name: startup_time
        measurement: startup_time
        operator: "<="
        threshold: 0.00002
        unit: s

  - name: psrr
    netlist: netlist_psrr.cir
    analyses: ["ac"]
    control_block: |
      .control
      ac dec 20 1 10meg
      meas ac psrr_bg_db FIND vdb(vbgout) AT=1
      .endc
    criteria:
      - name: psrr_dc
        measurement: psrr_bg_db
        operator: "<="
        threshold: -25.0
        unit: dB

  - name: settling
    netlist: netlist_settling.cir
    analyses: ["tran"]
    control_block: |
      .control
      tran 2n 5u
      meas tran v0pre  FIND v(vbg0) AT=0.9u
      meas tran v1pre  FIND v(vbg1) AT=0.9u
      meas tran v0post FIND v(vbg0) AT=1.5u
      meas tran v1post FIND v(vbg1) AT=1.5u
      meas tran v0dip  MIN v(vbg0) FROM=1u TO=1.5u
      meas tran v1dip  MIN v(vbg1) FROM=1u TO=1.5u
      let vbg0_droop_mv = (v0pre-v0dip)*1e3
      let vbg1_droop_mv = (v1pre-v1dip)*1e3
      let vbg0_resid_mv = abs(v0post-v0pre)*1e3
      let vbg1_resid_mv = abs(v1post-v1pre)*1e3
      print vbg0_droop_mv vbg1_droop_mv vbg0_resid_mv vbg1_resid_mv
      .endc
    criteria:
      - name: vbg0_droop
        measurement: vbg0_droop_mv
        operator: "<="
        threshold: 50.0
        unit: mV
      - name: vbg1_droop
        measurement: vbg1_droop_mv
        operator: "<="
        threshold: 50.0
        unit: mV
      - name: vbg0_residual
        measurement: vbg0_resid_mv
        operator: "<="
        threshold: 5.0
        unit: mV
      - name: vbg1_residual
        measurement: vbg1_resid_mv
        operator: "<="
        threshold: 5.0
        unit: mV

  - name: amp_loops
    netlist: netlist_loops.cir
    analyses: ["ac"]
    control_block: |
      .control
      set units=degrees
      alter @Vsc[acmag] = 1
      ac dec 20 1 100meg
      let tmag = vdb(ampout)-vdb(mpgate)
      let tph  = vp(ampout)-vp(mpgate)
      meas ac core_gain_db FIND tmag AT=1
      meas ac core_pm_deg  FIND tph WHEN tmag=0
      alter @Vsc[acmag] = 0
      alter @Vst[acmag] = 1
      ac dec 20 1 100meg
      let tmag = vdb(vfb)-vdb(trm_i)
      let tph  = vp(vfb)-vp(trm_i)
      meas ac trim_gain_db FIND tmag AT=1
      meas ac trim_pm_deg  FIND tph WHEN tmag=0
      alter @Vst[acmag] = 0
      alter @Vs1[acmag] = 1
      ac dec 20 1 100meg
      let tmag = vdb(vbg1)-vdb(b1_i)
      let tph  = vp(vbg1)-vp(b1_i)
      meas ac buf1_gain_db FIND tmag AT=1
      meas ac buf1_pm_deg  FIND tph WHEN tmag=0
      alter @Vs1[acmag] = 0
      alter @Vs0[acmag] = 1
      ac dec 20 1 100meg
      let tmag = vdb(vbg0)-vdb(b0_i)
      let tph  = vp(vbg0)-vp(b0_i)
      meas ac buf0_gain_db FIND tmag AT=1
      meas ac buf0_pm_deg  FIND tph WHEN tmag=0
      .endc
    criteria:
      - name: core_loop_gain
        measurement: core_gain_db
        operator: ">="
        threshold: 40.0
        unit: dB
      - name: core_phase_margin
        measurement: core_pm_deg
        operator: ">="
        threshold: 50.0
        unit: deg
      - name: trim_loop_gain
        measurement: trim_gain_db
        operator: ">="
        threshold: 60.0
        unit: dB
      - name: trim_phase_margin
        measurement: trim_pm_deg
        operator: ">="
        threshold: 70.0
        unit: deg
      - name: buf1_loop_gain
        measurement: buf1_gain_db
        operator: ">="
        threshold: 70.0
        unit: dB
      - name: buf1_phase_margin
        measurement: buf1_pm_deg
        operator: ">="
        threshold: 80.0
        unit: deg
      - name: buf0_loop_gain
        measurement: buf0_gain_db
        operator: ">="
        threshold: 60.0
        unit: dB
      - name: buf0_phase_margin
        measurement: buf0_pm_deg
        operator: ">="
        threshold: 80.0
        unit: deg
```

- [ ] **Step 4: Write the three seeded-failure specs**

Each is a copy of `spec.yaml` with exactly ONE threshold tightened, so a run
must fix exactly one block. Every one was verified to fail at nominal and to
be reachable by a knob inside a single subckt.

`spec_seed_tc.yaml` — change `temperature_coefficient`'s threshold from `70.0`
to `30.0`, with this header:

```yaml
# tc_ppm_per_c <= 30 fails at nominal (measured 36.30). The knob is the
# BGR_CORE Rp/R1 ratio: XRpa/XRpb l = 324.74 -> 321.3 (Rp/R1 9.5 -> 9.4) gives
# 29.30. This is the COUPLED seed - moving Rp also moves vbgout (1.2389 ->
# 1.2334) and vbg1 (1.1999 -> 1.1947), so the tuner has to hold two other
# criteria inside their windows while fixing this one. Both resistors must
# move together; changing only one unbalances the two branches.
```

`spec_seed_trim_pm.yaml` — change `trim_phase_margin`'s threshold from `70.0`
to `85.0`, with this header:

```yaml
# trim_phase_margin >= 85 deg fails at nominal (measured 81.14). The only knob
# that reaches it is TRIMAMP.XRz's length: l=15 -> 81.1, l=25 -> 98.2,
# l=40 -> 124.0, l=60 -> 125.4, l=90 -> 88.3 deg. Note it is NOT monotone -
# overshooting the nulling resistor loses phase margin again. Meanwhile
# buf1_phase_margin moves only 101.56 -> 101.65, so a proposal aimed at a
# buffer is a targeting miss.
```

`spec_seed_buf0_droop.yaml` — change `vbg0_droop`'s threshold from `50.0` to
`15.0`, with this header:

```yaml
# vbg0_droop <= 15mV fails at nominal (measured 19.93) and localises cleanly
# to BUF_P: growing BUF_P.Xcl from 20x20 to 50x50 gives 12.50mV, while
# vbg1_droop moves only 24.12 -> 23.97mV. Growing BUF_N.Xcl instead changes
# vbg0 not at all. The 2.5x growth needs the area gate's 3.0x tier, which is
# why SKY130_GEOMETRY_TIERS' first boundary is 25um (Task 2).
```

- [ ] **Step 5: Write `spec_pvt.yaml`**

A copy of `spec.yaml` with a `pvt_corners:` block inserted **above**
`testbenches:` (top-level key order matters only for readability, but
`benchmarks/two_stage_opamp/spec_pvt.yaml` puts it first — match that).

```yaml
# The full +-10% supply axis. This is only reachable because every amplifier
# is a folded cascode: the input pair's drain sits a |Vdsat| below vdd rather
# than a |Vgs| below it, which is worth ~0.5V of input headroom. An earlier
# 5T-OTA version of this circuit had to be specified at +-5% because at 1.62V
# the trim loop gain collapsed to -45dB and vbg1 fell to 1.084V. If an amp is
# ever simplified back to a mirror load, this axis stops being achievable.
pvt_corners:
  process: ["tt", "ss", "ff", "sf", "fs"]
  voltage: [1.62, 1.8, 1.98]
  temperature: [-40, 27, 125]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_bandgap_spec.py -v`
Expected: 5 passed.

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add benchmarks/bandgap tests/unit/test_bandgap_spec.py
git commit -m "feat: add bandgap spec, PVT spec and two seeded-failure variants"
```

---

### Task 6: document the benchmark

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the benchmark to the "Benchmarks" section**

Insert after the `two_stage_opamp` entries:

```markdown
- `benchmarks/bandgap/` — five-block Kuijk bandgap reference chain
  (`BGR_CORE` + `ERRAMP` → `TRIMAMP` → resistor ladder → `BUF_N`/`BUF_P`),
  producing `vbg1`=1.2V and `vbg0`=0.5V. Unlike every other benchmark this
  one is **multi-block**, and its actual purpose is to measure whether the
  tuner changes the *correct* block: `spec_seed_tc.yaml`,
  `spec_seed_trim_pm.yaml` and `spec_seed_buf0_droop.yaml` each tighten
  exactly one criterion whose only fix lives in one subckt, and each was
  verified both solvable and localised (fixing `BUF_N` does nothing for
  `vbg0`). This is what made subckt-scoped refdes a prerequisite — four
  amplifiers in one netlist means refdes collision is the normal case.
  Every amplifier is a folded cascode plus a common-source output stage,
  which is also what lets the 45-corner `spec_pvt.yaml` run the full ±10 %
  supply axis: a plain mirror load leaves a 1.2V-common-mode input pair no
  saturation margin at 1.62V. Uses `pnp_05v5` and `res_high_po` in addition
  to the FETs; every capacitor is an nfet/pfet MOS cap, never MiM. See
  `docs/superpowers/specs/2026-07-26-bandgap-benchmark-and-scoped-refdes-design.md`
  ("Part 2 — as built" and "Part 2 revision") for the full corner table and
  for the design assumptions ngspice disproved along the way.
```

- [ ] **Step 2: Add the sky130 gotchas to "Known limitations / gotchas"**

```markdown
- **sky130 device models are binned and exceeding a bin is a hard error.**
  `wmax`/`lmax` are 100 µm. `W=120` aborts the run with `could not find a
  valid modelname` — not a warning, not a bad number. A tuning proposal that
  widens a `W=40` device by 3× reaches that limit.
- **`mult` on a sky130 `pnp_05v5` does nothing.** It scales only the model's
  mismatch terms, which are zero without Monte Carlo. The emitter-area ratio
  a bandgap needs is the *instance* multiplier `m=8`. `mult=8` yields
  ΔVbe ≈ 0.
- **An nfet MOS cap cannot float** — its body is the p-substrate, so one
  plate is pinned to `vss`. A Miller cap, which must sit between two signal
  nodes, has to be a *pfet* MOS cap (isolated nwell body). Measured
  densities and the bias each needs are in the bandgap design spec.
- **The first line of a SPICE deck is the title.** A `.temp` placed there is
  silently consumed and the run happens at 27 °C, producing corner data that
  looks plausible and is wrong.
- **`Lfb`/`Cin` loop breaking does not survive a cascode.** A 1 MH inductor is
  only a 6.3 MΩ open at 1 Hz, so against a folded cascode's tens of megohms
  the loop is never actually broken; raising it to 1 GH spans the matrix over
  ~20 decades and the solver returns garbage at many corners. Where the break
  point drives a MOS gate, use series voltage injection (`DC 0 AC 1`) and read
  the loop gain as `vdb(out)-vdb(in)` — exact, and with no reactive elements.
- **A cascoded amp with a CS output stage can latch itself off.** If the bias
  chain is allowed to collapse with the core, every CS stage's NMOS sink turns
  off while its PMOS is fully on, pinning each amp output HIGH; a startup
  pull-down then has to outfight a much larger PMOS and loses. Keep a trickle
  current in the bias chain (`benchmarks/bandgap`'s `BGR_CORE.Xsu_b`).
```

- [ ] **Step 3: Note the area-gate fix in the `area_limits.py` architecture bullet**

Append to that bullet:

```markdown
  Size tiers are keyed on *scaled geometry* for sky130 primitives: a deck with
  `.option scale=1.0u` writes bare `W=30` meaning 30 µm, and reading that as
  an absolute value put every PDK device in the unbounded 1.5× tier, making
  the tier table inert on exactly the benchmarks that use a real PDK.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document the bandgap benchmark and the sky130 gotchas it surfaced"
```
