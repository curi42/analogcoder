# MOS-cap self-Miller false positive — fix report

## Defect

`patterns.find_patterns`, run against a real production netlist for the first
time, emitted three `miller_compensation` matches pairing a component with
itself: `m3, m3`, `m0, m0`, `md0, md0`. `patterns.py`'s stated bar is zero
false positives, and a component being its own Miller compensation network is
not a fact.

## Root cause

The offending devices are MOSFETs used as MOS capacitors
(`m3 nzero vssi nzero vssi UNITDEV_N_DEP_CAP …`, `md0 vssi vcci vssi vssi
UNITDEV_N_DEP_CAP …`): refdes prefix `m` (MOSFET), model name containing `cap`.

`structure._classify_model` substring-matched the model name against
`_MODEL_CLASS_MARKERS` unconditionally, so `UNITDEV_N_DEP_CAP` → `device_class =
"cap"`. Because the refdes prefix is `m`, `_terminals_for` still gave it the
full MOS terminal layout via `_TERMINALS_BY_CTYPE`. The device therefore
landed in both `patterns._find_in_block`'s cap list (`device_class == "cap"`)
and its MOS list (MOS terminal shape), and the Miller matcher's per-cap
gate/drain search found the device itself as its own "gain stage".

`area_limits._classify_ctype` already had the correct precedent: it only
consults model-name markers when `ctype == "X"` (the one case where the
positional value is a PDK primitive name, not a plain model reference).
`structure._classify_model` did not follow that rule.

## Fix 1 — ctype wins over the model name (`src/analogcoder/structure.py`)

`_classify_model` now returns `None` immediately unless `component.ctype ==
"X"`, mirroring `area_limits._classify_ctype` exactly. A refdes prefix that
already fixes the device class (`M`/`Q`/`R`/`C`/`L`/`D`) is never overridden
by a model-name substring; only an `X`-prefixed instance (ambiguous by
construction) still consults `_MODEL_CLASS_MARKERS`.

## Fix 2 — a pattern can never pair a component with itself (`src/analogcoder/patterns.py`)

`PatternMatch` gained a `__post_init__` that raises `ValueError` if
`members` repeats a refdes. This makes the invariant structural: it holds
regardless of which matcher (existing or future) tries to construct the
match, not just the Miller branch that happened to be the entry point this
time.

## TDD — RED then GREEN

**RED** (before the fix), `tests/unit/test_structure.py`:

```
$ .venv/bin/python -m pytest -q tests/unit/test_structure.py -k "m_prefixed or x_prefixed_sky130_cap"
...
tests/unit/test_structure.py:117: AssertionError
E       AssertionError: assert 'cap' != 'cap'
tests/unit/test_structure.py:134: AssertionError
E       AssertionError: assert 'res' != 'res'
2 failed, 1 passed, 9 deselected in 0.04s
```

(the third test, `test_an_x_prefixed_sky130_cap_still_classifies_by_its_model_name`,
already passed — it pins the case the markers exist for and must not move.)

**RED** (before the fix), `tests/unit/test_patterns.py`:

```
$ .venv/bin/python -m pytest -q tests/unit/test_patterns.py -k "self or itself"
F.F
AssertionError: assert not True   # miller self-pair for the cap-marker MOS
AssertionError: PatternMatch(kind='miller_compensation', block=None,
  members=('Md0', 'Md0'), detail='Md0 bridges vcci and vss of Md0')
2 failed, 1 passed, 26 deselected in 0.05s
```

**GREEN** (after both fixes):

```
$ .venv/bin/python -m pytest -q tests/unit/test_structure.py tests/unit/test_structure_golden.py
33 passed in 0.10s

$ .venv/bin/python -m pytest -q tests/unit/test_patterns.py
30 passed in 0.04s
```

## Regression tests added

`tests/unit/test_structure.py`:
- `test_an_m_prefixed_mos_cap_is_not_classified_as_a_cap_by_its_model_name` —
  refdes `M`, model name containing `cap`: `device_class != "cap"`, MOS
  terminal roles preserved.
- `test_an_m_prefixed_device_is_not_classified_as_a_resistor_by_its_model_name` —
  same shape for a `res` marker.
