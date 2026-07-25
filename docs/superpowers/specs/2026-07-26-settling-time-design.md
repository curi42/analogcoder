# Settling Time Verification — Design

## Problem

analogcoder verifies AC-domain criteria only (loop gain, UGBW, phase margin,
and — since the PSR feature — supply rejection). None of these directly
characterize how fast the amplifier's output actually settles after a step
input in real closed-loop use, which is a standard op-amp datasheet spec and
was the other verification gap flagged alongside PSR (see project memory
`project-psr-settling-scope`).

This adds **settling time** as a fourth testbench on `two_stage_opamp`: a
genuine closed-loop `.tran` step-response test, verified together with the
other three testbenches on every orchestrator iteration via the multi-
testbench infrastructure the PSR feature already built. No orchestrator,
simulator, judge, or `RunState` code changes are needed — `TargetSpec`'s
`analyses` field is purely descriptive metadata (confirmed by inspection: it
is never read outside `spec.py` itself), so a `.tran`-based control block
runs through the exact same `NgspiceBackend.run()` path as the existing
`.ac` ones. This feature is scoped to *adding a testbench*, not extending
the pipeline.

## Scope (v1)

- **Single nominal PVT corner**, matching every other testbench in this
  project. No corner sweeping (see project memory `project-pvt-corner-future`
  for that separately-scoped future work).
- **Closed-loop unity-gain buffer** (`vout` wired directly to `vinn`,
  feedback factor = 1): the fastest, most standard settling-time test
  configuration, and — because feedback factor 1 means the loop gain equals
  the amp's own open-loop gain — it stresses the *same* unity-loop-gain
  condition the existing `phase_margin` criterion already targets, so a
  poor phase margin (ringing) and a good one (fast, clean settling) show up
  directly in this testbench's measurement.
- **1V step** (`PULSE(0 1 1u 1n 1n 10u 20u)` at `vinp`), landing well inside
  the `±2.5V` supply rails.
- **±0.5% settling band** around the true final DC value. "Settled" means
  the *last* time the output leaves this band — not the first time it
  enters it, since ringing can re-exit a band it briefly touched.
- **Generous, regression-guarding threshold** (not a tuning target): real
  ngspice measurement of the unmodified `netlist.cir` circuit already
  settles in ~35.6ns; the threshold is set at 200ns of budget (1.2μs
  absolute, since the step lands at 1μs) — comfortably passing at baseline,
  the same role `psr_plus` plays in the PSR feature (guards against a
  *future* tuning change regressing settling behavior, without itself being
  something the tuner needs to chase).
- **Two separate criteria**, `settling_time_hi` and `settling_time_lo` (one
  per band edge), each a plain `measurement <= threshold` check against an
  absolute crossing time — not a single combined "settling time" value.
  ngspice's `.meas ... param='expr'` arithmetic (which would have let a
  single measurement compute `max(t_hi_last, t_lo_last) - step_time`) does
  not parse in this project's ngspice build (verified: both `param='t -
  1u'` and `param='max(a,b)'` fail with "no such function as..." errors),
  so the offset is folded into each criterion's threshold instead
  (`step_time + budget`) and the two edges are checked independently. This
  mirrors the PSR feature's existing `psr_plus`/`psr_minus` split into
  directional criteria — not a new pattern for this codebase.

## Testbench

`netlist_settling.cir` — a full copy of `netlist.cir` (same invariant as
the PSR testbenches: the `OPAMP2STAGE` subckt body must stay byte-identical
across every `two_stage_opamp` testbench file, since tuning changes are
applied to each file independently and rely on this), with the top-level
wiring changed from the AC loop-gain-breaking topology to a closed unity-
gain loop:

```diff
--- netlist.cir
+++ netlist_settling.cir
@@
- Vinp vinp 0 DC 0
- Lfb vout vinn 1e6
- Cin vstim vinn 1
- Vstim vstim 0 DC 0 AC 1
- Xdut vinp vinn vout vdd vss OPAMP2STAGE
+ Vinp vinp 0 PULSE(0 1 1u 1n 1n 10u 20u)
+ Xdut vinp vout vout vdd vss OPAMP2STAGE
```

Instantiating `Xdut` with `vout` passed for *both* the `vinn` and `vout`
subckt pins directly ties the inverting input to the output — a real wire,
not a large-inductor AC-only approximation like the main testbench's `Lfb`.

