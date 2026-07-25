# Area-Aware Parameter Tuning — Design

## Problem

The tuner can currently propose any numeric value change without regard to
how much it grows a component's physical size. A real run against
`spec_topology_required.yaml` showed exactly this: Claude fixed a phase-margin
failure by widening `M6` from `40u` to `100u` — a 2.5x jump — instead of a
smaller, more area-efficient fix. There's no cost signal discouraging this;
the tuner has no reason to prefer a modest change over an aggressive one as
long as it passes simulation.

This adds a **deterministic area growth gate**: before a proposed parameter
change is applied, the orchestrator checks how much it grows the component's
size relative to where that component started this run, and rejects (with
feedback, retryable) proposals that grow a component beyond what its size
tier allows. Consistent with every other guardrail this project has added
(`verify_pre`'s refdes/param checks, the topology-swap threshold), this is
Python arithmetic in the orchestrator, not an LLM asked to police itself —
weak models have already been observed ignoring soft prompt guidance.

## Scope (v1)

- Growth is measured **cumulatively against the run's starting netlist**
  (`netlist_v0`), not against the immediately-prior value. A component that's
  been nudged up several times in small steps is judged the same way as one
  moved in a single big step — both compare against where it started.
- Each **component is checked independently** — no cross-type area budget.
  There's no PDK behind this project (`two_stage_opamp` deliberately uses
  generic ngspice level-1 devices), so there's no real conversion factor
  between a transistor's W×L and a capacitor's farad value; summing them into
  one "total area" number would need an invented weight. Per-component
  ratios avoid that.
- Only **growth** is constrained. A proposal that shrinks a component (ratio
  ≤ 1) always passes the area check, regardless of tier.
- Covered component types, by `refdes[0].upper()` (matching the existing
  `Component.ctype` convention in `netlist.py`):
  - `M` (transistor): area ratio is `W` and/or `L` growth, multiplied
    together if a single proposal changes both for the same refdes. If a
    proposal changes W but not L (the common case — L is rarely tuned), the
    ratio is just `new_W / baseline_W` (L unchanged, so it factors out).
  - `C` (capacitor), `R` (resistor): ratio is `new_value / baseline_value`
    (both use the `param="value"` convention already established for
    positional-value components).
  - Everything else (`I`, `V` sources, etc.) is **not area-constrained** —
    they aren't physical devices with a meaningful size in this abstraction.
- **Topology-swap proposals are out of scope for this gate.** A topology
  swap replaces the whole subckt body with a pre-verified library template
  (already vetted for reasonable sizing) — it isn't an incremental value
  change, so there's nothing to compare against a per-component baseline.

## Size tiers (v1, adjustable later)

Larger baseline components get less room to grow. Boundaries below are a
first pass based on the value ranges already seen in this project's
benchmarks (transistor W 20u–100u, `Cc` 2p–20p) — not derived from any real
area model, since none exists here. Expect to revisit these once there's
more real data.

| Tier | Transistor (`W`, µm) | Capacitor (`C`, pF) | Resistor (`R`, Ω) | Allowed growth |
|------|----------------------|----------------------|---------------------|----------------|
| small  | < 30   | < 3   | < 1k   | **3.0x** |
| medium | 30–80  | 3–10  | 1k–10k | **2.0x** |
| large  | ≥ 80   | ≥ 10  | ≥ 10k  | **1.5x** |

When a single proposal changes both `W` and `L` for the same transistor, the
baseline `W` value (not `L`, which is rarely tuned in this project) picks the
tier; the pass/fail ratio is still the true combined `W×L` growth factor.

## Architecture

### Where the check runs

Inside the existing parameter-tuning retry loop in `orchestrator.py`,
between `agents.tune(...)` and `agents.verify_pre(...)`:

```
for retry in range(1, MAX_TUNING_RETRIES + 1):
    proposal = await agents.tune(...)
    area_ok, area_feedback = check_area_growth(baseline_components, proposal["proposed_changes"])
    if not area_ok:
        rejection_feedback = area_feedback
        continue          # skip verify_pre entirely - no LLM call spent on an already-doomed proposal
    review = await agents.verify_pre(...)
    ...
```

Running the area check first (pure Python, no LLM call) before `verify_pre`
(an LLM call) means an obviously oversized proposal gets rejected instantly
instead of spending an agent call to reach the same conclusion.

`baseline_components: dict[str, Component]` is built **once**, right after
`initial_netlist_text` is read, by indexing every component across the whole
netlist — both top-level and inside any `.subckt` blocks (the tunable
transistors and caps in `two_stage_opamp` live inside its subckt, not at top
level, so both must be indexed the same way `apply_changes` already treats
them: by refdes, regardless of nesting).

### Retry exhaustion: area failures behave like a rollback, not a hard stop

Today, if `verify_pre` rejects a proposal `MAX_TUNING_RETRIES` times in a
row, the **entire run ends immediately** with `FAIL` /
`"tuning proposal repeatedly rejected"`. Folding the area check into the same
retry loop unchanged would mean repeated area rejections end the run the
same way — before ever reaching the topology-swap threshold, defeating the
actual motivation for this feature (steering a blocked, area-hungry tuning
attempt toward the nulling-resistor topology instead of just giving up).

So the two exhaustion causes are handled differently:

- **All `MAX_TUNING_RETRIES` attempts rejected by `verify_pre`** (semantic
  issues — bad refdes, bad param): unchanged, hard-fails the run immediately.
  This is a fundamentally-confused-tuner signal, not something more retries
  at the same topology are likely to fix.