- `test_an_x_prefixed_sky130_cap_still_classifies_by_its_model_name` — pins
  that `X`-prefixed sky130 primitives still classify by model name.

`tests/unit/test_patterns.py`:
- `test_a_mos_used_as_a_cap_via_model_name_does_not_miller_pair_with_itself`
- `test_a_mos_used_as_a_resistor_via_model_name_does_not_self_pair`
- `test_pattern_match_construction_rejects_a_duplicate_member_directly` —
  constructs `PatternMatch(members=("M1", "M1"), ...)` directly and expects
  `ValueError`, independent of any matcher.
- `test_no_pattern_match_ever_pairs_a_component_with_itself` — runs
  `find_patterns` over a deck reproducing all three reported shapes (`m3`,
  `m0`-shaped, `md0`-shaped) and asserts no match has a duplicate member.

None of these reference the production netlist; each is a minimal synthetic
deck capturing only the shape (refdes prefix vs. model-name marker) that
caused the defect.

## Before/after `device_class` counts, all ten benchmark decks

Computed by loading the pre-fix `structure.py` (via `git show HEAD:...`) and
the post-fix module side by side and tallying `(ctype, device_class)` over
every component in every block, summed across:

```
benchmarks/bandgap/netlist.cir
benchmarks/bandgap/netlist_startup.cir
benchmarks/bandgap/netlist_psrr.cir
benchmarks/bandgap/netlist_settling.cir
benchmarks/bandgap/netlist_loops.cir
benchmarks/two_stage_opamp/netlist.cir
benchmarks/two_stage_opamp/netlist_psr_plus.cir
benchmarks/two_stage_opamp/netlist_psr_minus.cir
benchmarks/two_stage_opamp/netlist_settling.cir
benchmarks/inverting_amp/netlist.cir
```

Result: **identical before and after**, per-deck and in total:

| ctype | device_class | count (before = after) |
|---|---|---|
| C | None | 7 |
| E | None | 1 |
| I | None | 2 |
| L | None | 3 |
| R | None | 10 |
| V | None | 30 |
| X | None | 34 |
| X | cap | 8 |
| X | nfet | 210 |
| X | pfet | 194 |
| X | pnp | 10 |
| X | res | 55 |

Every non-null `device_class` in every one of the ten decks is already on an
`X`-prefixed component, which is exactly the case the fix leaves untouched.
No golden fixture under `tests/fixtures/structure_golden/` moved
(`test_derived_structure_matches_the_golden_snapshot` passed unchanged for
all 10 decks).

Manual confirmation against the reported defect's shape (synthetic, not the
real deck):

```
m3  nzero vssi nzero vssi UNITDEV_N_DEP_CAP w=1.5e-6 l=5.55e-6 m=4 nf=1 geomod=0
md0 vssi  vcci vssi  vssi UNITDEV_N_DEP_CAP     w=9.71e-6 l=6.38e-6 m=2 nf=1 geomod=0
```
→ `m3.device_class = None`, `md0.device_class = None` (both keep MOS
terminal roles), `find_patterns` returns `[]` (no self-matches).

## Full-suite result

```
$ .venv/bin/python -m pytest -q
451 passed, 2 skipped in 32.65s
```

(444 passed, 2 skipped on this branch before this change; +7 new tests, no
regressions, no golden fixture moved.)

## Files changed

- `src/analogcoder/structure.py` — `_classify_model` now gated on
  `ctype == "X"`.
- `src/analogcoder/patterns.py` — `PatternMatch.__post_init__` rejects a
  repeated refdes in `members`.
- `tests/unit/test_structure.py` — 3 new tests.
- `tests/unit/test_patterns.py` — 4 new tests.
- `CLAUDE.md` — extended the `patterns.py` Architecture bullet with the
  substring-vs-prefix hazard, the fix, and the mirror-image bandgap case
  (sky130 MOS caps named `…pfet…`, invisible to a cap-name matcher; same
  substring rule, opposite naming convention, opposite failure).

No production netlist content is included anywhere in this repo or this
report; all reproduction decks are synthetic and capture only the relevant
shape.
