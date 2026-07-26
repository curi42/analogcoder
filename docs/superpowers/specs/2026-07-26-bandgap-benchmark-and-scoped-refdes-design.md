# Bandgap Reference Benchmark + Subckt-Scoped Refdes Addressing — Design

## Problem

Every benchmark in this project is a **single-block** circuit: `inverting_amp`
is one VCVS, `two_stage_opamp` is one `.subckt OPAMP2STAGE` plus a testbench
harness. Real analog IP is not shaped like that. A production bandgap
reference is a chain of distinct blocks — a Kuijk core, an error amplifier, a
trim amplifier with a resistor ladder, and output buffers — and the useful
question about an automated tuner is not "can it turn one knob" but **"in a
circuit with many blocks, does it change the right one?"**

Nothing in the project answers that today, and worse, nothing *can*: the
netlist layer cannot address a component inside a specific subckt at all.

This design adds both halves — the addressing capability that makes
block-targeted tuning expressible, and a multi-block bandgap benchmark that
makes it measurable.

The user's stated purpose is explicit and shapes every decision below:
**specs do not need to be tight.** The circuit structure should resemble what
they actually use, and the benchmark exists to check that tuning targets the
correct part of a complex structure. Precision work happens later against
their real company circuit, not here.

## Part 1 — Subckt-scoped refdes addressing

### The current breakage

`parse_netlist` records subckt membership, but `apply_changes` and
`index_baseline_components` both ignore it. Verified against a two-subckt
netlist where each subckt contains its own `X1` and `Xcc`:

```
--- apply W=99 to refdes "Xcc", intending BUF_N's ---
   Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=99    <- BUF_P's changed
   Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20    <- BUF_N's untouched
```

Two distinct defects:

1. **`apply_changes` edits only the first match** and silently succeeds.
   There is no error, no warning, and the returned netlist looks plausible.
2. **BUF_N's `Xcc` is unaddressable.** No value of `refdes` can reach it.
   A tuner cannot express "increase the compensation cap in the vbg1
   buffer" even in principle.

`area_limits.index_baseline_components` has the same root cause in a
different form — it flattens every subckt's components into
`{c.refdes: c for c in components}`, so a duplicate refdes silently
last-wins and the area gate compares a proposal against a different
device's baseline.

