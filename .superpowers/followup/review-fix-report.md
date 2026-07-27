# Pre-merge review fix wave — `e2-followup-real-deck`

One wave, all six findings (C1, I1, I2, I3, M1–M4). `d885993` untouched.

Full suite: **485 passed, 2 skipped** (was 470/2 — 15 new tests).
Benchmarks: **zero diffs** across all ten decks (harness below).

---

## C1 (CRITICAL) — one parameter reaching two sibling nested instances squared its ratio

### What was wrong
`_traced_targets`' group key was `(refdes, device.scope, device.refdes)`. Every path
through the same subckt *definition* yields the same `Component` object, so `xl1→ma1`
and `xl2→ma1` collapsed into one group and `group.ratio *= ratio` ran twice.

### The design question, and what I chose
**A parameter reaching N physical devices is N groups, each bounded by the same
per-device ratio.** The tier table is a per-device *growth ratio* limit, not a
total-area budget: `SizeTier.max_value` is keyed on one device's size, and
`allowed_multiplier` answers "how much may *this* device grow". Growing one knob that
sizes two devices does grow total area more than one device's worth — but each device
still only grows 2.5×, and 2.5× per device is what the table bounds. Squaring was not a
conservative approximation of a total-area budget; it was a fabricated number
(`6.25x`) fed back to the tuner as rejection feedback, driving its next proposal off a
figure that describes nothing.

If a total-area budget is ever wanted, it is a *different* gate with a different unit
(µm², not a ratio) and it should be added as one, not smuggled in as a squared ratio.

### Change
- `netlist.TracedTarget` gains `chain: tuple[str, ...]` — the intermediate X instance
  refdeses walked to reach the device.
- `params._trace` carries `chain`, appending `device.refdes` on each recursion
  (the refdes was already in hand at the recursion site).
- `area_limits._traced_targets` keys on `(refdes, chain, device.scope, device.refdes)`
  and renders the label as `xtop -> xl1 -> LEAF.ma1`, so feedback names the physical
  device rather than the definition.

### Tests
`tests/unit/test_params.py::test_trace_distinguishes_two_sibling_instances_of_one_definition`
`tests/unit/test_params.py::test_trace_records_an_empty_chain_for_a_direct_body_device`
`tests/unit/test_area_limits.py::test_one_param_reaching_two_sibling_instances_does_not_square_its_ratio`
`tests/unit/test_area_limits.py::test_two_sibling_instances_are_each_bounded_on_their_own`

RED (reviewer's deck, `wtop` 2e-6 → 5e-6, a 2.5× inside the 3.0× tier):
```
E  AssertionError: xtop -> LEAF.ma1: proposed change grows area by 6.25x, exceeding the 3.0x limit for its size tier
E  assert False is True
E  AssertionError: assert '4.00x' in 'xtop -> LEAF.ma1: proposed change grows area by 16.00x, exceeding the 3.0x limit for its size tier'
FAILED test_one_param_reaching_two_sibling_instances_does_not_square_its_ratio
FAILED test_two_sibling_instances_are_each_bounded_on_their_own
```
GREEN: both pass; 2.5× is approved, and 4× is rejected **twice** (once per device,
`xl1` and `xl2` both named in the feedback, `6.25x` absent).

The second test is deliberate: the fix must not degrade to "a sibling exists → pass".

---

## I1 (IMPORTANT) — the gate is inert when the wrapper definition arrives via `.include`

### What was wrong
`annotate_traced_params` needs `_resolve_subckt_reference` to find the definition in the
parsed text; `parse_netlist` never follows includes. So an include-only cell library
traced to `{}` and `wn` 2 µm → 2 mm (**1000×**) returned `(True, None)`.

### What I did — and what I refused to do
Per the brief, `parse_netlist` still does **not** follow includes. Instead the blindness
became a recorded fact. The gate previously could not distinguish four situations that
all produced the same `(True, None)`:

| state | meaning |
|---|---|
| `bounded` | a tier applied — the gate actually judged |
| `neutral` | **nothing** to bound — `nf` is area-neutral by construction |
| `blind` | **cannot see** — component instantiates a subckt this deck does not define |
| `unjudged` | **cannot judge** — a value would not resolve, or the token is not a size |

### Change
- `netlist.Component.undefined_subckt: bool`, set by `annotate_traced_params` when an X
  component's subckt reference does not resolve. (The early-out was reordered so a
  param-less X instance is still flagged.)
