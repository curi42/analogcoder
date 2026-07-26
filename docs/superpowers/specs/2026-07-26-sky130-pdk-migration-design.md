# sky130 PDK Migration — Design

## Problem

The planned PVT (process/voltage/temperature) corner-verification project
(see project memory `project_pvt_corner_future`) requires **real** process
corners — not synthetic ones. `two_stage_opamp`, the only benchmark in this
project with device-level detail, currently uses generic ngspice level-1
`.model` devices (no PDK, no binned corner models, no real geometry
constraints). There is nothing to select a "corner" *from*.

This spec covers migrating `benchmarks/two_stage_opamp/` from generic
level-1 devices to the SkyWater sky130 open-source PDK, in place. PVT-corner
verification itself (selecting among tt/ss/ff/etc. corners, temperature and
voltage sweeps, the full-sweep-at-start/end constraint) is explicitly out of
scope — a separate, later spec that this migration exists to unblock.

## Scope (v1)

- **PDK**: SkyWater sky130, `nfet_01v8`/`pfet_01v8` core devices only (no
  RF/ESD/high-voltage variants), `tt` (typical) corner only — corner
  *selection* is next project's scope, this one only needs corner data to
  exist and be swappable.
- **Single 1.8V core supply** (`Vdd=1.8V`, `Vss=0V` hard ground), replacing
  the original ±2.5V split supply. sky130's core devices are 1.8V-rated;
  matching the original ±2.5V (5V total swing) split-supply scheme isn't a
  real use of this PDK.
- **Both `topologies.py` topologies migrated**: `miller_basic` (no nulling
  resistor) and `miller_nulling_resistor` (adds `Rz` in series with `Cc`).
  Both are re-sized and re-validated against real sky130 devices, not just
  the primary one.
- **`psr_minus` redefined as a GND-bounce test**: the original benchmark's
  `psr_minus` stimulated a separate negative supply rail (`Vss=-2.5V`).
  With `Vss` now hard ground, there is no negative rail to stimulate. The
  criterion is kept (not dropped) by injecting the AC stimulus directly on
  the `Vss` node itself — measuring how much ground noise/bounce couples to
  `vout`.
- **All 7 criteria's thresholds re-derived from real sky130 measurements**
  (`dc_gain`, `unity_gain_bandwidth`, `phase_margin`, `psr_plus`,
  `psr_minus`, `settling_time_hi`, `settling_time_lo`) — not inherited from
  the generic-device benchmark's numbers, which assumed a completely
  different device technology and supply scheme.
- **Corner-selection layer centralized, device-primitive layer not**: every
  testbench netlist and both `topologies.py` subckt bodies `.include` one
  shared file (`pdk_corner.inc`) for all PDK/corner data. Swapping to a
  different corner, or later to a company-internal PVT corner file
  (delivered as an HSPICE `corner.inc`-style `.inc`), means editing that one
  file. Device *primitive names* (`sky130_fd_pr__nfet_01v8`, the MiM cap
  subckt names) are **not** abstracted in this pass — a future PDK swap
  still requires touching every `X<n>` instantiation line. This is a
  deliberate scope cut: device-name indirection is only worth building when
  an actual second PDK swap is imminent, not speculatively now.
- **Overwrite `benchmarks/two_stage_opamp/` in place** — no parallel
  `two_stage_opamp_sky130/` directory. The generic-device version is fully
  replaced; nothing at the old generic-device benchmark remains after this
  migration lands.
- Not in scope: PVT corner sweeping/selection (next project),
  device-primitive-name abstraction (deferred), any change to the
  `inverting_amp` benchmark (ideal VCVS, no devices to migrate).

## PDK Vendoring

