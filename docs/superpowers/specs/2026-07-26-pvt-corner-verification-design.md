# PVT Corner Verification — Design

## Problem

`two_stage_opamp` now runs on the real SkyWater sky130 PDK (see
`docs/superpowers/specs/2026-07-26-sky130-pdk-migration-design.md`), but only
ever simulates at a single fixed point: `tt` (typical) process, `Vdd=1.8V`,
room temperature. There is no concept of a PVT (process/voltage/temperature)
corner anywhere in the project — no way to verify a design actually holds up
across the real range of manufacturing variation and operating conditions a
production tapeout would need to survive.

This adds real PVT corner sweeping to `two_stage_opamp`, per an
architectural constraint the user locked in before this design was written
(see project memory `project-pvt-corner-future`): **the first and final
verification passes must always run every PVT corner, no exceptions** —
corner-reduction techniques (surrogate models, DOE sampling, sensitivity-based
selection, active learning) may only be used to skip corners during
*intermediate* tuning iterations, never for initial characterization or the
final pass/fail verdict. The motivation is real production use at the user's
company, where running every criterion across every PVT combination on every
tuning iteration would be too resource-wasteful to be practical.

## Scope (V1)

- **Corner set**: 5 process corners (`tt`/`ss`/`ff`/`sf`/`fs` — sky130's
  full standard process-corner set, confirmed present in the vendored PDK)
  × 3 voltage points (`1.62V`/`1.8V`/`1.98V`, ±10% of `Vdd`) × 3 temperature
  points (`-40°C`/`27°C`/`125°C`) = **45 combinations**.
- **MiM capacitor RC corner (low/typical/high) is explicitly out of scope**
  for V1 — sky130 has this as a separate corner axis from the active-device
  process corner, but including it would multiply the combination count to
  135 and require extracting new low/high MiM parameter sets (only
  `typical` was extracted during the PDK migration). MiM caps stay fixed at
  `typical` for every combination in this sweep.