- `area_limits._visibility()` classifies each change; `AreaCheckResult(approved,
  feedback, states)` and `evaluate_area_growth()` return it. `check_area_growth` is now a
  thin wrapper preserving the old `(bool, str|None)` signature — no caller or test broke.
- **No behavioural change**: a blind change is still not blocked. Blocking on
  unreadable geometry would reject every legitimate proposal against an include-only
  deck. The fix is that the log now says so.
- A sky130 primitive is **not** blind: it is unresolvable too, but it is classified by
  its model name and tiered on geometry, so it reports `bounded`. Pinned by a test.

Documented in `CLAUDE.md` beside the existing "`parse_netlist` never follows includes"
note.

### Tests
`test_an_include_only_wrapper_is_reported_as_blind`
`test_a_pdk_primitive_is_not_blind_even_though_its_model_is_in_an_include`
`test_the_three_non_bounded_states_are_told_apart`

RED: `ImportError: cannot import name 'evaluate_area_growth'` — the API did not exist.
Behavioural RED from the reproduction deck:
```
=== I1 ===
traced: {}
(True, None)      # wn 2e-6 -> 2e-3, i.e. 1000x
(True, None)      # ma1 4 -> 400
```
GREEN: same two verdicts (unchanged by design) with
`states == {"xwrap1.wn": "blind"}`.

---

## I2 (IMPORTANT) — a wrapped resistor's size knob was unbounded, the bare one blocked

I agree with the reviewer and not with the original implementer: a positional value **is**
the size knob for R and C — that is precisely what `RESISTOR_TIERS`/`CAPACITOR_TIERS` are
keyed on.

### Change
`params._positional_target` extends the trace to a component's positional `value`, and
`_traced_targets` handles `token == "value"` with the ordinary device-value tiers.

Two deliberate restrictions, both to stay inside "trace, don't guess":
- **Bare identifier only.** `R1 a b rv` traces; `R1 a b {rv*2}` does not. Accepting an
  expression would assume the instance parameter's ratio equals the device value's
  ratio, which is only true for the linear case — assuming it is the guess this layer
  exists to forbid. An expression takes the existing "cannot judge" path.
- **Non-X devices only.** An X line's positional token is the subckt name, not a value.
- A parameterised *model name* (`m1 d g s b mod`) is harmless: it resolves to `None`,
  yielding "cannot judge, do not block" — the established fallback.
- The traced value is multiplied by the device's `m`, consistent with M3.

### Tests
`test_trace_follows_a_positional_value_that_is_a_bare_identifier`
`test_a_positional_value_that_is_a_literal_is_not_traced`
`test_a_wrapped_resistor_is_bounded_like_the_bare_one`
`test_a_wrapped_resistor_growing_within_its_tier_is_allowed`

RED:
```
E  KeyError: 'rv'                     (trace layer)
E  assert True is False               (gate: wrapped 1000x approved, bare rejected)
FAILED test_a_wrapped_resistor_is_bounded_like_the_bare_one
```
GREEN — both now reject, with the wrapped one naming the reached device:
```
xr1 -> RCELL.R1: proposed change grows area by 1000.00x, exceeding the 2.0x limit for its size tier
R2: proposed change grows area by 1000.00x, exceeding the 2.0x limit for its size tier
```

---

## I3 (IMPORTANT) — `_instance_env` resolved a contested name `build_param_envs` refuses to

I do **not** think an instance-scoped read is entitled to break the rule, so I applied the
shadowing rather than arguing for the exception. Reasoning, recorded at the code and in
`CLAUDE.md`:

`_instance_env` legitimately diverges from `build_param_envs` on exactly one axis —
"instances disagree" is normal in this deck style, and narrowing to one instance dissolves
that ambiguity. It does **not** dissolve the *dialect* ambiguity of a name declared both in
the subckt body and on the `.subckt` line; which one wins is a property of the simulator,
not of the instance. Two resolvers disagreeing about one deck meant the gate acted on the
one that guessed.

### Change
`params._instance_env` now computes `shadowed = set(body) & set(subckt.defaults)`, drops
those names, and masks them from the global environment — with an explicit instance
override still winning (`shadowed.discard(name)`), mirroring `build_param_envs`'
`shadowed -= set(agreed)`.

### Tests
`test_instance_env_drops_a_name_the_body_and_the_subckt_line_contest`
`test_a_contested_name_is_not_resolved_for_tiering_either`