- **All `MAX_TUNING_RETRIES` attempts rejected by the area check** (never
  even reached `verify_pre`): treated like a parameter-tuning rollback —
  `consecutive_rollbacks += 1`, the netlist is untouched (nothing was ever
  applied, so there's nothing to roll back), and the orchestrator `continue`s
  to the next `outer_iter`. If this keeps happening, `consecutive_rollbacks`
  eventually crosses `TOPOLOGY_SWITCH_THRESHOLD` and a topology swap gets
  offered — exactly the "too-expensive parameter fix → try a different
  topology instead" behavior this feature exists for.

If the 3 retries within one `outer_iter` have mixed rejection reasons (e.g.
attempt 1 area-rejected, attempt 2 `verify_pre`-rejected, attempt 3
area-rejected again), the rule is: **hard-fail if any attempt reached and
was rejected by `verify_pre`; soft-continue only if every attempt was
caught by the area gate first.** One `verify_pre` rejection anywhere in the
exhausted set is enough to trigger the existing hard-fail behavior.

## Components

### `src/analogcoder/netlist.py` (extend)

New pure function, alongside the existing `parse_netlist`/`apply_changes`:

```python
def parse_spice_value(s: str) -> float:
    """Parse a SPICE-style numeric literal ("40u", "2p", "1.5MEG") into a float."""
```

Standard SPICE suffix table (case-insensitive, longest-suffix-first so
`"meg"` isn't mistaken for `"m"` + trailing text): `T=1e12, G=1e9, MEG=1e6,
K=1e3, (none)=1, M=1e-3, U=1e-6, N=1e-9, P=1e-12, F=1e-15`. Any additional
trailing letters after a recognized suffix (e.g. a stray unit name) are
ignored, matching real SPICE parsers' tolerance.

### `src/analogcoder/area_limits.py` (new)

```python
from dataclasses import dataclass
from analogcoder.netlist import Component, parse_netlist, parse_spice_value

@dataclass(frozen=True)
class SizeTier:
    max_value: float | None   # None = this is the top/unbounded tier
    allowed_multiplier: float

TRANSISTOR_TIERS: list[SizeTier]   # W in meters: <30e-6, <80e-6, unbounded
CAPACITOR_TIERS: list[SizeTier]    # C in farads: <3e-12, <10e-12, unbounded
RESISTOR_TIERS: list[SizeTier]     # R in ohms: <1e3, <10e3, unbounded
TIERS_BY_CTYPE: dict[str, list[SizeTier]]  # {"M": ..., "C": ..., "R": ...}

def index_baseline_components(netlist_text: str) -> dict[str, Component]:
    """Index every component (top-level and inside any .subckt) by refdes."""

def check_area_growth(
    baseline_components: dict[str, Component], proposed_changes: list[dict]
) -> tuple[bool, str | None]:
    """Returns (approved, feedback). feedback is None when approved."""
```

`check_area_growth` groups `proposed_changes` by `refdes` (so a same-refdes
`W`+`L` pair combines multiplicatively), looks up each baseline value via
`baseline_components[refdes]` (using `.value` for `param == "value"`,
`.params[param]` otherwise), computes the combined ratio, and — for growth
only — compares it against `allowed_multiplier_for(ctype, tier-selection
baseline)`. On violation, the feedback string names the refdes, the
computed ratio, and the tier's limit, in the same style `verify_pre`'s
rejection feedback already uses.

### `src/analogcoder/orchestrator.py` (modify)

- Build `baseline_components = index_baseline_components(initial_netlist_text)`
  once, alongside where `analysis` and `topology_swap_available` are
  currently computed.
- In the parameter-tuning retry loop: call `check_area_growth` right after
  `agents.tune(...)`, before `agents.verify_pre(...)`, per the Architecture
  section above. Track within the loop whether any attempt was rejected by
  `verify_pre` (vs. only ever by the area check), to pick the correct
  exhaustion behavior when the loop ends without an approved proposal.
- Log a new `state.log_event("area_check", {...})` per retry attempt
  (mirrors the existing `tuning_proposal`/`verify_pre` logging), so
  `history.jsonl` shows why a specific attempt was blocked.

## Error handling

No new error-handling paths. `check_area_growth` and `parse_spice_value` are
pure, exception-free functions operating on already-schema-validated
strings (`TUNER_SCHEMA`'s pattern already guarantees `new_value` parses as a
number+optional-suffix) and a netlist parsed at run start — nothing here
calls an agent, so `AgentExecutionError`'s existing top-level catch is
unaffected and doesn't need to know about this feature.

## Testing

1. `tests/unit/test_netlist.py` (extend) — `parse_spice_value`: each suffix
   (`p`, `n`, `u`, `m`, `k`, `meg`, `g`, `t`, no suffix), case-insensitivity,
   negative numbers, and the `m` vs `meg` disambiguation specifically.
2. `tests/unit/test_area_limits.py` (new) — `index_baseline_components`
   finds both top-level and subckt-nested components by refdes;
   `check_area_growth`: growth within tier limit passes, growth exceeding it
   is rejected with a feedback string naming the refdes and ratio, shrinkage
   always passes regardless of tier, combined W+L growth on one refdes
   multiplies correctly, a refdes/param with no area constraint (e.g. an `I`
   source) is never rejected.
3. `tests/unit/test_orchestrator.py` (extend) — mocked agents: a proposal
   exceeding its tier limit is rejected without `verify_pre` ever being
   called (assert call count); exhausting all retries via area rejection
   alone increments `consecutive_rollbacks` and continues to the next
   `outer_iter` rather than failing the run; exhausting retries where at
   least one attempt reached and was rejected by `verify_pre` still hard-fails
   immediately (regression test for the existing behavior); repeated area
   rejection across enough iterations eventually triggers a topology-swap
   offer (composes with the existing `TOPOLOGY_SWITCH_THRESHOLD` test
   pattern from the topology-swap-tuning branch).