- **New spec file only**: `benchmarks/two_stage_opamp/spec_pvt.yaml`.
  `spec.yaml` and `spec_topology_required.yaml` are untouched — PVT
  verification is opt-in per spec file, not automatic for every benchmark
  (`inverting_amp`'s ideal VCVS has no device corners to sweep).
- **Same four netlist files, same thresholds as `spec.yaml`, reused as-is**
  — no new/duplicated netlist content, no new criteria. PVT verification's
  job is to check the *same* functional requirements hold across a wider
  condition set, not to define different requirements.
- **Corner sweep runs are deterministic, not LLM-driven**: process/voltage/
  temperature variation is 100% mechanical (swap an include file, change a
  source value, inject a `.temp` line) — no interpretation is needed, so
  each of the 45×4=180 sweep simulations calls `SimulatorBackend.run()`
  directly, bypassing the LLM-based simulator agent entirely. Only the
  *aggregated* worst-case result is ever shown to `judge` (still one LLM
  call per iteration, unchanged).
- **Mid-loop tuning iterations stay nominal-only** (`tt`/`1.8V`/`27°C`) —
  identical to today's behavior. `run_orchestration`'s existing tuning loop
  is not modified at all. PVT sweeping wraps around it: a full 45-corner
  sweep runs once before the loop starts (baseline characterization) and
  once after it ends (real final verdict), using direct `SimulatorBackend`
  calls, not the LLM agent loop.
- **A final full-PVT-sweep failure, even after nominal-only tuning
  converged to PASS, is reported as FAIL** — not silently promoted to PASS.
  This is the direct consequence of the locked "final pass always runs all
  corners" constraint: if the final verdict doesn't reflect every corner,
  it isn't trustworthy. V1 has no PVT-aware tuning to automatically fix a
  corner-specific failure, so this case is reported honestly with full
  per-corner diagnostic detail, for a human (or a later sub-project) to act
  on.
- **Explicitly deferred to a later sub-project** (per explicit user
  decision — V1's tuning loop is deliberately kept simple/nominal-only for
  now): corner-reduction techniques for accurate, resource-efficient
  mid-loop tuning, and automatic re-tuning triggered by a final-sweep PVT
  failure. This design only builds the *verification* capability (can we
  correctly detect a PVT failure across all 45 corners), not a PVT-aware
  *repair* capability.

## Architecture

### Corner data: sky130's real process-corner pairing

Confirmed directly against the vendored PDK's own `models/sky130.lib.spice`:
each of the 5 corner `.lib` sections (`tt`/`sf`/`ff`/`ss`/`fs`) includes the
*same*-suffixed nfet and pfet corner file — e.g. `.lib sf` includes both
`sky130_fd_pr__nfet_01v8__sf.corner.spice` and
`sky130_fd_pr__pfet_01v8__sf.corner.spice`. sky130 already encodes the
intended NMOS/PMOS skew combination per suffix; there is no need to
cross-pair (e.g. nfet `ss` with pfet `ff`) manually. The mismatch-parameter
include (`..._mismatch.corner.spice`) and `lod.spice` are corner-independent
— included identically regardless of which process corner is active,
confirmed by the same `.lib` sections all referencing the identically-named
mismatch file.

### `spec_pvt.yaml` schema

```yaml
circuit_name: two_stage_opamp
pvt_corners:
  process: [tt, ss, ff, sf, fs]
  voltage: [1.62, 1.8, 1.98]
  temperature: [-40, 27, 125]
testbenches:
  # identical to spec.yaml's four testbenches - same netlist files, same
  # control_block text, same criteria/thresholds. Reused, not duplicated.
```

`spec.py` gains a `PVTCorners` dataclass (`process: list[str]`,
`voltage: list[float]`, `temperature: list[float]`) and
`TargetSpec.pvt_corners: PVTCorners | None`, defaulting to `None` when the
key is absent (every existing spec file's behavior is unchanged).

### Corner-specific PDK include files

`pdk_corner.inc` (the `tt` corner) is unchanged and reused as-is for the
`tt` sweep points. Four new files are added alongside it:
`pdk_corner_ss.inc`, `pdk_corner_ff.inc`, `pdk_corner_sf.inc`,
`pdk_corner_fs.inc` — each byte-identical to `pdk_corner.inc` except the two
`nfet_01v8`/`pfet_01v8` corner-file include lines, which point at that
corner's `.corner.spice` files instead of `tt`'s. This mirrors the
project's already-established pattern of accepting explicit, small file
duplication (the four testbench netlists' byte-identical subckt bodies)
over a parametrized/templated single file — greppable, individually
testable, no risk of a stray template placeholder leaking into a real run.

### `render_corner_netlist`

A new function in `src/analogcoder/pvt.py` performs the three corner
substitutions on a netlist's text:

```python
def render_corner_netlist(netlist_text: str, process: str, voltage: float, temperature: float) -> str:
```

- **Process**: replaces `.include "pdk_corner.inc"` with
  `.include "pdk_corner_{process}.inc"` (a no-op replacement for `process="tt"`,
  which keeps using `pdk_corner.inc`).
- **Voltage**: a dedicated regex specifically targeting the `Vdd` source
  line's `DC <value>` token — **not** the existing `apply_changes()`
  (`src/analogcoder/netlist.py`), which was checked against
  `netlist_psr_plus.cir`'s `Vdd vdd 0 DC 1.8 AC 1` line and found unsafe:
  `apply_changes`'s generic positional-token targeting for `param="value"`
  would replace the *last* positional token (the AC magnitude, `1`), not
  the DC voltage, on any line with a trailing `AC` clause. `apply_changes`
  is designed for LLM tuning-proposal application and correctly assumes a
  single trailing value token — sourcing corner voltage needs a purpose-built,
  narrower substitution that only touches the token immediately following
  the `DC` keyword.
- **Temperature**: injects a `.temp <value>` line into the netlist body
  (SPICE `.temp` can appear anywhere at the top level, outside `.subckt`).
  The exact injection point and confirmation this works as expected needs a
  real-ngspice check during implementation (not yet verified empirically as
  of this design).

### Full-sweep orchestration wrapper

A new function, e.g. `run_full_pvt_sweep(netlist_texts, spec, sim_backend) -> PVTSweepResult`,
called from `cli.py`'s `_run()`:

1. For each of the 45 `(process, voltage, temperature)` combinations × each
   of the 4 testbenches (180 total `SimulatorBackend.run()` calls): render
   the corner netlist, write it to a temp path, run it, collect
   measurements.
2. For each criterion (from `spec.all_criteria`), compute the worst-case
   value across all 45 corner results for that criterion's testbench: the
   *minimum* observed value if the criterion's operator is `>=`, the
   *maximum* if `<=`. Track which corner produced the worst-case value, for
   diagnostics.
3. Return an aggregated result: worst-case measurements (in the same shape
   `judge_measurements` already expects) plus a per-criterion
   worst-case-corner breakdown for logging/reporting.

`_run()` calls this once with `initial_netlist_texts` before invoking
`run_orchestration` (baseline characterization, logged but not gating —
`run_orchestration`'s nominal-only loop proceeds regardless of the baseline
sweep's outcome, exactly as it does today with no PVT awareness), and once
more with the converged final netlist texts after `run_orchestration`
returns. If the final sweep's worst-case measurements fail any criterion,
the reported final `status` is overridden to `FAIL` (even if
`run_orchestration`'s own nominal-only result was `PASS`), with the
per-corner breakdown attached to the result for diagnosis.

`run_orchestration` and everything inside it (`orchestrator.py`) is
untouched by this design.

## Known Limitations / Deferred Work

- **No PVT-aware tuning or automatic re-tuning on final-sweep failure** —
  explicitly deferred to a later sub-project, per user decision. V1 can
  *detect* a PVT failure the nominal-only loop never saw; it cannot fix one
  automatically.
- **No corner-reduction techniques** (surrogate models, DOE, sensitivity-based
  selection, active learning) — also deferred to that same later
  sub-project. Mid-loop iterations stay nominal-only in V1, which is what
  makes the "always full sweep at start/end" constraint affordable to
  satisfy without also needing reduction for the loop itself.
- **MiM capacitor RC corner not swept** — fixed at `typical`, a separate
  corner axis from the active-device process corner sky130 exposes but this
  design doesn't yet cover.
- **Given the current sizing's already-thin margins at nominal** (e.g. the
  nulling-resistor topology's `phase_margin` at 62.88° against a 60°
  threshold — only 2.88° of nominal margin), **the first real 45-corner
  sweep against the current sizing is expected to surface real failures**,
  not a bug in the sweep itself. This design's job is to build correct
  verification, not to pre-emptively widen component values to survive
  corners that haven't been measured yet — any resulting circuit changes
  are out of scope here and belong to the deferred re-tuning sub-project.

## Testing

- `render_corner_netlist`: pure text-transformation unit tests, no ngspice —
  verify each of the three substitutions targets exactly the right line/token
  (including the `netlist_psr_plus.cir`-style trailing-`AC`-clause case that
  ruled out reusing `apply_changes`).
- The four new corner-specific PDK include files (`pdk_corner_ss.inc` etc.):
  real-ngspice smoke tests per file, mirroring the existing
  `test_pdk_corner_ngspice.py` pattern (nfet/pfet/MiM cap all load cleanly).
- Worst-case aggregation (operator-direction-aware min/max selection): pure
  Python unit tests against synthetic per-corner measurement data — no need
  to run all 45 real corners to verify this logic.
- A representative subset of real corners (nominal `tt`/`27°C`/`1.8V`, plus
  a small number of the combinations expected to be most stressing) gets
  real-ngspice regression tests; the full 45×4=180-run sweep is exercised
  once via manual end-to-end validation (`analogcoder --spec spec_pvt.yaml`),
  not in the committed unit suite, to keep the suite fast.