RED:
```
E  AssertionError: assert 1e-05 is None    # total_width read from the .subckt-line 10um
```
and at the gate: `ln` 1u → 2.8u returned `(True, None)` on a 3.0× tier chosen from the
guessed 10 µm. GREEN: `total_width is None`, state `unjudged`. Both resolvers now agree
the name is unknown.

---

## M1 — `area_check` distinguishes what the gate could see

`orchestrator.py` calls `evaluate_area_growth` and logs `states` alongside
`approved`/`feedback`. An `nf`-only pass, an untraceable pass, and an include-blind pass
are now three distinct lines in `history.jsonl`.

Test: `test_area_check_event_records_what_the_gate_could_see` (reads the real
`history.jsonl` through a full `run_orchestration`).
RED: `KeyError: 'states'`. GREEN: `{"xwrap1.wn": "blind"}` on every retry.

The state is computed *before* the integrality check so it records visibility, not
verdict.

---

## M3 — `m` multiplies the tier baseline for every class except `Q`

`_tier_baseline_value` multiplied by `_multiplicity` for `ctype == "M"` only. A sky130 MiM
cap with `m=4` was tiered on the single-unit `w`. Now every class multiplies; `Q` remains
the exception because its `m` *is* the tier key (emitter-area ratio) and multiplying would
double-count. The generic branch also guards `resolved_value is None` before multiplying.

Test: `test_a_mim_cap_is_tiered_on_its_multiplicity_too`.
RED: `assert 9.999999999999999e-06 == 4e-05` — and `w` 10→25 (2.5×) was **approved** under
the 3.0× tier. GREEN: baseline 40 µm, 2.0× tier, rejected.

---

## M4 — one wrapper fixture

`tests/unit/wrapper_decks.py` holds `WRAPPER_DECK` (previously duplicated verbatim as
`WRAPPER_DECK` and `WRAPPER_NETLIST`) plus the four new synthetic decks. `test_params.py`
and `test_area_limits.py` both import it, so the trace layer and the gate layer are always
looking at the same deck. All fixtures are shape-only synthetics — **no production netlist
entered the repo**.

## M2 — the deliberate `ValueError`

`patterns.PatternMatch.__post_init__` gained a comment: the raise is intentionally fatal —
derivation has started lying, so continuing to tune is meaningless — and
`run_orchestration`'s `except ValueError` turns it into a clean `FAIL` rather than a crash.
Do not swallow it.

---

## Benchmarks unaffected

Harness dumps, for all ten `benchmarks/*/*.cir`, every component's `ctype`,
`_classify_ctype`, `_tier_baseline_value`, resulting `allowed_multiplier_for`, full
`traced_params` (scope/refdes/token/total_width), plus a **verdict probe**: for every
numeric parameter the gate can see, `check_area_growth` at 1.6×/2.5×/4.0× growth, with the
exact feedback string. 749 keyed entries.

```
$ .venv/bin/python bench_dump.py > bench_before.json      # at 63ef8a7
$ .venv/bin/python bench_dump.py > bench_after.json       # after the fix wave
$ diff bench_before.json bench_after.json && echo "ZERO DIFFS"
ZERO DIFFS across all 10 benchmark decks
```

Explicitly re-confirmed, per the brief:
```
BGR_CORE.Xq8   tier baseline 8.0  (a count, unscaled, flat 2.0x)
  m 8 -> 16   (True, None)
  m 8 -> 17   (False, 'BGR_CORE.Xq8: ... grows area by 2.12x, exceeding the 2.0x limit ...')
BUF_P.Xcl      tier baseline 2.0e-05
  W 20 -> 60  (True, None)         <- 3.0x, the tier spec_seed_buf0_droop.yaml needs
  W 20 -> 61  (False, '... 3.05x, exceeding the 3.0x limit ...')
```

## Full suite

```
$ .venv/bin/python -m pytest -q
485 passed, 2 skipped in 32.57s
```
(was 470 passed, 2 skipped)

## Disagreements

None with the findings themselves. Two calls made inside them, flagged for the record:

1. **C1's grouping** — I chose "N reached devices = N groups, same per-device ratio"
   rather than any total-area accumulation, for the reason argued above. This is the
   finding's own suggested direction; I am noting it explicitly because the brief asked
   for the choice to be stated.
2. **I2's scope** — I restricted positional-value tracing to a *bare identifier*, which
   is narrower than "any expression reaching the value". The wider version would have
   assumed a linear relationship between the instance parameter and the device value.
   Consequence: `R1 a b {rv*2}` inside a wrapper remains unbounded, reported `unjudged`
   rather than silently passing as `bounded`.