CLAUDE.md documents this as a deliberately deferred limitation ("a refdes
collision between a subckt-local and top-level component could misfire").
That deferral is no longer tenable: with four amplifiers in one netlist,
refdes collision is the normal case, not an edge case.

### Design

Introduce a scoped refdes form: **`<subckt_name>.<refdes>`** for components
inside a subckt definition; plain **`<refdes>`** continues to address
top-level components.

The scope is the **subckt definition, not the instance.** Editing a subckt
body changes every instance of it — that is what SPICE actually means, and
modelling anything else would require splitting subckts into per-instance
copies. This is sufficient here because the benchmark's amplifiers are
already distinct subckts by role (see Part 2), so per-instance
differentiation is never needed. If a future circuit needs two differently
tuned instances of one subckt, the answer is to split the subckt, not to
add instance-scoped addressing.

Modules affected, in dependency order:

| Module | Change |
|---|---|
| `netlist.py` | `apply_changes` targets a scoped refdes; ambiguous unscoped refdes matching multiple scopes is an **error**, not a silent first-match |
| `area_limits.py` | `index_baseline_components` keys by scoped refdes |
| `schemas.py` | `TUNER_SCHEMA`'s refdes pattern permits `.` |
| `agents/analyzer.py` | reports `tunable_params` with scoped refdes |
| `agents/verifier.py` | `verify_pre` validates the scope exists |

An unscoped refdes that matches exactly one component anywhere stays valid,
so existing single-subckt benchmarks and their tests are unaffected.

## Part 2 — Bandgap reference chain benchmark

### Architecture

```
Kuijk core ──vbgout (zero-TC, ~1.16V)──> [TRIMAMP] ──Vtop (~1.5V)
                                            ^              |
                                            +--feedback tap-+  resistor ladder
                                                            |
                                                            +- 1.2V tap -> [BUF_N] -> vbg1
                                                            +- 0.5V tap -> [BUF_P] -> vbg0
                                                            +- gnd
```

The trim amplifier is non-inverting; the loop closes when the ladder's
feedback tap equals `vbgout`, so `Vtop = vbgout · (R_total / R_below_fb)`.
Other taps on the same ladder produce the 1.2V and 0.5V targets, and each is
buffered because a ladder tap cannot drive load.

**Trim is fixed at nominal** — a single hard-wired feedback tap. Trimming
compensates manufacturing spread after the fact; this project verifies the
design's PVT robustness, which is orthogonal. Modelling trim codes would
multiply every corner by a code sweep and introduce a discrete-variable
tuning problem that nothing else in the project needs.

### Kuijk core theory

With equal resistors `Rp` from `vbgout` to nodes A and B, the error amplifier
forcing `V(A) = V(B)` makes both branch currents equal, so:

```
I·R1 = Vbe(Q1) − Vbe(Q8) = VT·ln(8)     ->  I = VT·ln(8)/R1     (PTAT)
vbgout = Vbe(Q1) + (Rp/R1)·VT·ln(8)
```

Zero TC requires `(Rp/R1)·ln(8)·(k/q) ≈ −dVbe/dT ≈ 1.7 mV/°C`, giving
`Rp/R1 ≈ 9.5` and `vbgout ≈ 1.16 V`.

This yields a genuinely coupled three-knob structure, which is the point —
`two_stage_opamp`'s single `Cc` knob was too easy:

| Knob | TC | vbgout | Iq |
|---|---|---|---|
| `Rp/R1` ratio | dominant | moves with it | — |
| `R1` absolute | ~none | ~none | dominant |
| PNP `mult` ratio | dominant | moves with it | — |

Fixing TC pushes `vbgout` out of its window, so the tuner must move two knobs
together.

### Device choices

All three are verified present in the vendored sky130 submodule but absent
from its sparse-checkout, so Part 2 begins by adding them:

- **`sky130_fd_pr__pnp_05v5_W3p40L3p40`** — 4-terminal (`c b e s`) with a
  `mult` parameter, so the 1:8 emitter-area ratio is `mult=8`. `bf = 16.6`:
  base current is not negligible, which is a real and classic bandgap error
  source rather than a modelling artifact.
- **`sky130_fd_pr__res_high_po`** — `rsheet = 317.4 Ω/sq` with real
  temperature coefficients in the model (`tc1 = -4.3e-4`, `tc2 = 12e-6`).
  `Rp` and `R1` use the same resistor type so their ratio cancels resistor
  TC to first order, exactly as a real design relies on.
- **Capacitors are `nfet_01v8` with D/S/B tied** (MOS caps), not MiM. This
  matches the user's actual practice. `area_limits._classify_ctype` matches
  `"fet"` and classifies these as `"M"`, which is geometrically correct for
  area purposes and needs no change.

**`pnp_05v5` is currently unconstrained by the area gate.** Its model name
contains none of the `fet`/`cap`/`res` markers in
`area_limits._SKY130_CTYPE_MARKERS`, so `_classify_ctype` returns `"X"` and
`allowed_multiplier_for` returns `None`. Since PNP emitter area ratio is a
tuning knob here, Part 2 must add a `"pnp"` marker and a tier for it.

### Amplifier roles are forced by input common-mode headroom

At `Vdd = 1.8V`, with sky130 threshold voltages, a PMOS input pair tops out
near `Vdd − |Vgs_p| − Vdsat_tail ≈ 0.85V` and an NMOS input pair bottoms out
near `Vgs_n + Vdsat_tail ≈ 0.85V`:

| Amp | Input CM | Required pair |
|---|---|---|
| `ERRAMP` | ~0.65V (Vbe) | PMOS |
| `TRIMAMP` | ~1.16V (vbgout) | NMOS |
| `BUF_N` (vbg1) | 1.2V | NMOS |
| `BUF_P` (vbg0) | 0.5V | PMOS |

The two buffers therefore cannot share a topology. `two_stage_opamp` has a
PMOS input pair and is reused for `ERRAMP` with its self-bias loop
(`Xp3`/`Xp4`/`Xn1`/`Xn2`/`Rdeg`/`Rstart`) removed and biased instead from the
core's PTAT current, so the whole loop shares one zero-current degenerate
state and the startup criterion becomes real.

This is not a stylistic choice. A single shared buffer topology would leave
`vbg1`'s buffer dead across corners.

### Testbenches and criteria

| Testbench | Analysis | Criteria |
|---|---|---|
| `tc` | `dc temp -40 125 1` | `tc_ppm_per_c`, `vbgout_v`, `iq_ua` |
| `startup` | `tran`, Vdd ramp | `startup_time` |
| `psrr` | `ac`, AC on Vdd | `psrr_dc`, `psrr_hf` |
| `linereg` | `dc Vdd 1.62 1.98` | `line_regulation` |
| `dc_out` | `op` / `dc` | `vbg0_v`, `vbg1_v` |
| `settling` | `tran`, load step | `vbg0_settling`, `vbg1_settling` |
| `loop_erramp` | `ac`, broken loop | `erramp_gain`, `erramp_pm` |
| `loop_trimamp` | `ac`, broken loop | `trimamp_gain`, `trimamp_pm` |
| `loop_buf_n` / `loop_buf_p` | `ac`, broken loop | per-buffer gain/PM |

Loop-gain testbenches reuse the `Lfb`/`Cin` loop-breaking harness already in
`benchmarks/two_stage_opamp/netlist.cir`.

### Derived measurements

TC is a derived quantity. Two mechanisms were tested against real ngspice
before committing to this design:

- **`meas dc <name> param='<expr>'` does not work.** It evaluates the
  expression correctly but then reports `measure '<name>' failed` and emits
  no measurement line.
- **`let <name> = <expr>` followed by `print <name>` works**, emitting
  `tc_ppm_per_c = 3.583572e+02`, which matches `NgspiceBackend`'s
  `_MEASURE_RE`. Confirmed end-to-end through the real backend:
  `{'tc_ppm_per_c': 358.3572, 'iq_ua': 17.17839, ...}`.

So TC is computed inside the control block:

```
meas dc vmax MAX v(vbgout)
meas dc vmin MIN v(vbgout)
meas dc vnom FIND v(vbgout) AT=27
let tc_ppm_per_c = (vmax-vmin)/(vnom*165)*1e6
print tc_ppm_per_c
```

**`pvt.py` needs no changes.** An earlier reading of this problem assumed TC
would require a new cross-corner aggregation capability. It does not, and
deriving TC from the corner grid's three temperature points would in fact be
*wrong*: a bandgap's `Vref(T)` is parabolic with its extremum in the interior.
A probe run confirmed this directly — `vmin` landed at 15°C, not at either
endpoint, so sampling only −40/27/125 would systematically understate TC.

**Parser gotcha to preserve:** `meas MAX`/`MIN` output lines carry an `at=`
suffix (`vmax = 8.69e-02 at= 1.25e+02`) and therefore do **not** match
`_MEASURE_RE`. Any criterion needing a MAX/MIN value must route through
`let` + `print`. This is why `vmax`/`vmin` above are intermediates, not
criteria.

Quiescent current rides on the `tc` testbench (`meas dc idd FIND i(Vdd)
AT=27`, then `let iq_ua = -1e6*idd`) at zero extra simulation cost, and is
directly reusable as the objective function for the later area/current
minimisation sub-project.

### Corner grid

Unchanged: 5 process × 3 voltage × 3 temperature = 45, reusing `spec_pvt.yaml`
conventions.

The `tc` testbench runs its own temperature sweep, so the grid's temperature
axis is redundant for it — 45 corners yield only 15 distinct results, wasting
30 simulations (~10 s). Per-testbench corner axes are **not** added; the cost
does not justify the complexity. Every other testbench genuinely needs the
temperature axis.

### The benchmark's actual output: a culprit map

Thresholds are set **loosely** from measured values, and failure is seeded
into exactly **one** block at a time. Whether the tuner edits that block is
the result being measured.

| Criterion | Culprit block | Correct knob |
|---|---|---|
| `tc_ppm_per_c` | `BGR_CORE` | `Rp/R1` ratio |
| `vbgout_v` | `BGR_CORE` | `Rp/R1`, PNP `mult` |
| `vbg0_v` / `vbg1_v` | ladder | tap ratio |
| `startup_time` | `BGR_CORE` | `Xstart` W/L |
| `erramp_gain` / `erramp_pm` | `ERRAMP` | moscap, sizing |
| `trimamp_gain` / `trimamp_pm` | `TRIMAMP` | moscap, sizing |
| `vbg0_settling` | `BUF_P` | moscap, bias current |
| `vbg1_settling` | `BUF_N` | moscap, bias current |
| `iq_ua` | global | — |

This map is only meaningful because Part 1 makes each block's components
individually addressable. Without scoped refdes the tuner's proposal cannot
distinguish `BUF_P.Xcc` from `BUF_N.Xcc`, and the benchmark would report
noise from day one.

## Out of scope

- Trim code modelling (fixed nominal tap only).
- Corner reduction during tuning iterations — that is the separate PVT V2
  sub-project.
- Area/current multi-objective optimisation — separate sub-project; this
  design only *exposes* `iq_ua` for it.
- Graph-based netlist analysis, building-block recognition, signal-path
  tracing — the larger netlist-representation sub-project. Part 1 takes only
  the scoped-addressing slice, which is a hard prerequisite here.
- Instance-scoped addressing (`Xb1.Xcc`). Definition scope only.

## Testing

Following the project's TDD convention, every module gets paired unit tests
in `tests/unit/`.

Part 1:
- `apply_changes` with a scoped refdes edits only the targeted subckt.
- An unscoped refdes matching multiple scopes raises rather than silently
  editing the first.
- An unscoped refdes matching exactly one component still works
  (back-compatibility for existing benchmarks).
- `index_baseline_components` keys collide-free across subckts sharing a
  refdes.
- The area gate compares against the correct scoped baseline.

Part 2:
- Real-ngspice tests (following `test_psr_benchmark_ngspice.py`) asserting
  each testbench simulates and produces its measurements.
- A test asserting the `tc` control block's `let`/`print` derived value is
  parsed as a measurement — this is the mechanism the whole TC criterion
  rests on.
- A test asserting `pnp_05v5` is classified and tiered by the area gate
  rather than falling through to unconstrained.
- Threshold values in `spec.yaml` are derived from a recorded measurement
  run and documented here, not guessed — following the sky130 migration's
  precedent.

## Risks

- **The circuit is designed, not ported.** The user opted to have the core
  built here rather than porting their real netlist. With loose specs this is
  acceptable, but any structural mistake propagates into every sub-project
  that later uses this benchmark. Mitigation: the culprit map above is
  checkable — if seeded failures do not localise as predicted, the circuit is
  wrong, not the tuner.
- **Four amplifiers is a lot of circuit to size in sky130.** Loose thresholds
  keep this tractable, but the implementation plan should stage it: core +
  error amp reaching a working `vbgout` first, then trim/ladder/buffers.
- **Scoped refdes touches the tuner's contract.** `TUNER_SCHEMA`'s pattern,
  the analyzer's output, and `verify_pre`'s validation all move together; a
  partial change would let a scoped proposal pass schema validation and then
  silently no-op in `apply_changes` — the exact failure mode CLAUDE.md
  already warns about for `param`.

---

# Part 2 — as built (2026-07-26)

Part 1 is merged. Part 2's plan was deliberately deferred until the circuit
could be iterated in ngspice, because device sizing had to be measured rather
than guessed. That iteration is now done, and it **disproved several claims
made above**. Everything in this section supersedes the corresponding claim in
the original Part 2 design; the original text is kept so the corrections are
legible rather than silently rewritten.

## Corrections to the original Part 2 design

### `mult=8` does not set the PNP emitter-area ratio — `m=8` does

The original design said the 1:8 ratio is `mult=8`. It is not. In
`sky130_fd_pr__pnp_05v5_W3p40L3p40`, `mult` appears **only** inside the three
`*_mm` mismatch expressions, which are identically zero without Monte Carlo;
`is` and `bf` do not scale with it. Measured, two diode-connected PNPs at the
same 5 µA:

| ratio mechanism | ΔVbe |
|---|---|
| `mult=8` | −5.4e−15 V (i.e. zero) |
| `m=8` (instance multiplier) | 54.59 mV |

`VT·ln(8)` at 27 °C is 53.78 mV, so `m=8` is doing the right thing and `mult`
is doing nothing. Had this not been probed, the core would have silently had
no ΔVbe and no PTAT current at all.

### The error amplifier needs an NMOS input pair, not PMOS

The original headroom table assumed `ERRAMP`'s input common mode is "~0.65 V
(Vbe)". It is not a constant. Measured on the working core:

| T | Vbe (= ERRAMP input CM) |
|---|---|
| −40 °C | 0.832 V |
| 27 °C | 0.722 V |
| 125 °C | 0.549 V |

The binding case is the **cold** end, where a PMOS pair at 0.832 V has no tail
headroom left below a 1.71 V rail. An NMOS pair is binding at the hot end
instead, and 0.549 V is comfortable there. So `ERRAMP` is NMOS-input. The rest
of the table (TRIMAMP and BUF_N NMOS, BUF_P PMOS) survives.

### `TRIMAMP` must be two-stage; the buffers must not be

A 5T OTA can only deliver DC load current by unbalancing its input pair, and
that imbalance appears directly as input-referred offset. Measured with a 5T
`TRIMAMP` driving the 270 kΩ ladder: **42 mV** of offset, which propagated to
a 4 % error on `vbg1`. Rebuilt as two-stage (5T OTA + PMOS common-source
output), the same measurement is **1.65 mV**.

The buffers drive gate capacitance only, so they have no DC load current and
stay 5T — which is also what keeps their compensation trivial (see below).

### Compensation: which caps are possible is decided by MOS-cap physics

The user's constraint is that every added capacitor is a MOS cap, not MiM.
Measured densities (`.ac` at 1 MHz, D/S/B tied):

| gate-body bias | nfet cap | pfet cap |
|---|---|---|
| 0.1 V | 2.30 fF/µm² | — |
| 0.5 V | 5.72 fF/µm² | 1.83 fF/µm² |
| 0.7 V | 7.03 fF/µm² | 2.05 fF/µm² |
| 0.9 V | 7.48 fF/µm² | 3.41 fF/µm² |
| 1.3 V | 7.75 fF/µm² | 6.87 fF/µm² |
| ≥1.5 V | 7.85 fF/µm² | 7.30 fF/µm² |

Two consequences the original design did not anticipate:

1. **An nfet MOS cap cannot float.** Its body is the p-substrate, so one plate
   is pinned at `vss`. Rail-referenced compensation is therefore all an nfet
   cap can do. A **pfet** cap sits in its own nwell and *can* float between
   two signal nodes — which is what a Miller cap requires.
2. **Loading the first stage is not compensation for a two-stage amp.**
   Measured on the trim loop: no cap → −16.4°, 5 pF at `outA` → −0.6°, 50 pF
   at `outA` → 25.7°. The second stage's gain sits *after* that pole, so only
   pole splitting moves the crossover. A Miller cap of 3–5 pF does what 50 pF
   of first-stage loading could not.
3. **The Miller RHP zero caps phase margin near 45°** regardless of Cc (ideal
   5 pF Miller → 45.8°). A nulling resistor fixes it, and the optimum is broad:

   | Rz | trim loop PM |
   |---|---|
   | 0 | 40.6° |
   | 2.0 kΩ | 52.9° |
   | 5.1 kΩ | 70.4° |
   | 9.9 kΩ | 94.2° |
   | 19.4 kΩ | 119.1° |
   | 38.5 kΩ | 70.2° |
   | 63.9 kΩ | 27.6° |

   This is the same prescription as `topologies.py`'s existing
   `miller_nulling_resistor` entry, arrived at independently.

So: `ERRAMP` and both buffers use rail-referenced **nfet** MOS caps; `TRIMAMP`
uses a floating **pfet** MOS cap plus a `res_high_po` nulling resistor. Every
cap in the circuit is still a MOS cap.

### The supply range is ±5 %, not ±10 %

This is the one place where measurement forced a **spec** change rather than a
design change. The input pair's saturation margin is

```
Vds(X1) = Vdd − Vcm − |Vgs_p(load)| + Vgs_n(pair)
```

and `TRIMAMP`/`BUF_N` have `Vcm` ≈ 1.20–1.24 V because that is what the chain
is required to produce. Measured `|Vgs_p|` at 5 µA is 0.94–1.13 V depending on
geometry and corner, and `Vgs_n` is 0.55–0.70 V. At `Vdd = 1.62 V` that leaves
`Vds(X1)` at or below zero, and it was directly observed: at `sf`/1.62 V/27 °C
the trim input pair sat at `Vds = 27.8 mV`, the trim loop gain collapsed to
**−45.4 dB**, and `vbg1` fell to 1.084 V (−9.4 %).

Widening the PMOS mirror loads recovers the loop gain (−45.4 dB → +23.7 dB)
but not the DC accuracy, because the constraint is `Vdd − Vcm`, which no
sizing changes. A supply sweep locates the knee:

| Vdd | `vbg1` at `sf` |
|---|---|
| 1.62 V | 1.084 V |
| 1.68 V | 1.140 V |
| 1.71 V | 1.162 V |
| 1.75 V | 1.183 V |
| 1.80 V | 1.193 V |

Amplifying a 1.2 V common mode from a 1.62 V rail needs a folded-cascode or
rail-to-rail input stage. Building one is **out of scope**: this benchmark
exists to check that tuning targets the right block, and doubling the
transistor count of three amplifiers does not serve that. The corner grid
therefore uses **1.71 / 1.80 / 1.89 V**, and the reason is recorded here so
nobody "fixes" the grid back to ±10 % without also fixing the input stages.

## Measured baseline, all 45 corners (5 process × 3 voltage × 3 temperature)

Every criterion produces a measurement at every corner — no silent gaps.

| measurement | min (corner) | max (corner) |
|---|---|---|
| `vbgout_v` | 1.2352 (ff/1.71/27) | 1.2422 (ss/1.89/27) |
| `vbg0_v` | 0.49121 (sf/1.71/27) | 0.50251 (ss/1.89/27) |
| `vbg1_v` | 1.1624 (sf/1.71/27) | 1.1997 (ss/1.89/27) |
| `tc_ppm_per_c` | 33.76 (tt/1.89) | 41.96 (ss/1.89) |
| `iq_ua` | 78.97 (sf/1.71) | 88.81 (fs/1.89) |
| `psrr_bg_db` | −75.05 (fs/1.89/−40) | −43.99 (ss/1.71/125) |
| `startup_time` | 67.8 ns (fs/1.89/125) | 91.1 ns (sf/1.71/−40) |
| `vbg0_droop_mv` | 56.28 (sf/1.89/125) | 81.25 (ss/1.71/−40) |
| `vbg1_droop_mv` | 56.27 (ff/1.89/−40) | 63.69 (ss/1.71/−40) |
| `core_gain_db` | 35.78 (ss/1.71/125) | 46.89 (ff/1.89/−40) |
| `core_pm_deg` | 69.42 (ss/1.71/−40) | 84.48 (ss/1.89/125) |
| `trim_gain_db` | 23.66 (sf/1.71/−40) | 72.29 (fs/1.89/−40) |
| `trim_pm_deg` | 64.32 (sf/1.89/27) | 88.70 (sf/1.71/−40) |
| `buf1_gain_db` | 42.69 (sf/1.71/125) | 44.77 (fs/1.89/−40) |
| `buf1_pm_deg` | 81.63 (fs/1.71/−40) | 85.80 (sf/1.71/−40) |
| `buf0_gain_db` | 40.37 (fs/1.89/125) | 43.54 (fs/1.8/−40) |
| `buf0_pm_deg` | 76.40 (fs/1.89/−40) | 83.64 (ss/1.71/−40) |

Nominal (tt/1.80 V/27 °C): `vbgout` 1.2390 V, `vbg0` 0.5016 V, `vbg1` 1.1965 V,
TC 33.9 ppm/°C, `Iq` 75.1 µA.

Thresholds in `spec.yaml` are set loosely around these ranges, per the user's
"specs do not need to be tight" instruction.

## Five testbenches, not ten

The original design listed ten. Each testbench costs one LLM `simulate` call
per iteration and 45 simulations per full sweep, so they were consolidated:

| testbench | analysis | criteria |
|---|---|---|
| `dc_tc` | `dc temp -40 125 1` | `tc_ppm_per_c`, `vbgout_v`, `vbg0_v`, `vbg1_v`, `iq_ua` |
| `startup` | `tran`, 100 ns Vdd ramp | `startup_time` |
| `psrr` | `ac`, AC on Vdd | `psrr_bg_db` |
| `settling` | `tran`, charge kick | `vbg0_droop_mv`, `vbg1_droop_mv`, `vbg{0,1}_resid_mv` |
| `amp_loops` | four `ac` runs | gain and PM for all four loops |

Two consolidations carry the load:

- **`dc_tc` covers every DC output.** One temperature sweep yields TC *and*
  every `AT=27` DC value, so separate `dc_out` and `linereg` testbenches are
  redundant — and the corner grid's voltage axis already *is* the line
  regulation test.
- **`amp_loops` measures all four loops in one netlist.** All four feedback
  loops are broken with an `Lfb`/`Cin` harness, and the control block runs
  four `ac` analyses, using `alter @Vsrc[acmag]` to enable one injection
  source at a time. Injecting into all four simultaneously would cross-couple
  through `vbgout` and the ladder; injecting one at a time does not.

`psrr` is measured on `vbgout` only. On the buffered outputs it collapses to
−0.5 dB at `sf`/1.71 V/−40 °C for the same input-headroom reason as above, so a
threshold there would have to be so loose as to assert nothing.

## Loop breaking is done through ports, not per-file edits

`BANDGAP`'s port list carries both halves of each broken loop
(`ampout`/`mpgate`, `vfb`/`trm_i`, `vbg1`/`b1_i`, `vbg0`/`b0_i`). A testbench
that wants the loop closed passes the same node twice; `amp_loops` passes
distinct nodes and inserts the harness. This keeps every subckt definition
**byte-identical across all five netlist files**, which is what makes
`orchestrator._apply_to_all` correct — a tuning change to `TRIMAMP.XRz` must
land in all five files or the testbenches silently diverge.

## Measurement definitions that survive corners

Two `meas` idioms were found to fail *silently* — producing no measurement
rather than a bad one:

- **A threshold-crossing settling time does not survive corners.** `meas tran
  ... WHEN v(vbg1)=1.1845 CROSS=LAST` produced no measurement at 14 of 45
  corners, because the DC level moves and the absolute crossing level is never
  reached. Replaced by droop (`v_pre − v_min` during the kick) and residual
  (`|v_post − v_pre|` 500 ns after it), both of which are always computable.
- **`meas MAX`/`MIN` output carries an `at=` suffix** and so never matches
  `NgspiceBackend._MEASURE_RE`. It is usable only as an intermediate feeding
  `let` + `print`, which is how `tc_ppm_per_c` and the droop metrics are
  emitted. Verified end-to-end through the real `NgspiceBackend`: all five
  testbenches return `status="success"` with every expected measurement.

## Infrastructure gaps this benchmark exposes

Three are real defects that Part 2 must fix, not just benchmark content:

1. **`area_limits` cannot size an X-prefixed resistor.** `_classify_ctype`
   correctly returns `"R"` for `sky130_fd_pr__res_high_po` (the name contains
   `res`), but `_tier_baseline_value` then falls through to
   `parse_spice_value(component.value)` — and `value` is the subckt *name*.
   The `ValueError` is swallowed by `check_area_growth`'s existing guard, so
   the resistor is silently **unconstrained**. It needs the same treatment the
   MiM cap already has: read the geometry param (`l`) instead of `value`.
2. **`pnp_05v5` is unconstrained.** Its model name contains none of the
   `fet`/`cap`/`res` markers, so `_classify_ctype` returns `"X"` and
   `allowed_multiplier_for` returns `None`. The emitter-ratio knob is `m`, a
   count rather than a length, so it needs its own marker and a single
   unbounded tier.
3. **sky130 device models are binned, and exceeding a bin is a hard error.**
   `wmax`/`lmax` are 100 µm. `W=120` produced `could not find a valid
   modelname` and aborted the simulation — not a warning, not a bad number. A
   tuner allowed to widen a `W=40` device by 3× reaches 120 and kills the run.
   The transistor tiers happen to bound this in practice, but the failure mode
   should be documented so it is recognised if it appears.

## Smaller facts worth not re-deriving

- **The Kuijk loop is genuinely bistable in DC.** Without a `.nodeset` on the
  PNP emitters, ngspice converges to the degenerate sub-nA state
  (`vbgout ≈ 0.47 V`) even with an ideal amplifier. The real circuit resolves
  this with the startup device; the ideal-amp probe used
  `.nodeset v(na)=0.75 v(nb)=0.75 v(ne8)=0.70`.
- **`res_high_po` has a fixed head resistance.** For `w=1`,
  `R ≈ 317.4·(l+0.247)/0.999 + 299.5 Ω` — the ~300 Ω head term is not
  negligible for the 10.9 kΩ `R1`. Verified: `w=1, l=10` measures 3550 Ω
  against 3554 Ω predicted.
- **The zero-TC ratio moves with the amplifier.** With an ideal op-amp the
  optimum is `Rp/R1 = 9.3` (26.1 ppm/°C); with the real `ERRAMP` it is
  **9.5** (≈40 ppm/°C), because the OTA's offset is itself temperature
  dependent. Sizing the core against an ideal amp and stopping there would
  have left the benchmark 20 ppm/°C off its own optimum.
- **The first line of a SPICE deck is the title.** A `.temp` directive placed
  there is silently consumed and the simulation runs at 27 °C. This cost one
  debugging cycle and produces corner data that looks plausible and is wrong.
- **Amplifier polarity flips with stage count.** In a 5T OTA the diode-side
  input is non-inverting; adding an inverting second stage moves the
  non-inverting input to the other side. Getting this backwards does not
  produce an obvious failure — the core latched at `vbgout = 1.759 V` with a
  perfectly respectable-looking TC of 36 ppm/°C.
