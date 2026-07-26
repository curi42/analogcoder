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