Vendored as a git submodule at
`third_party/skywater-pdk-libs-sky130_fd_pr`
(`https://github.com/google/skywater-pdk-libs-sky130_fd_pr.git`), shrunk
from a 668MB full checkout to a 24MB working tree via
`git sparse-checkout init --cone` +
`git sparse-checkout set models cells/nfet_01v8 cells/pfet_01v8 cells/cap_mim_m3 tech`.
The `.git/modules/.../` object store itself stays ~114MB regardless of
sparse-checkout (matches the GitHub API's reported repo size) — this is
submodule metadata, not working-tree bloat, and isn't reducible further
without losing the ability to `git submodule update` cleanly.

## Architecture

### Corner/device include layering

`benchmarks/two_stage_opamp/pdk_corner.inc` (already created, see file for
full content) is the single include point every testbench netlist and both
`topologies.py` subckt definitions pull PDK/corner data from:

```
.option scale=1.0u
.include ".../models/parameters/lod.spice"
.include ".../cells/nfet_01v8/sky130_fd_pr__nfet_01v8__tt.corner.spice"
.include ".../cells/nfet_01v8/sky130_fd_pr__nfet_01v8__mismatch.corner.spice"
.include ".../cells/pfet_01v8/sky130_fd_pr__pfet_01v8__tt.corner.spice"
.include ".../cells/pfet_01v8/sky130_fd_pr__pfet_01v8__mismatch.corner.spice"
```

`.option scale=1.0u` is mandatory — without it, `W=`/`L=` values on device
instantiation lines are interpreted as meters, not microns, and every
device falls outside all defined bin windows (`could not find a valid
modelname`). `mismatch.corner.spice` is required even for a nominal `tt`
run — several `_slope`/`_slope1` process-sensitivity parameters are
referenced unconditionally inside the binned BSIM4 model cards, and the
official `sky130.lib.spice`'s `.lib tt` section includes it too.
`models/parameters/lod.spice` supplies `_diff` (length-of-diffusion stress)
parameters referenced the same way.

### MiM capacitor include recipe

The compensation capacitors (`Cc`, `Ca`) become sky130 MiM caps
(`sky130_fd_pr__cap_mim_m3_1`), a real user decision (not the recommended
"keep them ideal" option) — the migration should exercise real PDK
parasitics on the compensation network, not just the transistors.

The *official* MiM cap include chain
(`models/r+c/res_typical__cap_typical.spice` →
`models/sky130_fd_pr__model__r+c.model.spice`) pulls in an entire unrelated
resistor-cell family (`cells/res_generic_nd/...`) not needed for this
migration and not in the sparse-checkout. Rather than expand the
sparse-checkout for one family this project doesn't use, the ~10 needed
scalar parameters were hand-extracted into a minimal self-contained
`.param` block, verified via a clean ngspice `.op` run with no missing
includes:

```
.param tol_m3=0.0
.param rm3=0.047 rcvia3=3.41
.param tc1rm3=3.424e-3 tc2rm3=-7.739e-7
.param tc1rvia3=2.366e-3 tc2rvia3=-1.025e-5
.param m3_dw=-0.025u
.param camimc=2.00e-15 cpmimc=0.19e-15
.include ".../cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_1.model.spice"
.include ".../cells/cap_mim_m3/sky130_fd_pr__cap_mim_m3_2.model.spice"
```

Capacitance: `C = camimc*w*l + cpmimc*2*(w+l)` (verified to ~0.5% accuracy
against ngspice `.op`). Instantiation:
`X<n> <plus> <minus> sky130_fd_pr__cap_mim_m3_1 w=<um> l=<um> mf=<int>`.

This block gets folded into `pdk_corner.inc` (making it a combined
PDK-and-passives include, not just device models) so every testbench and
both topologies pull it from the same single file.

### Circuit changes (both topologies)

- **Input/output common-mode: 0.55V**, not mid-rail (0.9V). The PMOS input
  pair needs roughly 1.0–1.2V of Vsg for reasonable current at sky130's
  real threshold voltage, which isn't available at mid-rail on a 1.8V
  single supply with the pair's source sitting near Vdd. This is a real,
  validated constraint of this topology on this PDK, not an arbitrary
  choice.
- **Self-biased, resistor-referenced current mirror** replaces the original
  ideal-current-source-fed diode-connected-PMOS bias generator (`Iref`/`M9`
  in the generic-device version). An ideal `Iref` has no sky130 equivalent;
  a self-biased loop (`Xp3`/`Xp4`/`Xn1`/`Xn2` mirror pair + `Rdeg=20k`
  degeneration resistor) generates the bias current from the supply itself.
  Requires `Rstart=3Meg` (Vdd→internal `nbias` node) as a **permanent part
  of the reference**, not a simulation-only workaround — without it the
  loop can converge to a degenerate near-zero-current solution.
  - **Known characteristic, not fixed by this migration**: this reference
    topology is bimodal — depending on exact `Rdeg`/`Rstart` values it can
    settle into one of two disconnected stable current bands (~1.7–3.5µA
    "low" or ~53–75µA "high"), with adjacent parameter values sometimes
    landing on opposite bands. The shipped `Rdeg=20k`/`Rstart=3Meg`
    combination is verified (via the measurements below) to land in the
    stable, usable band — but this is an empirical property of the
    specific values, not a guarantee that nearby values also work. Anyone
    tuning `Rdeg`/`Rstart` in this reference should re-verify the resulting
    bias current, not assume monotonic/continuous behavior.
- **`M6:M7` (second-stage NMOS driver : PMOS load) width ratio**, not
  absolute size, is what controls PSR. Narrowing the PMOS load relative to
  the NMOS driver (the shipped ratio is `W6=8µm` / `W7=30µm`) flips both
  `psr_plus` and `psr_minus` from positive dB (the supply/ground ripple is
  *amplified* toward `vout`) to negative dB (real rejection) — the Vdd-to-
  `vout` resistive divide this stage forms is skewed by the relative
  `rds` of the driver vs. the load.
- **Refdes prefix**: sky130 primitives are subckt calls (`X<n>
  ... sky130_fd_pr__...`), not raw `.model`-referencing lines (`M<n>
  ... PMOSG`). This is a mechanical consequence of using a real PDK, not a
  design choice — but it breaks `area_limits.py` (see below).

### `area_limits.py` needs a fix (implementation-plan item, not done here)

`TIERS_BY_CTYPE = {"M": TRANSISTOR_TIERS, "C": CAPACITOR_TIERS, "R":
RESISTOR_TIERS}` (`src/analogcoder/area_limits.py:27`) classifies a
component's device type from its refdes's first letter. Every sky130
primitive in this migration — both transistors and MiM caps — is
`X`-prefixed, so `allowed_multiplier_for` (`area_limits.py:34`) returns
`None` for all of them via `TIERS_BY_CTYPE.get(ctype)`, silently disabling
the area-growth gate for this entire benchmark. The fix must classify by
the instantiated subckt/model name (e.g. matching
`sky130_fd_pr__{n,p}fet_01v8` vs. `sky130_fd_pr__cap_mim_m3_*` in the
instantiation line), not the refdes prefix. This is a required task in the
implementation plan — this benchmark cannot ship with the area gate
silently disabled.

### Testbenches

Four testbenches, mirroring the existing PSR/settling-time multi-testbench
architecture (`TargetSpec.testbenches` in `spec.py`) — unchanged
infrastructure, only netlist/threshold content changes:

**`ac_loop_gain`** (`netlist.cir`) — same `Lfb`/`Cin`/`Vstim` AC-loop-break
topology as today, `set units=degrees` before the `ac` analysis (the
project's established phase-margin convention —
`meas ac phase_margin_deg find vp(vout) when vdb(vout)=0`, no `cross=`
qualifier). Produces `gain_db`, `ugbw_hz`, `phase_margin_deg`.

**`psr_plus`** (`netlist_psr_plus.cir`) — `AC 1` on `Vdd`, same
`Lfb`/`Cin`/`Vstim` topology as `ac_loop_gain` (not a simplified
input-tied-to-output wiring — verified this matters, see Validation).
Produces `psr_plus_db`.

**`psr_minus`** (`netlist_psr_minus.cir`) — `AC 1` on `Vss` (the
GND-bounce redefinition), same loop topology. Produces `psr_minus_db`.

**`settling_time`** (`netlist_settling.cir`) — closed-loop unity-gain
buffer (`vout` wired to `vinn`), `PULSE(0.4 0.7 1u 1n 1n 10u 20u)` at
`vinp` (a 0.3V step centered near the 0.55V input CM, not the original
benchmark's 1V step within ±2.5V rails). Band edges for the `CROSS=LAST`
settling measurements are `0.70398`/`0.69698` (±~0.5% around the ~0.700V
settled value for this step). Produces `t_hi_last`, `t_lo_last`.

All four `.subckt OPAMP2STAGE` bodies stay byte-identical at every point,
same invariant as today.

### Re-derived thresholds

Derived from the real measurements in Validation below, using the same
"validated real value + sensible margin" philosophy as the existing PSR/
settling-time thresholds (not inherited from the generic-device numbers,
which assumed a different device technology entirely):

| Criterion | Threshold | Rationale |
|---|---|---|
| `dc_gain` | `>= 60.0 dB` | measured 71.09dB on both topologies, ~11dB margin |
| `unity_gain_bandwidth` | `>= 1,500,000 Hz` | measured 2.08MHz (main) / 2.73MHz (nulling); sky130 L=0.5µm sizing at these currents can't reach the original 20MHz target — 4 rounds of parameter search (documented in Validation) confirm this isn't reachable without a longer-channel-length redesign, out of scope here |
| `phase_margin` | `>= 60.0 deg` | kept at the original value **deliberately** — the main topology (34.56°) fails this by design, so the topology-swap mechanism (`TOPOLOGY_SWITCH_THRESHOLD`, `orchestrator.py:12`) has a genuine trigger; the nulling-resistor topology (62.88°) passes with a real (if thin) margin |
| `psr_plus` | `<= -10.0 dB` | kept at the original value; both topologies measure -15.40dB, 5.4dB margin |
| `psr_minus` (GND bounce) | `<= 0.0 dB` | re-derived — both topologies measure -1.43dB. The original -8dB threshold assumed a floating negative rail; with Vss hard-grounded and no cascode on `M6`'s source, that level of rejection isn't physically available here. `<=0dB` still requires genuine attenuation (not amplification) of ground noise, which is a real, meaningful, and honestly-achievable bar for this topology |
| `settling_time_hi` / `settling_time_lo` | `<= 2.8e-6 s` (both) | re-derived — worst measured value across both topologies is 2.47µs (main topology `t_hi_last`); 2.8µs gives ~13% margin above it while still being a real constraint (nulling topology's 2.11µs/1.56µs pass with more room) |

## Nulling-resistor topology: `Rz` validation

The generic-device benchmark's `miller_nulling_resistor` topology uses
`Rz=500Ω` (`topologies.py:61`) — not reusable as-is, since sky130's device
impedances at the currents this reference produces are entirely different
from the generic level-1 devices' scale. Validated directly (real ngspice,
not delegated) by sweeping `Rz` from 1Ω to 1MΩ in the finalized sky130
`OPAMP2STAGE` subckt (same subckt as the shipped `miller_basic`, with `Cc`
routed through `Rz` to `vout` instead of directly):

| `Rz` | `phase_margin_deg` | `ugbw_hz` |
|---|---|---|
| 1Ω (≈none) | 34.56 | 2.08M |
| 50,000 | 43.39 | 2.11M |
| 120,000 | 54.23 | 2.24M |
| 175,000 | 60.44 | 2.46M |
| **220,000** | **62.88** | **2.73M** |
| 250,000 | 62.79 | 2.94M |
| 400,000 | 49.03 | 3.93M |
| 1,000,000 | 18.76 | 4.63M |

Peaks around `Rz≈220–225kΩ`, then falls off (classic RHP-zero
over-cancellation past the peak). **`Rz=220kΩ`** (a standard E24 resistor
value, at the measured peak) is the shipped value — chosen because it
improves phase margin *and* bandwidth simultaneously relative to the
no-`Rz` baseline, rather than the usual nulling-resistor trade-off of
bandwidth for phase margin.

`Rz` was confirmed **provably orthogonal to both PSR criteria**, not just
empirically identical across the sweep: `Cc`'s impedance at the 1Hz PSR
measurement frequency (`|Zc| ≈ 1/(2π·1Hz·0.30pF) ≈ 5.3×10¹¹Ω`) is ~2.4
million times larger than the largest `Rz` tested (220kΩ), so `Rz` cannot
materially affect the psr_plus/psr_minus measurement regardless of its
value. `psr_plus_db`/`psr_minus_db` measured bit-identical (-15.4047dB /
-1.43298dB) at `Rz=1Ω` and `Rz=220,000Ω`.

## Known Limitations / Deviations

- **0.55V input/output common-mode**, not mid-rail (0.9V) — a real
  constraint of the PMOS input pair's headroom at sky130's threshold
  voltage on a single 1.8V supply, not an arbitrary choice.
- **`unity_gain_bandwidth` threshold (1.5MHz) is far below the original
  generic-device benchmark's (20MHz)** — sky130 at the currents/lengths
  this design uses cannot reach the original target; a longer first-stage
  channel length would likely help but is a structural redesign outside
  this migration's resizing-only scope.
- **`settling_time` thresholds (2.8µs) are ~2.3x looser than the original
  (1.2µs)** for the same bandwidth reason.
- **Self-biased reference is bimodal** (see Circuit changes above) — a
  genuine circuit-design characteristic of this reference topology, not
  smoothed out within this migration's scope. The shipped `Rdeg`/`Rstart`
  values are verified to land in the usable band; don't assume nearby
  values do too.
- **Device-primitive-name abstraction deferred** — a future real PDK swap
  (e.g. the user's company's own PDK) will still require touching every
  `X<n>` device instantiation line across every netlist and both
  `topologies.py` subckt bodies. Only the corner-selection layer
  (`pdk_corner.inc`) is a single-file swap point.
- **`area_limits.py`'s refdes-based device classification is broken** by
  this migration (X-prefixed devices aren't recognized) — must be fixed as
  part of the implementation plan, not deferred further; shipping this
  migration with the area gate silently disabled is not acceptable.

## Validation

All measurements are real ngspice runs (not estimated), on the exact final
`OPAMP2STAGE` subckt bodies this migration ships.

**`miller_basic` (main topology, no `Rz`):**

| Criterion | Measured | Threshold | Pass? |
|---|---|---|---|
| `dc_gain` | 71.0861 dB | ≥60.0 dB | ✅ |
| `unity_gain_bandwidth` | 2,079,550 Hz | ≥1,500,000 Hz | ✅ |
| `phase_margin` | 34.5636° | ≥60.0° | ❌ (by design — triggers topology-swap path) |
| `psr_plus` | -15.4047 dB | ≤-10.0 dB | ✅ |
| `psr_minus` | -1.43298 dB | ≤0.0 dB | ✅ |
| `settling_time_hi` | 2.46659 µs | ≤2.8 µs | ✅ |
| `settling_time_lo` | 2.26099 µs | ≤2.8 µs | ✅ |

**`miller_nulling_resistor` (`Rz=220kΩ`):**

| Criterion | Measured | Threshold | Pass? |
|---|---|---|---|
| `dc_gain` | 71.0861 dB | ≥60.0 dB | ✅ |
| `unity_gain_bandwidth` | 2,728,120 Hz | ≥1,500,000 Hz | ✅ |
| `phase_margin` | 62.8821° | ≥60.0° | ✅ |
| `psr_plus` | -15.4047 dB | ≤-10.0 dB | ✅ |
| `psr_minus` | -1.43298 dB | ≤0.0 dB | ✅ |
| `settling_time_hi` | ~2.11 µs | ≤2.8 µs | ✅ |
| `settling_time_lo` | ~1.56 µs | ≤2.8 µs | ✅ |

This gives the migration a genuine, real topology-swap story end to end:
the main topology fails exactly one criterion (`phase_margin`) by a wide,
unambiguous margin, and the alternate topology — with an `Rz` value
independently re-derived for sky130, not reused from the generic-device
benchmark — passes all seven. Four rounds of real-ngspice parameter search
on the main topology (widening/narrowing `M6`/`M7`/`Cc`/`Ca`, a PSR-fix
cascode attempt, a full self-biased reference redesign, a current-budget
rebalance) never found a parameter-only combination reaching
`phase_margin≥60°` without regressing PSR or gain below threshold — unlike
the generic-device `spec_topology_required.yaml` benchmark, where a real
Claude run found a parameter-only escape hatch the topology-swap mechanism
was designed for. This sky130 migration is expected to be a much more
reliable real trigger for the topology-swap path.