Control block:

```
.control
tran 1n 6u
meas tran t_hi_last WHEN v(vout)=1.00539 CROSS=LAST
meas tran t_lo_last WHEN v(vout)=0.99539 CROSS=LAST
.endc
```

(`1.00539` / `0.99539` are the ±0.5% band edges around the measured true
final value ~1.0004V, hard-coded rather than computed at simulate time —
consistent with how the AC testbenches already hard-code their `.meas`
targets in the control block text.)

`spec.yaml` gains a fourth `testbenches` entry:

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

## Known limitation (deliberately accepted, not fixed)

If a future tuning proposal makes the step response perfectly monotonic
(damps out all overshoot, never crossing the upper 1.00539V band edge),
`t_hi_last`'s `.meas` finds no crossing and ngspice reports it as failed —
the measurement key is simply absent from `NgspiceBackend`'s parsed results
(confirmed in `simulators/ngspice.py`'s `_MEASURE_RE` line-scan: a failed
`.meas` just never matches, it doesn't emit an error entry). Downstream,
`judge_tools.evaluate_criteria` treats a missing measurement as an
automatic fail (`actual: NaN`, `pass: False`) — so a *faster, cleaner*
settling response could paradoxically fail `settling_time_hi`, the opposite
of the intended meaning.

This is accepted as a known limitation rather than engineered around, the
same way `netlist.py`'s missing subckt-scope tracking is documented and
deliberately deferred in CLAUDE.md: fixing it properly would need judge
logic beyond a generic `measurement <= threshold` comparison (e.g. treating
"never crossed" as automatically passing), which is a larger change than
this feature's scope, and the real Cc-sweep validation below never actually
triggered it — every tested value crossed both edges.

## Validation

Real ngspice-46 measurements against the actual `netlist.cir` circuit
(unity-gain-buffer wiring, 1V step at t=1μs):

| Cc | `t_hi_last` | `t_lo_last` | Settling budget used |
|---|---|---|---|
| 2p (baseline) | 1.02823μs | 1.03563μs | ~35.6ns |
| 3.3p | 1.03343μs | 1.02143μs | ~33.4ns |
| 4p | 1.03786μs | 1.02494μs | ~37.9ns |
| 6p | 1.04677μs | 1.03798μs | ~46.8ns |

All four comfortably clear the 1.2μs (200ns budget) threshold — including
every `Cc` value already explored by the existing phase-margin tuning
sweep, confirming the threshold doesn't accidentally block legitimate PM
tuning. Interestingly, increasing `Cc` (which improves phase margin) does
**not** meaningfully improve settling time here — the dominant-pole
slowdown from a larger `Cc` roughly cancels the ringing reduction from
better phase margin, so this testbench's threshold is a genuine
regression guard, not something the existing PM-tuning knob happens to
also satisfy for free.

The raw waveform (baseline, `Cc=2p`) confirms the measurement technique
works as intended: the step at 1μs produces an initial ~32mV undershoot
(an artifact of the two-stage amp's dynamic response, not the step edge
itself), rises through a ~40% overshoot peak (~1.40V) around 21ns after the
step, rings back down, and both `CROSS=LAST` measurements land within ~1ns
of each other (~28-36ns after the step) — consistent with a single damped
ring-down cycle, not a slower secondary tail.

## Testing

No unit tests beyond the existing multi-testbench infrastructure's own
tests (from the PSR feature) — this is a data-only addition. The proof is
the same pattern used for the PSR testbenches:

1. A real-ngspice test (non-skip-gated, in `tests/unit/`, mirroring
   `test_psr_benchmark_ngspice.py`) that: confirms `spec.yaml` now declares
   4 testbenches with `settling_time`'s expected criteria/thresholds; runs
   the real `netlist_settling.cir` through `NgspiceBackend` and asserts the
   measured `t_hi_last`/`t_lo_last` land within a tolerance band of the
   validated baseline values above (catches drift the same way the PSR
   test does); confirms the `OPAMP2STAGE` subckt body is byte-identical
   across all four `two_stage_opamp` testbench files now, not just the
   three PSR-era ones.
2. Manual end-to-end validation (real Claude backend) of the full 4-
   testbench benchmark, run together with the PSR feature's own deferred
   end-to-end validation — per the user's explicit request to validate
   both together in one real orchestration run rather than two separate
   ones.
