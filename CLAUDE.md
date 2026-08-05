# analogcoder

CLI that automates iterative analog circuit verification and repair: run a SPICE
simulation, judge the result against pass/fail criteria from a spec, and if it
fails, propose and apply netlist parameter changes, then re-verify — repeating
until it passes or hits iteration/retry limits.

## How to read this file

Every bullet is a **rule** or a **disproved assumption**, not a module summary.
Where a bullet names a number, that number was measured. Full evidence, tables
and derivations live in `docs/superpowers/specs/` and each entry points at its
doc — read the doc before re-opening a settled question.

### The four standing questions

Ask these while writing the thing, not after:

1. **"What does the log look like when this gate does nothing?"** A gate that
   ships passing but unable to fail is this repo's most repeated defect.
2. **"Was the condition under which this metric could return a different answer
   present in the runs I measured?"** Ask it while *choosing the runs*.
3. **"Is this check on the path that decides the verdict?"** A check present in a
   cheap screening stage and absent from the deciding stage is defect #12's
   shape, and it has recurred in a measurement script since.
4. **"Is my own self-check verified?"** A results document once *claimed* it had
   passed question 2 and the claim was false. A sentence asserting "this could
   have come out differently" must show the value's definition, or it is not a
   check.

### Silently-inert gates — running total **12**

`.option scale` read as metres · include-only wrapper cells · wrapper instance
parameters · a zero area baseline · `unconstrained_refdes` logging `[]` while the
devices it covered were tiered against another topology's geometry ·
`topology_unavailable` with no reason code · `render_corner_netlist`'s supply
rewrite (made a *measurement* meaningless, not just a check) ·
`scripts/search_ab.py`'s corner regime (caught in review, never shipped) ·
`corner_render` never reaching either full sweep (found by a measurement run, and
it covered the sweep that decides the verdict) · and three from curation:
`tunable_range` taking the direct branch where judging takes the traced one, a
zero-tolerance Pareto that could not reject on a coupled multi-block slot, and
the incumbent's own point excluded from that comparison.

The count enumerates **gates only**. Metrics with the same defect (D1's `0.000`)
are not folded in — that would make the number mean two things.

### Rules that generalise past any one module

- **`re.sub` / `str.replace` are silent by construction.** Any rewrite this repo
  *asks for* must be counted (`re.subn`) and its result recorded.
- **When you fix a shape inside a function, read the rest of that function for
  it.** `render_corner_netlist` carried a comment diagnosing the exact silent
  no-op six lines above a line that still had one.
- **When a gate's rule changes, re-read the prompt that mirrors it.** A prompt
  stricter than its gate is safe; a prompt that contradicts its gate converts an
  approved proposal into a run-ending one.
- **Where `netlist.py` owns a parsing rule, import it.** `compose.py` hand-copied
  the include rule and diverged in both directions. `simulators/cache.py` imports
  the regexes for this reason.
- **Never recognise meaning by name.** Forbidden guesses, each already paid for:
  a supply rail by `vdd`/`^Vdd`, a corner axis by filename, a device class by an
  instance parameter's name, "top-level passive" as testbench apparatus. What is
  decidable by a *parsed fact* (refdes prefix + top-level scope; "a top-level
  independent source connects here") is allowed.
- **Patterns never guess.** A match is a fact, a non-match is silence. The
  acceptance bar is zero false positives, not recall — silence or a truthful
  label, never a wrong label with a footnote.
- **A measured fact beats a declared one.** `addresses` is measured, never
  declared; regressions come from the judge's own `pass` flips, never from
  `verify_post`'s `regressed_criteria` (schema-attached but still an LLM claim).
- **The result must describe the deck it returns.** Recurred five times
  (`final_criteria`/`final_netlist_paths`, topology swaps, corner-reduction
  re-entry, the PVT sweep absent from `report.md`, promotion re-entry).
- **Pre-registration is binding.** Fix the verdict rule before results arrive;
  never edit it after. A negative result is an artifact, not a failure. This repo
  has paid for post-hoc rule changes and explicitly retracted the practice (D1).
- **A distinction is a fact.** `null` means "no value in this field", `NaN` means
  "measured, and no value came out"; `capped` means "unknown", not "absent";
  `refused` means "not measurable", not "measured and empty"; `void` means the
  measurement cannot answer. Collapsing any pair destroys evidence.

## Architecture

Three independent LLM agents (simulator, tuner, verifier) coordinated by a
deterministic (non-LLM) Python orchestrator in `orchestrator.py`. The
orchestrator never parses free text — every agent call returns JSON validated
against a fixed schema (`schemas.py`). **Judging is not one of them** — the
`judge` slot on `OrchestratorAgents` is still awaited and still logged under the
event name `judge`, but `cli.py` fills it with `judge_tools.evaluate_criteria`
directly (see the deterministic-derivation section).

### Backends and agents

- `agents/backend.py` — `AgentBackend` interface, `ToolSpec`,
  `AgentExecutionError`. All LLM execution is behind this interface; agent modules
  never call an LLM SDK directly.
- `agents/backends/claude_sdk.py` — default backend, wraps claude-agent-sdk and
  rides a Claude Code subscription (no separate API key needed).
- `agents/backends/openai_compatible.py` — any OpenAI-style
  `/chat/completions` endpoint, for eventually running against a lower-capability
  local LLM. Has its own tool-call loop and a schema-validation-with-repair retry
  loop, since local models are much less reliable at strict structured output.
- `simulators/base.py` / `simulators/ngspice.py` — `SimulatorBackend` adapter for
  swapping the SPICE engine. Only ngspice is implemented; HSPICE is a documented
  future backend, and implementing it is the first task of any production port.
- `agents/*.py` — one file per agent: system prompt + schema + tool declarations
  (`ToolSpec`, not provider-specific). Every public function takes a required
  `backend: AgentBackend` as its last positional arg.

### Spec, topologies, and the area gate

- `spec.py` — `spec.yaml` declares one or more **testbenches**, each with its own
  netlist file, control block and criteria. `TargetSpec.canonical`
  (`testbenches[0]`) is the text handed to the tuner, `verify_pre` and the
  area-baseline indexer; `all_criteria` flattens every testbench's criteria for
  one `judge` call. `simulate` fans out per testbench; `judge`/`tune`/
  `verify_pre`/`verify_post` stay at one LLM call per iteration, so a change that
  fixes one testbench cannot silently regress another. `RunState` (`state.py`)
  versions each testbench's netlist independently but always in lockstep —
  `push_netlist_version`/`rollback` are atomic across testbenches, never partially
  applied. See `2026-07-25-psr-verification-design.md`.
  - **A criterion's operator is validated at load against `ALLOWED_OPERATORS`
    (`>=`, `>`, `<=`, `<`), and `==` is refused even though `evaluate_criteria`
    implements it.** Three consumers disagree on what `==` means:
    `relative_slack` and `baseline_ratio_allowances` fall to the upper-bound
    branch, `guard_band_violations` skips it. For `vref == 1.2` measuring 1.0,
    `relative_slack` returns **+0.167** — positive slack on a *failing* criterion,
    so `_tightest_slack` under-reports it; and `baseline_ratio_allowances` hands it
    an allowance nothing applies, so the report renders *"0 (every criterion is
    corner- or ratio-guarded)"* over a criterion with zero guard. Three modules
    disagreeing is a reason to refuse, not to pick one — the same shape as
    `render_corner_report` raising on a supply line it cannot rewrite. The same
    gate closes a typo: before this, `operator: "=~"` loaded fine and surfaced as a
    `KeyError` from `judge_tools._OPERATORS` in the middle of a 45-corner sweep,
    and `cli.py`'s bare-`Exception` guard cited that as its third example.
    (Enumerating exception types is guessing — and that list losing a member is the
    proof.) Zero shipped specs are affected: all **217** criteria across the 15
    benchmark specs are `>=` or `<=` (117 / 100).
    - **A fourth consumer implements `==` correctly, and the refusal withdraws it.**
      `curation._at_least_as_good` reads `==` as "at least as good when inside a
      two-sided band", with `COMPARISON_REL_TOLERANCE = 1e-3` — the tolerance
      decision the refusal says nobody made was already made there, from
      measurement (noise 4.2e-5, real difference 0.102), and `_is_better` shares
      the same band. Since `_load_criteria` is the only `Criterion(...)`
      construction site in `src/`, blocking `==` makes curation's **entire
      equality-aware Pareto path unreachable from any spec**: a slot spec with a
      centred criterion (`vref == 1.2`) now fails at load. That is a tested,
      reviewed capability being withdrawn — the price of refusing. Reopening it
      means **making the three judging sites agree**, and the default should be to
      adopt curation's measured band rather than derive a fourth answer.
      `curation._unjudgeable_operators` becomes unreachable-from-a-spec as a
      by-product; it is **not** added to the silently-inert ledger, because the
      gate that now decides is `ALLOWED_OPERATORS` and it fires loudly at the
      boundary — an inert *stage* behind a live gate is not an inert gate. Total
      stays **12**.
- `area_limits.py` — a deterministic size model run on every tuning proposal:
  it computes how far a proposal grows a component past a size-tiered limit
  relative to `netlist_v0`, since there is no PDK here to derive a real area
  model. Motivated by a real run where a phase-margin failure was "fixed" by
  widening a transistor 2.5×. See `2026-07-25-area-aware-tuning-design.md`.
  - **It stopped rejecting on 2026-08-05 — it computes, records and reports, and
    the proposal goes through.** The reason for the demotion is that the tier
    ceilings are numbers with no derivation behind them, and the fix for growth
    now exists downstream: the area-minimisation phase runs unconditionally after
    PASS and takes it back. See
    `2026-08-02-area-first-optimization-design.md` and
    `2026-08-05-area-first-stage2-stage3.md`.
  - **`evaluate_area_growth` itself was not touched, on purpose.** The optimizer
    phase and curation call the same function, so changing what it *computes*
    would move three consumers at once. What changed is one call site's reaction
    to the result.
  - **`area_check` now carries `blocking: false`, written unconditionally.** The
    absence of the key and `false` must differ, or "the gate was demoted" and
    "this instrumentation is gone" look identical — the same rule as
    `tuning_retries` and `margin_floor_rule_applied`.
  - **Deleting the gate outright was rejected**: then growth becomes invisible,
    which is the silently-inert-gate mistake pointed the other way.
  - **Two behaviours the demotion removed, both re-pinned through another gate.**
    An oversized proposal used to be rejected *before* the LLM `verify_pre` call
    (it now spends one), and exhausting all retries on area rejection alone used
    to escalate into a topology swap. That escalation still exists — it just
    needs `refdes`/`param`/`stimulus`, which still reject — and the tests that
    pinned it were retargeted rather than deleted. `"area"` stays in
    `REJECTION_REASONS`: past `history.jsonl` files carry the code and
    `attempt_log` renders it, so a new run counting 0 and the key vanishing are
    different facts.
  - Tiers are keyed on **scaled** geometry for PDK primitives (`W`/`w`/`l` times
    `.option scale`) and on the component's own value for generic M/C/R. Reading a
    bare `W=30` as absolute put every PDK device in the unbounded 1.5× tier. A
    `pnp_05v5` is tiered on its emitter multiplier `m`, a count not a length.
  - Where a deck is built from **wrapper cells** (geometry declared as subckt
    parameters, real numbers on the instance line), the gate **traces** each
    instance parameter to the body token it lands on
    (`params.annotate_traced_params` → `Component.traced_params`) and tiers on
    that. It never reads meaning out of the instance parameter's *name* — those are
    the designer's convention. The **body token** name (`w`, `l`, `m`, `nf`) is
    standard SPICE device syntax, so it is a fact.
  - **The gate never blocks shrinking** (`evaluate_area_growth` short-circuits on
    `ratio <= 1.0`). Any code describing a symmetric `[baseline/M, baseline*M]`
    window as "the area gate's allowance" is wrong on the low half.
- **The tuner may offer up to three alternatives, and they are measured before
  one is chosen** (2026-08-05, `2026-08-05-area-first-stage2-stage3.md`).
  `alternatives` is optional in `TUNER_SCHEMA` — **a proposal with one change set
  takes a path byte-identical to before**, including simulating exactly once,
  because there is nothing to choose between. Screening only happens at 2+.
  - **The order is gates → `verify_pre` → simulate → choose, and `verify_pre`
    before simulation is the load-bearing part.** Picking by measurement lets
  cheating win: scaling `Vin`'s AC amplitude lifts `gain_db` 20 → 60 with the
  circuit untouched, and shrinking `Cload` improves phase margin and UGBW at once.
  - **The choice rule has two branches and both must stay alive.** If any
    alternative passes, the smallest total area among the passing ones wins; if
    none passes, the largest improvement wins. Improvement alone would make the
    area computation a gate that never changes a decision; area alone would keep
    picking tiny changes that never converge. `alternatives.py` holds the rule as
    a pure function so both branches are pinned without the orchestrator.
  - **`area_after is None` means "could not measure", never area 0**, so it can
    never win by default; when *every* passing candidate is unmeasurable the rule
    name itself says so (`max_improvement_area_unmeasurable`) — otherwise "nothing
    passed" and "area was unmeasurable" would read identically in the log.
  - **`tuning_alternatives` is written every retry, and `screened` is the
    denominator.** A retry with one candidate had no choice to make; a retry that
    screened and found ≤1 passing candidate did. Collapsing them makes "the
    multi-pass branch fired 0 times" unreadable — and 0 firings is the
    pre-registered signal to revert this whole change.
  - **Screening does not use the simulator agent** (`cli.screen_simulate`). The
    agent converges a control block, and a control block is a property of the
    *testbench*, not of the parameter values — which is why `corner_sim` reuses
    one across 45 corners. The design spec's cost paragraph assumed `simulate`
    was a SPICE call; it is an LLM agent fanned out per testbench, so screening
    through it would take bandgap from 5 to 15 simulator LLM calls per iteration
    on the axis this repo has measured to dominate wall clock. Two consequences
    are recorded rather than fixed: screening measures **the text it is handed**
    (`simulate_fn` reads `state.current_netlist_paths()` instead), and screening
    stays at **nominal even when corner reduction is active** — running the corner
    wrapper would advance the probe rotation for a candidate that may be discarded.
    Screening chooses; the winner is re-measured by `simulate` and judged there.
- `topologies.py` / `topology_match.py` — a small curated library of pre-verified
  amplifier bodies the orchestrator can swap in as a last resort after repeated
  rollbacks (`TOPOLOGY_SWITCH_THRESHOLD`), instead of only ever changing values.
  Four entries, each declaring the `ports` its body requires and the
  `assumes_scale` its geometry is written in.
  - `topology_match.compatible_swaps` judges each `(block_path, topology_id)` pair
    and is a **candidate generator, not a gate**. `tried` is a set of *pairs* —
    trying an entry on one block says nothing about another.
  - Four rules: **ports** (one-directional subset, order ignored — the body's
    ports must all exist on the block; the block's leftover ports are judged
    separately by `_leftover_ports_float_reason`. Equality was relaxed to a subset
    **on purpose**, because equality rejects a legitimate drop-in for a block
    carrying extra bias ports — `bc53d9e` relaxed it deliberately.
    **Do not "restore" equality.**), **models** (the
    body's model names must already appear in the deck — decidable without
    following `.include`), **`.option scale`**, and **`identical_body`**.
  - **`identical_body` is judged against the *current* deck, so it is not a static
    property of an entry.** `miller_basic` is byte-identical to `OPAMP2STAGE`'s own
    body, so `two_stage_opamp` offers 1 candidate not 2 — and after a swap the
    roles invert. Likewise `folded_cascode_nmos_in_cs` ≡ `TRIMAMP` and
    `folded_cascode_pmos_in_cs` ≡ the shipped `BUF_P`, taking bandgap from 8 raw
    pairs to 6 candidates. Without this rule the agent can pick a swap that changes
    nothing, spending an outer iteration and resetting `consecutive_rollbacks` —
    delaying the very escalation that triggered it.
  - A candidate must be compatible in **every** testbench versioned together
    (`missing_in_testbench`); `push_netlist_version` is atomic, so a swap applied
    to only some decks would have `judge` merging measurements from two different
    circuits.
- **A failed escalation must never be worse than not escalating.** When the
  topology proposal loop exhausts its retries the run does not end: it logs
  `topology_unavailable` with a reason, resets `consecutive_rollbacks`, and falls
  through to parameter tuning. `block_path` is deliberately **not** in
  `TOPOLOGY_SCHEMA`'s `required` (a required field a weak model omits hard-FAILs
  every spec), and an omitted one is only resolvable when a single candidate
  carries that `topology_id` — never true on bandgap, where 3–4 blocks share each
  entry. Measured on `spec_seed_topology.yaml`: with `block_path` supplied, PASS at
  iteration 4 (`buf0_gain_db` 100.158); with it omitted, the old code returned FAIL
  with the deck back at `netlist_v0`, throwing away six iterations and a working
  tuning path. It now ends `max iterations reached` at 81.643 dB.
  **The prompt requires `block_path` while the schema does not, and that asymmetry
  is deliberate** — do not "fix the inconsistency".
- **`topology_unavailable` carries a reason code, and that is the point.**
  `no_subckt_definitions`, `empty_library`, `all_pairs_already_tried`,
  `all_pairs_rejected`, `proposal_unresolved`. Known precision limit:
  `all_pairs_already_tried` requires *every* rejection to be `already_tried`, and a
  real multi-block deck always also carries `ports` and `identical_body`
  rejections, so genuine exhaustion there reports `all_pairs_rejected`. The finer
  fact survives per-pair in the `topology_candidates` event.
- **The area gate's baseline is `netlist_v0` and is deliberately never refreshed
  after a swap** — swapped-in components have nothing in the original to compare
  against. The `topology_swap` event logs *two* lists: `unconstrained_refdes` (no
  baseline entry) and `stale_baseline_refdes` (has one, but parameters differ). The
  second exists because the first alone was misleading: on four of bandgap's six
  candidates `unconstrained_refdes` is `[]` while ~14 of 15 devices are tiered
  against the previous topology's geometry. Post-swap `BUF_P.Xt` is `W=24` against
  a baseline of `W=8`, so a proposal of `W=48` is scored 6.0× instead of its true
  2.0× — conservative for these four entries, but that is an accident of these
  bodies, not a property of the rule.
- Swaps are accumulated across corner-reduction attempts and each record carries an
  `attempt` index — `outer_iter` restarts per attempt and `tried` resets, so one
  block can legitimately be swapped in more than one attempt.

### Topology curation — how a new library entry gets in

`curation.py` / `agents/curator.py` / `agents/variant_author.py` /
`cli_curate.py` — a second console script, `analogcoder-curate`, that decides
whether a candidate topology earns a place in `TOPOLOGY_LIBRARY`.

- **It never writes the library.** It emits `curation_report.md`, `curation.json`
  and a `topology_candidate.py` snippet, and a human commits. "Pre-verified" has to
  keep meaning "a person read the measured evidence".
- **A library entry exists for exactly one reason: to reach where parameter tuning
  cannot.** That is why the gate's centre is not "does it simulate and reproduce
  its numbers". Submission is `(candidate, verification slot)`, because "better"
  needs an incumbent to be better *than*. Stages: structure (reuses
  `compatible_swaps`) → characteristic reproduction (measures candidate **and**
  incumbent) → corner verification → scoped comparison. Verdicts are
  `ADMIT`/`REJECT`/`INCONCLUSIVE`; a crash at any stage still writes all three
  artifacts.
- **Corner verification is required for `provenance == "authored"` only, and that
  asymmetry is the entire licence for letting an LLM write SPICE here.** The tuner
  is forbidden from authoring structure because its proposals reach the deck with
  only text gates in between. Curation is the opposite: a three-stage simulation
  gate, a corner sweep and a human commit sit in between, and the authoring is a
  *local modification of an already-sized working block*. The danger was never
  authoring; it was **application without verification**.
- **The scoped comparison sweeps one knob at a time and says what it looked at** —
  knobs, ranges, point counts, simulation totals, omitted/unresolved knobs and
  excluded points all land in the record. It never claims to have excluded all
  tuning: this repo has twice found the winning fix in a knob combination no
  single-knob sweep tried.
- **`COMPARISON_REL_TOLERANCE = 1e-3`, and the value is arithmetic, not a round
  number.** Zero-tolerance Pareto could not reject on a real multi-block slot and
  manufactured claims out of solver noise — both measured on one run: the Ahuja
  candidate came out ADMIT with `dominating: None` because two criteria physically
  decoupled from the swept knob sat `0.0011°` and `0.0001 dB` short at every point,
  while `+0.0028°` became a *measured* `addresses` that `agents/tuner.py` renders
  straight into the swap prompt. Largest measured noise is `4.2e-5`, real
  improvement `0.102`, so 1e-3 sits ~24× above noise and ~100× below signal.
  Applied symmetrically to `_is_better` and `_at_least_as_good`. Set it to 0 and
  the shipped run ADMITs again — that counter-run is pinned.
- **The cheapest tuning is changing nothing, and the gate excluded it.** The
  incumbent now enters the Pareto test as a labelled zero-cost point
  (`point="incumbent"`, `simulated_here: False`). It needs only a knob whose
  baseline sits near an optimum — which is what a shipped design *is*.
- **A count-based knob cap is order-dependent, and the default made the gate
  blind.** `--max-knobs 8` truncated alphabetically and `TRIMAMP.XRz.l` is the 9th
  of 30, so the knob that decides the case was never swept. The cap is now opt-in;
  the honest default is "sweep everything" — measured **120 simulations /
  2 min 41 s** on a single-testbench slot. A 5-testbench slot is ~12 min, which is
  why single-testbench validation slots are the documented recommendation.
- **`verified_at` is a property of (body × slot), not of the SPICE text.** A
  45-corner PASS was earned inside the body's *original* surrounding circuit, so
  extracted and file candidates ship `verified_at="nominal"`.
- **All three candidate sources share `reject_unreferenced_ports`.** Only source B
  used to check that a declared port is referenced by the body; the justification
  ("stage 1 judges port compatibility anyway") was false, because the ports handed
  to stage 1 are the block's own, making the subset test an identity. It reads
  `component.nodes` only, so a port referenced solely inside a behavioural
  expression is reported unreferenced — it fails **closed**, and zero shipped
  blocks trip it.

### The optimization phase

`optimizer.py` / `agents/optimizer.py` / `area.py` — **two phases** run after the
loop returns PASS and before the final PVT sweep, spending the spec's remaining
margin. The **area phase** (`run_area_optimization`, `PhaseConfig` `AREA_PHASE`)
runs first, on **every spec** — it requires no `optimize:` declaration and calls no
LLM. The **objective phase** (`run_optimization`, `phase_from_spec`) runs second,
only when `spec.yaml` declares an `optimize:` block, spending remaining margin on
the objective that block names. Both share `_optimize`/`_result`/`accept_step`
through the `PhaseConfig` data (`objective`/`area_budget`/`guard_band`/`label`);
`report.py`'s `_area_optimization_lines` and `_optimization_lines` render one
section each (Korean `## 면적 최소화` / English `## Optimization`).

- **The area phase has no agent at all — that is its headline property.**
  `rank_by_area_gain` (`area_ranking.py`) orders every tunable knob by the
  deterministic area a one-step shrink would give up, and
  `run_area_optimization` injects that fixed ranking into
  `OptimizerAgents.knob_ranking` so `_knob_ranking` never calls `agents.propose`.
  `test_the_area_phase_calls_no_agent_at_all` pins it by making `propose` raise.
- On the **objective phase**, the agent only **ranks knobs**: `OPTIMIZER_SCHEMA`
  structurally forbids a value, and a deterministic search decides how far to move
  each one (`×0.9` per step for a geometry, `±1` for a count) and measures the
  result.
- **Which search runs is swappable (`OptimizerAgents.search_strategy`), and the
  seam has one rule: a strategy proposes, it never judges.** `SearchRun` exposes
  `spend_step`/`knob_state`/`attempt`/`exhausted`/`log_event` and nothing that
  writes the tallies — `accepted`/`rejected`/`best_objective` are read-only, so the
  numbers describing the returned netlist never pass through a strategy.
  `log_event` is the **only** door to `history.jsonl`, and it prefixes the phase
  label itself (`f"{phase.label}_{suffix}"`); the parameter is named `suffix` so a
  call site cannot mistake it for a full event name. Before that, a strategy could
  emit an unlabelled event, which is indistinguishable between the area and
  objective phases when both run — `mads.py`'s three call sites had exactly that
  defect and one fix closed both. **Consequence: `runs/search_ab/` artifacts written
  before 2026-08-03 carry `mads_poll`, not `optimize_mads_poll`.** A renamed event
  leaves old artifacts wearing the old name.
- **Measured on real ngspice runs (2026-08-02).** `benchmarks/bandgap/spec.yaml`:
  19.19% area reduction (1.10546e-08 → 8.93292e-09), 16 steps accepted / 4
  rejected, ~123.5s. `benchmarks/two_stage_opamp/spec.yaml`: UNCHANGED, 0 accepted
  / 20 rejected (every top-ranked candidate immediately broke a criterion), ~24.1s.
  Bandgap's accepted steps drained real margin: `buf0_phase_margin` 104.39° →
  81.89° (relative slack 0.305 → 0.024), `buf1_phase_margin` 101.56° → 82.99°
  (0.269 → 0.037) — against a spec that declares no `pvt_corners`, so nothing
  downstream re-checks it. **This is a cost that was invisible before the
  `unguarded_criteria` plumbing described below, not a defect being introduced
  now**: the area phase has no ratio guard band (`AREA_PHASE.guard_band is None`),
  and an unguarded criterion can only ever surface in an **acceptance**, never a
  rejection — with allowance `0.0`, `guard_band_violations`' limit collapses to the
  exact predicate `accept_step` already checked one line earlier as `overall_pass`.
  Scanning rejections for this risk, as this feature's own first measurement did,
  is structurally blind to it. **The stage-1 plan's "확정됨" decision — that the
  area phase carries no ratio guard band — was reverted on that evidence**
  (`docs/superpowers/plans/2026-08-02-area-optimization-phase.md`): the accepted
  steps broke **2 of 22** criteria at corners on a deck that passed all 45 before,
  firing the pre-registered revert rule of
  `2026-08-02-area-phase-guard-measurement-results.md`. What the revert bought is
  the four bullets below — measured, and **not** a value.
- **A margin floor for the area phase was pre-registered, measured over 14
  combinations, and *not adopted* — `AREA_PHASE.margin_floor` stays `None`.** Three
  rule shapes were pre-registered (F1 fixed relative slack `f`, F2 ratio `r` of the
  baseline slack, F3 corner-measured-where-available) but **only two arms were
  measured**: the locked grid holds F1 and F2 over 2 (deck, grid) pairs, and
  `rule="f3"` was never run. F3's corner half is what `corner_allowances` already
  does for *every* rule, and its corner-less half is "whichever of F1/F2 wins" — a
  choice **rule 3 fired before the measurement could make**. So F3 has no defined
  meaning today and **`_margin_floor_allowances` raises on it**; it used to resolve
  it to `f1`, and that choice was arbitrary, not F3's definition. The failure that
  refusal prevents: the next pre-registration takes F2 `r=0.75` as the winner and
  wires `MarginFloor("f3", 0.75)`, the old code reads 0.75 as f1's `g`, bandgap
  demands `vbgout >= 2.1` **and** `<= 0.32`, every criterion is guard-infeasible at
  baseline, 0 steps are accepted and the run reports a clean `UNCHANGED`.
  **Pre-registration verdict rule 3 fired** — no `(rule, value)` was safe on both
  pairs — so the whole family is rejected and any alternative needs a *new*
  pre-registration; no value was chosen here. The one safe **and** useful point in
  the entire grid was F2 `r=0.75` on `benchmarks/bandgap`: 8 accepted / 12
  rejected, area −10.74% (1.10546e-08 → 9.8670e-09), 45-corner sweep PASS. It
  rests on **one deck**, and the pre-registration conceded up front that the
  **grid axis is untested** — both pairs' 45-corner grids share identical
  coordinates — so it is an input to the next pre-registration, never a value to
  wire in. `2026-08-02-area-phase-margin-floor-results.md`.
- **P2's whole arm is `void`, and the cause is a defect in the pre-registration,
  not in the floor.** `two_stage_opamp/spec.yaml`'s baseline already fails
  `phase_margin` at nominal (34.5636° against `>=60`, relative slack **−0.42394**)
  and `accept_step` requires *every* criterion to pass, so all 7 P2 combinations
  returned 0 accepted / 20 rejected regardless of the floor's rule or value. **The
  condition under which P2 could have returned a different answer was never
  present.** The pre-registration verified that P2's corner-less and corner specs
  carry identical criteria, but never that P2's baseline *passes* — and in
  production the area phase runs only after the loop returns PASS (`cli.py`), so
  the measurement ran it in a state it never sees. **This is standing question 2
  asked too late**: ask it while *choosing the runs*. Both readings are on the
  record and **neither is resolved** — rule 3 fired *literally* and its action
  stands, while `void` is a record label the controller added *after* results (the
  locked definition of "safe" is binary, and no verdict moves under either
  reading). Which reading governs P2 next time is deliberately left open, to be
  fixed **before** results arrive.
- **The only baseline-feasible F1 value accepted exactly the same steps as having
  no floor at all.** `f1=0.02` on bandgap: 16 accepted / 4 rejected, 1.10546e-08 →
  8.932917e-09 (19.19%), failing `buf0_phase_margin` 65.0993 at `sf/1.98/125` and
  `buf1_phase_margin` 76.0735 at `sf/1.62/-40` — digit-for-digit the unguarded
  control in `2026-08-02-area-phase-guard-measurement-results.md`. On this deck and
  this grid the one F1 value the baseline could carry was indistinguishable from no
  gate. **It was considered for the silently-inert-gate running total and
  deliberately not counted**: that number enumerates gates in shipped or reviewed
  *code*, and this is a measured grid point that was never a gate in this codebase
  — folding a second kind of thing in would make the count mean two things.
- **The revisit trigger is the tightest relative slack now, because the old one
  could not fire where the risk lives.** "The area phase's accepted steps break at
  a later corner sweep" is unobservable on a spec that declares no corners —
  exactly the specs where every criterion is unguarded. So every run records the
  smallest relative slack across all criteria and the criterion it belongs to, at
  `result["area_optimization"]["tightest_slack"]` (and
  `result["optimization"]["tightest_slack"]` for the objective phase), rendered in
  both `report.md` sections. `None` is drawn as its own sentence and never as a
  value — it means no criterion's slack was computed at all. This is recorded
  **independently of whether a floor is ever adopted**; with rule 3 fired it is the
  *only* observation channel there is.
  - **`tightest_slack` is the *landed deck's* minimum, not the run's**, and the
    pre-registered sentence has two clauses that live in two different places. The
    clause it satisfies is the second ("at the end of the run leave the minimum and
    its criterion name in the result and the report"); the first ("record every
    criterion's relative slack **after each accepted step**") is the `{label}_step`
    event's `criteria_slack`. They diverge in a configuration this repo has already
    measured: bandgap's objective phase accepts 10 steps, the confirmation sweep
    fails, bisection lands on v4 — `tightest_slack` describes v4 while v10, which
    was tighter, exists only in the step events. `criteria_slack` holds **every**
    criterion (no NaN filtering, unlike `_tightest_slack`, which drops NaN out of a
    *min* competition), and is `None` on a step that was not accepted — the key is
    written on every step event so "not accepted" and "the instrumentation is gone"
    differ.
- **Nothing recorded that a floor was in force until it did, and `f1` is the
  reason.** For `f1` the resulting allowances dict is byte-identical to what a
  declared `guard_band=g` produces, so from `history.jsonl` alone "no floor +
  `guard_band: 0.2`" and `MarginFloor("f1", 0.2)` were indistinguishable.
  `{label}_baseline` now carries `margin_floor_rule_requested` and
  `margin_floor_rule_applied`, **written unconditionally** (`None`/`None` when no
  floor was supplied) for the same reason `tuning_retries` and `corner_seed` are.
  **The asymmetry between the two fields is the whole signal**: a floor handed to a
  corner-capable run is never consulted (measured allowances beat a guess, by
  design), and that shows up as `_requested` naming the rule while `_applied` is
  `None`. Without it a follow-up asking "does the floor still help where corners
  *are* measurable" gets a result identical to no floor and concludes the floor is
  a no-op at corners, with nothing to contradict it. The rule name is still read in
  exactly one place — `_margin_floor_allowances` returns it alongside the
  allowances rather than letting the log site re-read `floor.rule`.
- **The accept rule deliberately does NOT reuse `verify_post`**: that contract is
  "roll back if regressed", and a good optimization step consumes margin on
  purpose. A step is kept only if every criterion still passes with its guarded
  margin, the objective fell, and total area is inside the budget.
- **Optimization has no FAIL outcome, and that includes crashing.**
  `_run_simulation` and `_run_sweep` each swallow a bare `Exception` for that
  reason; the module's one LLM call did not, so `run_optimization` wraps `_optimize` in
  `except (AgentExecutionError, ValueError, OSError)`, rolls back to the version the
  phase started from, logs `optimize_failed` and returns a well-formed `UNCHANGED`.
  Without it, an `AgentExecutionError` from the one LLM call escaped `asyncio.run`
  and a run that had already PASSed ended as a traceback with no `result.json` and
  no `report.md`.
- **`run_orchestration` catches `OSError` too, with a distinct reason.**
  `orchestrator.py:250` calls `state.current_netlist_texts()` at the head of every
  outer iteration, and that is `open(path).read()`. Reproduced: roll back to v0,
  let the file vanish, and the next head read raised `FileNotFoundError` past both
  excepts. "The netlist-apply path failed" and "the run could not read its own
  deck" send the next reader to different places. **The handler must not call
  `state.log_event`** — the same broken disk would fault the handler.
  `push_netlist_version` sits *before* the `try` on purpose: the guard's whole
  point is to still write artifacts into that directory.
- **The margin allowance is measured, not guessed.** Each criterion's allowance is
  `|worst corner − nominal|` read off the entry corner sweep. The `guard_band`
  ratio only fills criteria the sweep produced no value for — and it has to,
  because the consumer reads a missing name as allowance `0.0`, i.e. no guard at
  all on exactly the criteria whose corner behaviour is unknown.
- **The guard band is `T ± g·|T|`, never `T·(1±g)`.** The latter inverts on a
  negative threshold (`psrr_dc <= -25` with `g=0.2` becomes `<= -20`, *looser*).
  Each criterion is judged against its own threshold, so a two-sided window keeps
  both sides — `pvt.py` lost one side of exactly that shape twice.
- **The ratio fallback alone is not a usable guard on a real spec.** On
  `benchmarks/bandgap`, `g=0.2` demands `vbgout_v >= 1.44` *and* `<= 1.024` — an
  empty interval the 1.2389 V baseline already violates. `spec_pvt.yaml`'s sweep
  replaces that 0.24 with a measured 0.0051 and the same search then accepts ten
  steps. Both outcomes are pinned in
  `tests/unit/test_optimizer_bandgap_ngspice.py`. The **area phase's** F1 floor
  reproduces the same empty interval at `f ∈ {0.05, 0.10, 0.20}` on the same deck,
  and that is an **arithmetic identity, not independent corroboration** —
  `_margin_floor_allowances` returns `ratio_allowances` for `f1`, the same function
  the objective phase calls, with the same arguments. The genuinely new fact is the
  finer grid: `f=0.02` is baseline-feasible and so is `f ≈ 0.03` — the global
  minimum crossing is `vbgout_max` at **0.032130** — so the locked grid left
  `(0.02, 0.03213)` **unsampled**. Say "unsampled gap", never "F1 has no feasible
  middle"; the grid was not widened because the pre-registration forbids it.
- **The guard band can be infeasible at the baseline**, and the condition is not
  "`pvt_corners` is absent" — the measured path reaches it whenever nominal is
  worse than every corner for some criterion. `guard_band_violations` runs on the
  baseline and is logged **unconditionally** as `optimize_guard_infeasible` (plus
  `result["guard_infeasible"]`), and deliberately does not early-return: a step can
  push the violating criterion back inside, and the cost ceiling is already one
  simulation per candidate.
- **On a failed confirmation the loop bisects the accepted versions**, it does not
  re-search with a bigger guard. Re-searching is a retry with a larger guess and no
  cost ceiling; bisection is bounded (`ceil(log2 n)` sweeps), directed, and lands
  on a version whose sweep was observed to pass — worst case the anchor.
- **A guard band measured at the starting point does not hold once the circuit has
  moved, and the first real run proved it.** On bandgap the nominal search accepted
  10 steps on `TRIMAMP.Xt.W` (8 → 2.78943, `iq_ua` 212.99 → 211.68 µA) and the
  confirmation sweep failed **six** criteria — draining that tail widens the very
  corner spread the allowance was read from. Bisection landed on v4 (W=5.2488,
  212.25 µA, corner-confirmed): **4 of 10 steps survived.** A nominal-only
  optimizer would have shipped a design that fails at corners.
- **`check_stimulus_untouched` is a prerequisite of this phase, not a reuse.** The
  cheapest way to cut quiescent current is to lower a supply. All four addressing
  gates run on the optimization path, on the full deck rather than the folded
  prompt view. **They do not close the degenerate-answer surface.** The stimulus
  gate covers top-level `V`/`I` only, because that is what a *fact* can decide.
  `two_stage_opamp/netlist.cir` also has top-level `Lfb`, `Cin` and `Cload` — pure
  testbench apparatus in the tunable index and reachable by the optimizer;
  shrinking `Cload` improves phase margin and UGBW without touching the DUT.
  Widening the gate to "top-level passive" is **not** the fix — on
  `benchmarks/inverting_amp` the top-level `Rin`/`Rf`/`Eopamp` **are** the circuit.
  It is handled where a judgement call belongs: a paragraph in
  `OPTIMIZER_SYSTEM_PROMPT`.
- **Area is derived, the objective is measured — except on the area phase, where
  the objective *is* the derived area.** `_objective_value` returns `derived_area`
  when `phase.objective is AREA_OBJECTIVE` (`optimizer.py:46-52`), which is also
  why `AREA_PHASE.area_budget` is structurally `None`: `accept_step`'s "objective
  must fall" requirement already makes area monotonically decrease on that phase,
  so a separate ratio ceiling could never bind — turning one on would be a check
  that cannot fail, the same shape this repo already forbids elsewhere. On the
  **objective phase** the distinction in this bullet's title holds as written:
  `area.total_area` sums `w × l × m` over resolvable devices (`m` multiplies area,
  `nf` does not), so an over-budget candidate is discarded before it spends a
  simulation. Two things it is not: it sums over subckt **definitions**, so a
  definition instantiated N times is counted once
  (`structure.blocks[path].instance_count` is what a weighted
  version would read); and the budget compares `area / area_before`, so
  **`area_before == 0` disables it entirely** — reachable on a wrapper-cell deck
  where `build_param_envs` drops every disagreed name (`test_area_total.py` pins
  `counted == 0, skipped == 2`). That silence is now recorded:
  `AreaTotal.counted`/`skipped`, the enforced flag and a reason go into
  `optimize_baseline` and `result["area_coverage"]`.
- **`result["final_criteria"]` must describe the version bisection landed on.**
  `_search` stores each version's `evaluate_criteria` verdict in `records`, and
  `report.md` carries an Optimization section (objective/area before→after, steps,
  corner confirmation, and the guard-infeasible / area-coverage / phase-failure
  reasons).
- **`report.md` must draw the sweep that decides the verdict, including when it
  passes.** Re-rendering `runs/pvt_sonnet_1/result.json` with the old code printed
  `**Status:** FAIL` above seven criteria all marked `[PASS]`, with zero
  occurrences of "corner", while the same file's `pvt_sweep` failed all seven
  (`dc_gain` 71.09 → **3.14 dB** at `fs/1.98/125.0`). The
  silence-means-did-not-run rule applies to the **key's absence**, never to the
  value.
- **"Final criteria" must say what it measured.** Four provenances are possible:
  the mid loop on one unrendered deck point; the mid loop on the worst value across
  the reduced corner set; the version the **area phase** landed on
  (`result["area_optimization"]["final_criteria"]`, wired to the top-level key at
  `cli.py:910-911`); or the version the **objective phase** bisection landed on
  (`cli.py:945-946` overwrites the key last, so when both phases moved the deck the
  objective phase wins — it runs after the area phase). The heading carries both
  axes, derived from `corner_reduction.active` and `optimization.final_criteria`
  **plus `area_optimization.final_criteria`** (`report.py`'s
  `_final_criteria_provenance`, added when the area phase shipped). **Since the LLM
  `judge` was removed all four are judged by `evaluate_criteria`, so the "who
  judged" axis no longer discriminates and the *deck* axis is the whole signal** —
  a test asserting only that the heading names `evaluate_criteria` passes on all
  four and pins nothing. A `worst_case_corners` entry whose `value` is
  `None` is **not an argmax** — it is `missing_corners[0]` — so the report says "no
  measurement at corner X".
- **`result.json` and `history.jsonl` were not RFC 8259 JSON, and the danger was
  silent value corruption.** Both wrote bare `NaN`/`-Infinity` on the *normal* path
  (`evaluate_criteria` puts `math.nan` in `actual`/`margin` for a missing
  measurement; `corner_severity` returns `-math.inf`).
  `runs/pvt_sonnet_1/result.json` carries 8 literal `NaN`s; node's `JSON.parse`
  rejects the file and **`jq` 1.7.1 rewrites `NaN` to `null` and `-Infinity` to
  `-1.797e308`**, handing a consumer a number no simulation produced.
  `analogcoder/json_io.py` is the one place: `json_safe` (string markers) +
  `allow_nan=False`, **in that order** — `allow_nan=False` alone raises inside
  `write_result_json` and takes `write_report_md` down with it. The marker is a
  **wire format**, so `history.read_events` restores it with `restore_non_finite`.
  Known limits: `checkpoint.py` still serialises a `judge_result` the same way, and
  a genuine string whose value is exactly `"NaN"` would be restored as a float.

- **`result["pareto_front"]` collects every point the two phases already measured,
  and it costs zero extra simulations** (`pareto.py`, 2026-08-05). Three sources:
  the tuning loop's landing point (`entry`), the area phase's accepted points
  (`area`), the objective phase's (`objective`). **Nobody re-simulates** — the
  phases already store `{version → area, objective, criteria}` in `_search`'s
  `records`, and `_result` now exposes it as `accepted_points`.
  - **The entry point is always row one.** Curation's "the incumbent's own point
    was excluded from the comparison" is one of the 12 silently-inert gates; doing
    nothing can be the best answer, and a person cannot choose an option that is
    not in the table.
  - **Dominated points are kept and labelled, not dropped.** A front is a
    non-dominated set, but deleting the dominated rows destroys evidence — and the
    first row it would delete is usually the entry.
  - **The shipped point is the minimum area across all three sources**, not the
    area phase's result: the objective phase can shrink a device and lower current
    *and* area together, so pinning the shipped point to the area phase throws away
    a smaller point already in hand. When no point has a measurable area, the entry
    ships and `shipped_reason` says `area_unmeasurable` — "chose the minimum" and
    "could not choose" are different facts.
  - **Corner verification is on the shipped point only.** Confirming the whole
    front is a 45-corner sweep × N. Every other row renders as "코너에서 확인되지
    않음", which is all this run can say about it.
  - **The area phase's `objective` is NOT a second axis, and real data caught
    this.** On that phase `objective` *is* the derived area
    (`optimizer._objective_value` returns `derived_area` for `AREA_OBJECTIVE`), so
    feeding it to the objective axis builds a fake two-axis front whose axes carry
    identical numbers — measured on `two_stage_opamp/spec.yaml`, area and objective
    both `2.370369e-10`. `single_axis` came out `False` and the report would have
    drawn a table where it owes the sentence "축이 하나여서 공선이 아니다".
  - Dominance reuses `curation`'s `_is_better`/`_at_least_as_good` and therefore
    `COMPARISON_REL_TOLERANCE = 1e-3` — **not a second definition of the same
    band**. A point with an unmeasurable axis dominates nothing (reading `None` as
    "equal" would make the unmeasurable point the dominator).

### Corner reduction and re-entry

`corner_selection.py` / `corner_sim.py` — the tuning loop no longer simulates one
nominal point. `seed_from_sweep` takes the entry corner sweep and seeds a
`CornerSet` with the union of each criterion's worst-case corner;
`corner_sim.build_corner_simulate` wraps the existing `simulate_fn` contract so
`judge` sees the worst value across that set plus `corner_worst`/`probe`; and
`cli.py` re-enters the whole loop when the final sweep fails, having first grown
the set with the corners that failed. Declared per spec via
`corner_reduction: {enabled, retry_budget, probe}`; an absent block means today's
behaviour, and *why* it is off is logged (`corner_reduction_inactive`).

- **The reduced set is always optimistic, and that is the locked constraint.** A
  mid-loop FAIL is real — a corner is a corner whether or not you looked at the
  other 40. A mid-loop PASS can be wrong. The full sweep still runs before anything
  is reported, so the only cost of a wrong PASS is an iteration, never a wrong
  verdict. Every decision here defends that direction. **That rule is this repo's
  own convention, not an external requirement**, and it was written when a full
  sweep cost 286 s.
- **Nominal is the deck itself, not a name.** `corner_selection.NOMINAL is None`
  and `_run_point` simulates the file on disk unrendered. `tt/27` is a real corner
  and is *not* nominal — rendering through `tt` rewrites the include and injects a
  `.temp`. **`corner_fields` writes `{"corner_id": None}` — an absent identity, not
  the string `"(deck)"`**; putting a *name* where a coordinate goes leaves a reader
  seeing `"(deck)"` in the slot `ss` sits in. The human-readable `"(deck)"` is
  `corner_selection.raw_label`'s job, and that function owns the rule — do not
  re-derive the dict shape at a call site. `_as_point` **rejects** that shape,
  detected by the *absence* of coordinates, never by matching the string; without
  the rejection a `CornerPoint(process="(deck)")` reaches `render_corner_netlist`,
  which writes an include naming a file that does not exist.
- **A rewrite nobody counted is a corner nobody ran.** `render_corner_netlist`'s
  supply rewrite required a literal `DC` token, and
  `benchmarks/bandgap/netlist_startup.cir` writes `Vdd vdd 0 PWL(...)` — the ramp
  *is* that testbench's reason to exist. Zero matches, and `re.sub` returns its
  input unchanged. On the 45-corner grid that testbench saw **15 distinct
  conditions, not 45**. Measured after the fix at `tt/27`: **5.836 µs at 1.62 V vs
  87.03 ns at 1.98 V**, a 67× spread the sweep had collapsed to one number; and
  real `sf/1.62/-40` is **9.751 µs** while what ran under that label was 5.140 µs.
  The spec's own comment quotes `9.75us`, i.e. the threshold was set from a number
  the automated sweep could never reproduce. Still inside the 20 µs threshold, so
  the verdict does not move. **Any `startup_time` corner number quoted from a run
  before 2026-07-29 is a 1.8 V number wearing another voltage's label.**
- **The corner renderer reports what it did and refuses what it cannot do.**
  `render_corner_report` returns `CornerRender(text, states)` and a call site that
  passes a logger gets a `corner_render` event **once per testbench,
  unconditionally** — per corner would be 45 identical lines; only-on-failure would
  make "checked, fine" and "the check is gone" identical. Three states: `applied`;
  `absent` (nothing in this deck to touch — not an error); and, when the line **is**
  there in a form that cannot be rewritten without guessing, a raised
  `CornerRenderError`. It subclasses **`ValueError` on purpose** so the existing
  guards fold it into a clean FAIL / `optimize_failed`. One gap is deliberate:
  `cli.py`'s final sweep is not inside either guard, so an unrenderable deck
  crashes there — reaching it requires a deck no benchmark has, and the alternative
  is the silent wrong verdict this entry is about.
- **The PWL supply rewrite is two SPICE facts plus one stated judgement.** Facts: a
  PWL value list is alternating `t1 v1 t2 v2 …` so voltages are the odd indices;
  and after the last time point a PWL holds its last value forever, so that value
  is the settled level. Judgement: **every voltage entry numerically equal to that
  settled level moves to the corner voltage, every other entry is waveform shape
  and is left alone** — which keeps a startup ramp starting at 0 V. Comparison goes
  through `netlist.parse_spice_value`, so `1.8` and `1800m` are one level. Refused
  with `CornerRenderError`, never half-handled: an odd token count, any non-numeric
  token, the bare unparenthesised PWL, a `+`-continued line, and a settled level of
  `0`. The `DC` branch is byte-for-byte what it was — verified by hashing every
  render of all eleven benchmark decks at three corners: **31 of 33 identical**, the
  two that moved being `netlist_startup.cir` at 1.62 V and 1.98 V.
- **Never put a NaN in a `CornerPoint`.** It is a frozen dataclass, so it compares
  and hashes by field, and `NaN != NaN` — such a value is not equal to *itself*.
  Every set operation then breaks silently: `point not in cs.corners` is true for a
  corner already there, so `grown_with` re-adds it and the duplicate check turns a
  diagnosis into a `ValueError`. Nothing constructs one today, so this is a rule for
  whoever first derives a corner from a *measurement*.
- **The probe does not vote. It only promotes.** Each iteration also simulates one
  corner from outside the set (`next_probe`, rotating in ascending severity), and if
  it fails, `promote` moves it into the set permanently. Its measurements are
  deliberately **not** merged into the judged worst case — mixing them would destroy
  the optimism argument, since a probe result is one sample of a rotation. A probe
  that crashes is recorded (`error` in `corner_probe`) and judges nothing; the
  rotation is committed in a `finally` so a raised judge-path exception cannot pin
  the box on one probe corner forever.
- **A verdict failure that can add no new corner is a path disagreement, and it is
  not retried.** `cli.py` reports `corner_path_disagreement` instead of looping.
  **Two real channels, down from three**: the mid loop uses the control block the
  simulator agent converged on while the sweep uses the spec's text; and —
  **deterministic and always present** — on any criterion sharing a measurement name
  with another, the mid loop is structurally blind to one side. **Check that one
  first.** The third channel ("the mid loop's judge is an LLM while the sweep calls
  `evaluate_criteria`") **is gone**: both sides now call `evaluate_criteria`, so a
  disagreement can no longer be an LLM's reading. A failing criterion with no worst
  corner at all is a *different* fact (`corner_unattributed_failure`).
- **A two-sided window shares one judge slot, so the slot is resolved rather than
  overwritten.** `worst_case_measurements` is keyed by *measurement* name, so
  `vbgout_min` (`>=`) and `vbgout_max` (`<=`) cannot both be represented. Writing
  per criterion let the later-declared one win: the mid loop handed the judge the
  ss/1.62 **maximum** while `vbgout_min` should have been judged against 1.233753 at
  ff/1.98. This is the **third** time `pvt.py` lost one side of a two-sided window.
  The cost was the whole `retry_budget` — the loop **cannot converge on the blind
  half**, it re-derives the same PASS until the budget is gone.
  - The fix keeps the judge's contract and fabricates nothing: candidates are
    accumulated per measurement name and the collision is resolved by **preferring a
    value that violates one of the criteria sharing that name**, falling back to
    last-writer when all pass. Every candidate is a real measurement at a real
    corner, and because every candidate lies in `[min, max]` while a threshold
    comparison is monotone, substituting one criterion's worst case for another's
    **can only reveal a violation, never invent one**. So a mid-loop FAIL is still
    genuine and a mid-loop PASS still merely optimistic. The corner-less
    single-criterion case takes the same code path with one entry and is unchanged.
    `judge`, `evaluate_criteria`, `guard_band_violations`, `optimizer._search` and
    `run_full_pvt_sweep` are all untouched — the last reads
    `combined_worst_corners`, not `measurements`, and evaluates one criterion at a
    time against its own worst value, which is where the trap was already solved 70
    lines away. Pinned by
    `test_a_violated_side_of_a_two_sided_window_wins_the_shared_measurement_slot`
    and `test_a_two_sided_window_with_both_sides_passing_keeps_the_last_writer`.
- **A topology swap moves the circuit far more than a parameter step, and the corner
  set is not re-seeded afterwards.** The locked invariant still holds, so this costs
  **relevance, never correctness**. Recorded so nobody "fixes" it by re-seeding
  mid-run, which would spend a full sweep to buy nothing.
- **The allowance baseline is whatever the search actually measures.**
  `judge_tools.corner_allowances`' first argument is `reference`, not `nominal`, and
  `optimizer.py` passes `baseline_measurements`. Re-measuring nominal separately
  would count the same corner spread twice.
- **Where this pays is decided by criteria count, not corner count, and the repo's
  benchmarks are mostly outside that regime.** The seed is bounded by
  `min(#criteria, #corners)`, so a small grid saturates: a process-only 3-corner
  grid seeded all 3 and reduced nothing. Cost is
  `#testbenches × ceil(points_per_tb / workers)`, so reducing corners buys nothing
  once `points_per_tb` fits one wave. Measured at 9 workers: `spec_pvt` 45×22 →
  seed 9, 11 pts/tb, 10 waves; `spec_corner_reduction` 9×22 → seed 6, 8 pts/tb,
  5 waves (nothing to buy); `two_stage_opamp/spec_pvt` 45×**7** → seed 5, 7 pts/tb,
  4 waves. Exactly one shipped spec is in the regime on this hardware, and **the
  boundary moves with `cpu_count`** — which is why the adoption metric is
  `points_per_tb`, not waves.
- **Measured on `spec_corner_reduction.yaml` (9 corners), and the numbers are
  unflattering.** Seed = 6 corners + the deck = 7 selected points, plus 1 probe =
  **8 simulated points out of a 9-corner grid, per testbench**. The loop is
  testbenches-outside, so 5 testbenches make it **40 direct simulations per
  iteration** (~250 s) against 5 before. The saving against the full grid is one
  point per testbench. **The follow-on claim here — that a 45-corner grid projects
  to ~125 direct sims per iteration, can cost more than the sweep it pre-empts, and
  needs a `max_corners` ceiling first — was measured and is wrong.** On
  `spec_corner_reduction_45.yaml` the seed is **10** corners → 12 points/testbench →
  **60 sims per iteration, 0.267× the 225-sim full sweep**. The ceiling is not a
  prerequisite on this deck and grid. What stays true is the other comparison: with
  reduction off the mid loop simulates one nominal point per testbench, so enabling
  it is **5 → 60, a 12× mid-loop cost**, bought against catching corner failures
  early. `2026-08-03-reduction45-precondition.md`.
  Re-entry fired **zero** times **on that 9-corner spec**. Of 22 criteria, 5
  drifted between entry and verdict sweep and **all 5 landed on corners already
  inside the set**. Pinned in `test_corner_reduction_bandgap_ngspice.py`.
  **Those 6 corners** — the 9-corner spec's seed, not the 45-corner slot's 10 —
  **do not cover all 22 criteria**: three two-sided windows mean `vbgout_min`,
  `vbg0_min` and `vbg1_min` are never judged in the mid loop no matter which
  corners are selected. Read it as covering 19 of 22.
  - **That benefit has never been measured, and no shipped spec can measure it.**
    The one 45-corner spec declaring reduction **passes all 45 corners at baseline**,
    so a run ends PASS at iteration 0 with the machinery never exercised. And of 20
    recorded `result.json`s, 7 have a final sweep, **5 of those already had reduction
    on**, leaving **n = 1** with it off (`pvt_sonnet_1`, which ended FAIL). Read that
    as condition-not-present, never as "reduction buys nothing" — D1's `0.000` is the
    same shape. Measuring it needs a seeded 45-corner slot; the screen of the three
    `spec_seed_*` variants against the 45-corner worst case (no new simulation) puts
    `spec_seed_buf0_droop` first — spread `19.9324 → 31.6032` (**1.59×**), and it is
    already verified localised and solvable.
  - **That slot was built and run, and the A/B was REJECTED — but the treatment arm
    was never once observed, so do not read the rejection as "reduction loses".**
    6 runs (`benchmarks/bandgap/spec_seed_buf0_droop_45.yaml`, reduction on/off × 3):
    **5 of 6 hit the pre-registered 40-minute cap**, leaving one observed OFF run and
    **zero** observed ON runs. The rule was applied literally — precondition held,
    both acceptance clauses failed, reject — and the cap came from a cost model that
    was **wrong by 4×** (measured: ~10 min per outer iteration, and this slot needs
    5–6 iterations off / more on, so **60–100 min per run**; LLM latency dominates,
    not SPICE). `2026-08-03-reduction45-benefit-results.md`.
  - **What that one observed run bought is the thing this repo had only projected:
    a nominal-only mid loop declaring PASS on a deck whose real worst corner misses
    by 75%.** `off_3` drove `vbg0_droop` to **12.5891** at nominal, called PASS, and
    the 45-corner sweep measured **26.2401** at `ss/1.62/-40` against `<= 15` — the
    nominal number was off by **2.08×**, with `orchestration_attempt.status == "PASS"`
    followed by `pvt_final_sweep.overall_pass == False` in the history. **The failure
    corner reduction exists to prevent is real and now reproduced end to end.** It is
    one observation: an existence proof, never a frequency. The area phase is ruled
    out as a confound, but not by the route the pre-registration assumed: it accepted
    **zero** steps (`status: UNCHANGED`, `corner_confirmed: false`, landing identical
    to the loop's `netlist_v2_*`), so the confirming sweep had no reason to run.
    `corner_confirmed: false` means "nothing to confirm", not "confirmation failed".
  - Recorded as a side observation and **not** used in the verdict: `on_3` was at
    `vbg0_droop` **14.4843** *at its worst corner* with all 22 criteria passing when
    the cap killed it. The treatment arm was solving the honest problem and was cut
    before its confirming sweep.
  - **Three defects in that pre-registration, all worth carrying forward.** (a) the
    cost model above; (b) `void` was hung on the precondition alone, so "precondition
    held but the treatment arm has zero observations" fell through to a *reject*
    whose plain meaning misdescribes what happened — a rule must be able to say "this
    could not be measured"; (c) the accuracy clause's second half ("fewer than OFF")
    **cannot fire**, because the precondition already guarantees `off_fail >= 1`, so
    `on_fail == 0` makes it vacuously true; and (d) the cost metric's own
    "final sweep(s)" term **cannot contribute** — the excluded area phase runs the
    same full grid on the same deck immediately before it, so the final sweep is
    entirely cache hits (measured on `off_3`: area entry sweep **0 hits / 225
    misses**, final sweep **225 hits / 0 misses**), while the 1.5× threshold was
    derived from *saving* that 225-sim sweep. Threshold and metric point at different
    quantities. Check at pre-registration time that every clause — and every term of
    every metric — has an input it can take a non-trivial value on.
  - **v2 re-ran it with those four fixed, observed both arms 2 vs 2, and it is
    REJECTED again — but on the *cost* clause, with the accuracy clause passing.**
    `2026-08-04-reduction45-benefit-v2-results.md`. Both preconditions held: OFF hit
    "mid-loop PASS then final sweep fails" in **2 of 2** observed runs (`vbg0_droop`
    22.9376 and 20.1424 against `<= 15`, plus `buf0_phase_margin` 78.7902 on one),
    ON in **0 of 2**. Cost: ON's median outer iterations / OFF's = **2.5** counting
    the dead runs, **1.571** excluding them — both over 1.5, so the verdict does not
    depend on how the dead runs are read. **Do not read this as "reduction is
    expensive"** — see the next bullet.
  - **The cost clause's own justification is falsified by the code, and that is the
    sixth pre-registration defect.** v2 chose outer iterations partly because "OFF's
    failure path (re-entry) is captured in the iteration count too". It is not:
    `cli.py:727` sets `retry_budget = … if reduction_active else 0` and `cli.py:1024`
    breaks on `not reduction_active`, so **re-entry exists only in the ON arm** — all
    four observed runs have `reentry_count = 0`. The metric therefore compares
    *iterations to a wrong answer* against *iterations to a right answer*: `off_2`
    stopped at 2 because it was **early and wrong**, not because it was cheap. Applied
    literally anyway (editing a metric after results is what D1 paid for), and the
    next pre-registration must either drive both arms to the same terminus
    (corner-confirmed PASS) or drop the cost clause.
  - **The verdict turns on a single iteration, and n=2 cannot carry that.** ON `[5,6]`
    → median 5.5 over OFF `[5,2]` → 3.5 gives 1.571 against a 1.5 threshold — a 5%
    gap on an axis whose grain is 1. One fewer ON iteration (`[4,6]` or `[5,5]`) gives
    1.4286 and **adopts**.
  - **The mechanism is now visible as a trajectory, not one data point.** The mid
    loop's `vbg0_droop` starts at **19.932 with reduction off and 31.603 with it on**
    — same deck, same moment; the first is nominal, the second the real 45-corner
    worst at `ss/1.62/-40`. OFF optimises against a number **1.59× too optimistic**
    and declares PASS the moment it crosses 15. v1's single `off_3` observation
    (2.08×) reproduced **2 of 2**.
  - **The fifth defect — "observed" not excluding a backend failure — was
    non-decisive here, and the instrumentation lesson is separate.** Wave 3 died in
    *both* arms at 69.66/69.68 s with `iterations_used: 0`, satisfying v2's literal
    definition of observed (no cap hit, both artifacts written). Both readings reject,
    so nothing moved. What did need fixing is attribution: the harness wrote no
    per-run stderr, and `RuntimeError: aclose(): asynchronous generator is already
    running` turns out to appear in the stderr of the **completed, PASSing `on_2`**
    too — it is an at-exit asyncgen warning, not a failure signal. **The discriminator
    is `orchestration_attempt.failure_reason`, which was in `history.jsonl` all
    along**; a first pass claiming "no error event was written" was wrong.
  - Side facts, not used in the verdict: seed is **9 corners → 11 points/testbench →
    55 sims per iteration** on this slot (not the `spec_corner_reduction_45.yaml`
    10/12/60 — different `vbg0_droop` threshold, not a contradiction), `covered 22/22`,
    `dropped []`; probe promoted 6 and 8 corners. **The area phase makes wall clock
    non-comparable**: OFF ran `UNCHANGED 0/0` both times (a deck failing at corners
    lets `accept_step` accept nothing) while ON accepted 14/6 and 16/4 and ran its
    confirming sweep — so much of ON's 90–100 min is work OFF never does.
- **Re-entry is not reachable in the argmax regime on this benchmark, and that
  asymmetry is the useful fact.** Growth requires a *failing* criterion whose argmax
  sits outside the set — and if it is inside, the mid loop would have failed there
  first. `scripts/reentry_feasibility.py` swept 11 perturbation shapes over every
  (entry, verdict) deck pair: **9 of 242** fire on the 9-corner grid, and they are
  **argmax 0, coverage 9**. Projected onto 45 corners: **43 of 242, argmax 21,
  coverage 22** — the seed tracks *criteria* while the grid does not, so 36 corners
  sit outside. Read it as: enable reduction on a 45-corner grid and re-entry stops
  being dead code. That row was labelled "a projection, not a measurement (neither
  45-corner spec declares `corner_reduction:`)"; **`spec_corner_reduction_45.yaml`
  now declares it, and the label was misleading anyway** —
  `reentry_feasibility.py` calls `seed_from_sweep`, which never reads the
  `corner_reduction` block, so re-running it on the declaring spec reproduces
  43/242 exactly. What the declaration changed is that a *production* loop can now
  reach the regime; the script's number was always the script's own output.
  **Firing needs all three conditions, and "the mid loop exits PASS" is a property
  of the run, not of one criterion** — a first pass checked only the third
  (29 firings), a second checked one per criterion (25), and only the global form
  gives 9. The per-criterion form was once reported here as "the claim has been
  measured false"; that was wrong and a whole-branch review caught it. **Check the
  condition the *system* branches on, not a per-item proxy.**
- **ε-근접 피복 was REJECTED by its own pre-registered rule on 2026-07-30**, and the
  mechanism is measured, correct and **not adopted**. The rule required zero missed
  violations; broadening the perturbation from one shape to eleven produced a
  counter-example on the first try (`cc_trim_20`, ε=0.03 misses
  `trim_phase_margin`). Two facts the rejection turns on:
  - **ε is a property of (deck × grid), not of the deck.** Same deck, same
    perturbation: 45 corners tolerate ε=0.03, 9 corners do not. A sparse grid's
    ε-neighbourhood pulls in *far* corners. The largest ε holding 0 missed on the
    9-corner grid is **0.01**.
  - **The value 0.03 was derived from a single perturbation axis**, and that
    derivation is what got falsified. **A tolerance derived from one perturbation
    axis is not a tolerance** — same error class as D1's `0.000`, except the missing
    condition is *perturbation diversity*, not run count.
    `scripts/perturbations.py` owns the shape list so the two feasibility scripts
    cannot drift.
  Reviving it needs a new pre-registration, ε derived per (deck × grid) from several
  axes, and the probe-promotion fix. Full numbers:
  `2026-07-29-theory-combination-results.md` §9. Mechanism, for whoever revives it:
  declared as `corner_reduction.coverage: {epsilon, tau}`; corner `c` covers
  criterion `j` iff `|value_j(c) − worst_j| ≤ ε·|worst_j|`; absent block ⇒ argmax
  union, and the selected `CornerSet` is **byte-identical** (verified over 603
  cases, zero mismatches). **ε and τ have no code-side default** — they are derived
  per deck, so moving to another deck is a re-declaration. `k` is derived from `τ`;
  there is deliberately no `max_corners` integer cap.
  - **`scale = abs(worst)` with no fallback**: a criterion whose worst is `0.0` is
    covered only by an exact tie. An `or 1.0` fallback silently turns ε from a
    relative into an absolute tolerance via a constant derived from nothing. Failing
    closed only ever grows the seed. **A corner with no measurement is never
    approximated.**
  - The reasoning that still holds: **a violation is a band, not a knife edge**, so
    dropping the exact argmax rarely loses the violation. The first attempt to
    measure this was **void** and that is recorded: on the entry deck both specs
    pass everywhere, so "violations caught" could only be 0. The mid loop runs on
    decks the tuner *moved*; that is where the measurement belongs.
- **`seed_from_sweep` returns `(CornerSet, record)`** and `cli.py` logs it as
  `corner_seed` **unconditionally, including on the argmax path**. `dropped` answers
  "what does this look like when the gate does nothing?". `by_criterion` is
  **omitted** in coverage mode rather than emitted, because it is an argmax
  attribution and could name a corner not in the set. `points_per_tb` is the
  algorithm's metric; parallel-wave and wall-clock numbers are deployment facts tied
  to worker count and are deliberately not logged as if algorithmic.
- **`scripts/search_ab.py` refuses a non-`argmax` corner regime.** The harness calls
  `run_optimization` directly and never enters the reduced loop, so a `coverage:`
  regime mutated a field nothing in that call graph read **while the record named
  the regime**. It now refuses at the boundary and names what would have to change.
  **Consequence: the stage-1 × stage-3 factorial cannot be run until that harness
  drives the reduced loop.**
- **Best-arm identification was considered and rejected.** Pure-exploration bandits
  exist to spend a sampling budget well when each evaluation is *noisy*. SPICE is
  deterministic: one evaluation is that corner's exact value, there is no confidence
  interval to shrink, and re-sampling is pure waste. What remains is a covering
  problem over a known finite grid.
- **The union of argmaxes IS a covering problem — but a degenerate one.**
  `worst_case_corners` maps criterion → **one** corner, so the sets are disjoint,
  the coverage function is *modular* not merely submodular, greedy is exactly
  optimal, and **you cannot drop a corner without dropping every criterion it
  covered**. The pre-registered rule ("fewer simulations at the same coverage rate")
  had no satisfying case at all. Same shape as D1's `0.000`, caught before running.

### Deterministic netlist derivation, and what the tuner is shown

`structure.py` / `signal_path.py` / `patterns.py` / `control_block.py` /
`structure_view.py` — the deterministic replacement for what used to be an LLM
`analyzer` agent. That agent contributed nothing measurable: a run passed in 4
iterations on an analysis that was `{"circuit_type": "test", ...}`, and across runs
on one bandgap netlist it produced 93, 26 and 1 component roles. See
`2026-07-27-netlist-structure-derivation-design.md`.

- **The LLM `judge` agent went the same way, and the measurement is the whole
  argument.** Replaying every `judge` event in `runs/*/history.jsonl` — **58 events,
  742 criterion instances** — against `judge_tools.evaluate_criteria` gives **0
  differing `pass` flags and 0 differing `overall_pass`**. The verdict was never the
  LLM's contribution. What the LLM *did* add was **25 fabricated values**: when a
  simulation failed and `measurements` came back `{}`, it wrote `actual=0,
  margin=0`, while the deterministic path writes `NaN`. That is this repo's
  `null`/`NaN` distinction being destroyed on the path that decides the verdict —
  "not measured" rendered as a number that then gets compared to a threshold. So
  `cli.py`'s `judge_fn` calls `evaluate_criteria` directly. Three things that stay
  the same **on purpose**: the `judge` slot on `OrchestratorAgents` and the
  `state.log_event("judge", …)` event name (measurement scripts and `report.py` read
  that key); `judge_fn` stays `async` (the orchestrator awaits it); and
  `evaluate_criteria` itself is **untouched** — `pvt.py`, `optimizer.py` and
  `curation.py` already call it and ship its output, so "improving" `target` to
  carry a unit would be a regression in three artifacts. The visible consequence is
  that `target` loses its unit (`">=60.0"`, not `">=60.0 dB"`), which is the point:
  **the mid loop and the final sweep now emit the same string.** Pinned by
  `test_a_missing_measurement_is_judged_NaN_and_never_zero`.
- `structure.py` derives flat per-scope facts (inventory, device classes, the
  tunable `(refdes, param)` index). `signal_path.py` maps ports to nets across
  hierarchy and labels each net's drivers and sensors by *definition* name, since a
  definition is what the tuner can address.
- **A net's entry holds a set of roles per definition, not one winning role.**
  Collapsing to "drive wins" is right for a diode-connected device and wrong for a
  feedback amplifier — it printed `BUF_P … drives vbg0 senses -`, affirmatively
  denying the loop, and disabled `select_focus`'s reverse hop from exactly the
  blocks the design doc's worked example starts from.
- **`paths.roles_on(net)` drops the `drive` role only, on supply/stimulus nets.**
  The source drives that net, so no block can be its driver — `OPAMP2STAGE drives
  vdd,vss` was a false structural claim produced by two-terminal devices whose rail
  end is a `drive` terminal. A block *sensing* such a net is true and useful, so it
  stays. `paths.supply_nets` holds the nets a **top-level** `V`/`I` touches — a
  parsed fact, not the forbidden name guess.
- **`net_blocks` holds top-level nets only, and that bounds the reverse hop.**
  `walk` drops a net that stops being a port at an intermediate definition, because
  pushing it outward under its local name would attach roles to an unrelated
  same-named top-level net. Consequence: a seed block whose input comes from a net
  internal to a parent definition produces no upstream hop. On bandgap that is
  exactly `BUF_P` — its input `vt05` lives inside `BANDGAP`, so the design doc's
  `vbg0 → BUF_P → upstream` example does not reach the resistor ladder on that
  deck. The hop is correct and pinned synthetically; the deck offers no hop. What
  keeps `XRl1`/`XRl2` reachable is the tuner prompt saying a folded block may still
  be named.
- `patterns.py` matches differential pairs, current mirrors, **stacked pairs** and
  Miller compensation. `stacked_pair` is deliberately not called `cascode`: a
  cascode, a source follower over a current sink and a power-gating switch have an
  identical local subgraph, and telling them apart means reading net names. So the
  matcher states the connection and leaves the naming to the LLM, which has the
  netlist. It is the only one of the three modules that can be wrong, which is why
  it is separate.
- **A model-name substring marker must lose to the refdes prefix.**
  `_classify_model` now returns `None` unless `ctype == "X"`, mirroring
  `area_limits._classify_ctype` — an X-prefixed instance's positional value is a PDK
  primitive name, the one case where the prefix does not fix the device class. The
  gap was flagged as low-risk "because no benchmark exercises it"; the first real
  production deck did, three times: a MOSFET used as a MOS capacitor is written
  `m3 … UNITDEV_N_DEP_CAP …` — refdes `m`, model name containing `cap` — so
  `device_class="cap"` put it in both the cap list and the MOS list and the Miller
  matcher paired `m3` with itself. Confirmed zero change across all ten benchmark
  decks. Independently, `PatternMatch.__post_init__` now rejects any match whose
  `members` repeats a refdes. This is the mirror image of the bandgap case (a
  sky130 MOS cap's model name contains `pfet`, so it is invisible to a cap-name
  matcher): same substring rule, opposite naming convention, opposite failure.
- **The prompt is focused; the gates never are.** `structure_view.py` picks the
  blocks reachable from the failing criteria's nets (via `control_block.py`, which
  resolves a measurement name to the nets its `meas`/`let` lines observe) and
  renders every block at one line, focused blocks in full, and the netlist with
  unfocused `.subckt` bodies folded. `check_area_growth`,
  `check_refdes_resolution`, `check_param_applicability` and
  `check_stimulus_untouched` always read the whole deck, so a wrong focus costs
  relevance, never correctness. A proposal naming a block outside focus is logged
  as `focus_miss`.
- **The tuner prompt must never restate the focus as a restriction.** Saying "only
  propose changes to parameters listed under tunable" turns the layered view back
  into a filter and deletes the answer whenever focus is wrong — bandgap's
  `vbg0_min`/`vbg0_max` focus on `{BUF_P}` while the only fix (`XRl1`/`XRl2`) lives
  in the folded `BANDGAP` block. Measured corroboration: `ERRAMP` is in no seeded
  case's focus, yet a single knob in it passes the whole sweep on the first try.
- **Focusing a nested definition means focusing its ancestors too.**
  `render_netlist` folds a nested definition together with its parent, so a focus
  set holding `OUTER.INNER` but not `OUTER` elides the block it focused.
  `select_focus` runs every path that adds a scope through `_with_ancestors`. No
  benchmark deck nests subckts, so this is dead here and live on the first
  production one.
- **The testbench's own sources are not tunable, and it takes a gate to say so.** A
  top-level `V`/`I` is stimulus or supply by construction (refdes prefix plus
  top-level scope). `structure.py` omits them from the tunable index,
  `structure_view.py` renders them under `stimulus (not tunable):` so the omission
  is visible, and `netlist.check_stimulus_untouched` rejects a change to one; all
  three share `netlist.is_top_level_stimulus`. Not redundant with the address book:
  found by review, `{"refdes":"Vin","param":"value","new_value":"100"}` on
  `inverting_amp` passes area, refdes and param checks, rewrites the deck to
  `Vin in 0 AC 100`, lifts `gain_db` from ~20 dB to ~60 dB, and reports **PASS on an
  unmodified circuit** — `verify_post` has no reason to roll back a change that
  improved every criterion.
- **A tuning `param` must be able to apply.** `check_param_applicability` rejects
  `param="value"` when the positional token is a model or subckt name, and rejects a
  named parameter that appears neither on the component's own line nor on any
  same-model peer. The peer rule keeps `Xq1.m` reachable in bandgap — `Xq1` writes
  no `m=` but `Xq8` writes `m=8`, and `m` is the only knob that sets the
  emitter-area ratio. Without this gate a proposal like `param="width"` appends
  `width=55`, changes the netlist, does not change the device, and burns an
  iteration on a rollback nobody can explain.
- **A deterministic gate and the LLM prompt behind it must agree.** `verify_pre`'s
  prompt restates what the gates check — but the param gate was *widened* by the
  peer rule and the prompt was not, so `verify_pre` was instructed to reject exactly
  the `Xq1.m` case the gate exists to admit. Three such rejections set
  `verify_pre_rejected_any` and hard-FAIL the run, skipping topology escalation. The
  same shape appeared at the netlist view: `verify_pre` was handed a folded deck
  while told to reject anything absent from it, fixed by rendering its view with the
  focus extended to the blocks the proposal names.
- **The tuner is shown what it already tried in this run, as facts.**
  `attempt_log.py` keeps one `Attempt` per *component change* — not per proposal —
  because "which knob did what" is the only thing the tuner needs back. Each entry
  carries `(outer_iter, retry, refdes, param, old_value, new_value, outcome)` where
  `outcome` is `kept`/`rolled_back`/`rejected`, plus measured per-criterion `deltas`
  and `regressed` names for the first two, and `reason`/`detail` for the third. The
  last `ATTEMPT_RENDER_LIMIT = 30` are rendered; an empty list renders the **empty
  string**, because an empty table reads as "something happened". An `attempt_log`
  event is written **every retry, including when empty**. `deltas_between` drops any
  criterion missing from either side rather than reading an absent measurement as
  0.0.
- **The retry budget's headroom is instrumented because the branch that spends it
  has never fired.** Exhausting all three retries hard-FAILs the run when any one of
  them was a `verify_pre` rejection, and escalates to a topology swap otherwise —
  an **intended asymmetry** the topology path mirrors, and it is unchanged. What was
  missing is that a gate you cannot observe short of firing is unobservable at all:
  **0 of 15 recorded runs** ended `tuning proposal repeatedly rejected`, the worst
  iteration burned **2** of `MAX_TUNING_RETRIES = 3` (headroom **1**), and of the 8
  iterations that failed at all, **6 reached the last retry and 5 of those already
  carried a `verify_pre` rejection** — an r3 failure there would have been the hard
  FAIL, not the escalation. **Exhaustion is mixed**: gate rejections and `verify_pre`
  rejections consume the same three slots (`r1:area r2:verify_pre r3:OK` is real), so
  counting only `verify_pre` rejections understates how close the branch came — that
  error was made and corrected here. The `tuning_retries` event is therefore written
  **unconditionally, including when the first retry is approved**, so "spent no
  budget" and "the instrumentation is gone" differ, and headroom shrinking across
  runs is visible before the branch fires. `outcome` is
  `approved`/`exhausted_hard_fail`/`exhausted_escalate`, one per code branch.
  `failures` is `retry - 1` (each retry either breaks approved or continues failed),
  so the five rejection sites are untouched; `by_reason` counts distinct
  `(retry, reason)` pairs from `tuning_history` **filtered to this `outer_iter`**,
  because `_record_rejected` writes one `Attempt` per *change* and a plain count
  over-reports a multi-change proposal.
- **A rejection's reason code is recorded where it is decided, not re-parsed out of
  `history.jsonl`.** The five codes are `area`, `refdes`, `param`, `stimulus`,
  `verify_pre`. They cannot be recovered from the event stream: `area_check` and
  `refdes_check` both write their text under the same `feedback` key. A gate rejects
  the **whole** proposal, so every change gets the same code — which change tripped
  it is not something the gate reports.
- **`deltas`/`regressed` are measured once per proposal and stamped onto every
  change in it, so the render is a joint fact wearing a per-knob shape.** A 3-change
  proposal renders as three lines each ending with the same delta, and the only
  grouping signal is the shared `iter N.R` prefix. This is the shape this repo has
  paid for three times (F2's declared `addresses`, the zero-tolerance Pareto turning
  noise into a claim, and this). The fix is one clause in `TUNER_SYSTEM_PROMPT`,
  pinned by
  `test_the_tuner_prompt_says_lines_sharing_an_iteration_prefix_were_applied_together`.
  Splitting the measurement per knob is not available — one simulation measures one
  deck.
- **D1's claim was measured and is not supported; the mechanism it rests on is.**
  The paired probe ran 2026-07-29 — 75 pairs, 150 calls, 0 failed — and the
  pre-registered rule returns **no measured effect**: `R_exact` A 0.173 vs B 0.187,
  **p = 1.0**. Per the rule fixed before running, D1 is a feature spending prompt
  tokens for a benefit that did not appear — **explicitly not neutral**.
  **But the history does change behaviour, strongly, in a way the verdict metric
  does not see**: context-only `R_knob` went A 0.933 → B **0.653**, **p = 1.0e-4** —
  given the record, the tuner *leaves* a failed knob far more often. It was declared
  context-only **before** the run, so promoting it now is the move this repo
  forbids. Post-hoc, with no test attached: conditional on staying on the knob, B
  repeats the *value* more (`P(exact|knob)` 0.186 vs 0.286).
  Three things this does **not** licence: opening D2 (a suppression gate needs
  repeats shown to be a *cost*, and the metric cannot tell a deliberate knob walk
  from a rediscovery); the permission-sentence ablation (needs B significantly
  higher, and p = 1.0); and *removing* D1 (the measurement says "no benefit on this
  metric", not "no effect"). Full numbers:
  `2026-07-29-d1-remeasurement-results.md`; design:
  `2026-07-29-d1-remeasurement-design.md`.
  - **The design is why the answer exists at all**: a paired probe, not another pair
    of runs. It replays a recorded run's `history.jsonl` to each proposal point and
    calls the tuner **twice from the identical state**, differing in
    `tuning_history` alone, k=5 per point, McNemar exact. Timepoints are selected on
    `failed ≠ ∅`, which makes the first measurement's defect structurally
    unrepeatable. Do not read a partial sweep's verdict — the script computes one at
    any n.
  - **The superseded first measurement's "it went up" reading is withdrawn.** The
    metric counts a repeat only when that `(refdes, param)` already ended in a
    rollback or rejection, and the baseline run had **0 rollbacks, 0 gate rejections
    and 0 `verify_pre` rejections across 4 proposal events** — so `0.000` was the
    only value it could return. Two further limits: the metric keys on
    `(refdes, param)` and never on the proposed **value**, so a search walk on one
    knob counts identically to rediscovering a known-bad change; and the design
    document *defends* that walk. The one genuine rediscovery is the branch's most
    informative event: `OPAMP2STAGE.X6.W 8 -> 14` was `verify_pre`-rejected at
    iter1.r1 and re-proposed **byte-identically** at iter6.r1 with that rejection
    still inside the 30-entry window — **exactly the failure D1 was built to
    prevent, occurring with D1 active.** Verdict: **D2 is not opened**, on the
    ground that the measurement was uninformative. Do not remove the prompt's "You
    MAY propose the same component and parameter again" sentence on this evidence —
    the settled rule is that history is presented as facts, never as a restriction.
    Full numbers: `2026-07-28-tuning-attempt-record-measurement.md`.
  - **The original cross-run D was deferred (to D3)** because it would have carried
    between runs the fields one run was already throwing away: the within-run
    history held `{outer_iter, proposal, recommendation}` with **no measured
    values**, and a rejected attempt never survived its iteration.
    `2026-07-28-tuning-attempt-record-design.md`.

### Measurement apparatus (Stage 0), and the corners a spec declares

- `simulators/cache.py` / `simulators/parallel.py` — a simulation is a **pure
  function of `(deck text, control block, corner, simulator identity)`**, the same
  determinism premise the corner argument rests on, so it is content-addressed and
  cached. All four determinants are in the key: drop one and the cache
  **manufactures a fact**, which is worse than any inert gate. Hits and misses are
  logged, because "the cache never hit" and "there is no cache" must not look the
  same. Corner × testbench points are independent, so they run in a pool
  (`ANALOGCODER_SIM_WORKERS`, default `cpu_count-1`); the merge is order-independent
  and results are re-read in declaration order. Measured: the 45-corner ×
  5-testbench bandgap sweep went **286 s → 52.6 s** (225 sims at 0.234 s).
  **Every cost argument that multiplies 1.271 s/sim is quoting the pre-parallel
  number.**
- `checkpoint.py` / `history.py` — resume at **boundaries only** (outer iteration,
  corner-reduction attempt, entry to optimization). Mid-iteration resume would mean
  replaying LLM calls. The sharp edge is **not** the state snapshot but the event
  log: a crash leaves *partial* events, the resumed run writes the same kinds again,
  and two measurement scripts read `history.jsonl`. Do **not** truncate the log —
  destroying evidence is not the answer. The checkpoint records the line count, the
  `resume` event records the abandoned range, and `history.read_events` drops those
  ranges. `resumed_from` is **always** in `result.json` (`null` when not resumed) —
  a partial run entering a mean as if it were whole is half of why the first D1
  measurement was void.
- `json_io.py` is the one place that writes this repo's JSON. Checkpoints differ
  from `result.json` in one way that matters: they are **read back into a running
  run**, so a marker string reaching `judge_result` would `TypeError`.
- `control_block_gate.py` — the simulator agent may return a control block, and it
  is executed. The gate allows a fixed command vocabulary and requires every
  non-`.option` line to be preserved in order. **An allow list narrows the command
  vocabulary, not the argument surface**, and that gap was a live
  arbitrary-command-execution hole: `option`/`options` (non-dot) lines are the one
  class inside the allow list *and* excluded from the line-preservation comparison,
  and ngspice's `cp` shell performs backtick substitution on them. Demonstrated end
  to end — gate `accepted=True`, file created. Two layers close it: the allow list
  is dot-form only (what the prompt always said; cost zero, no shipped block
  contains an option line of any form), and shell substitution markers in the free
  surface are refused with their own reason code. Narrowing alone would rest on
  "today's ngspice does not substitute in dot form".
- **A spec declares corners as an *enumeration*, and axes are sugar.** Sign-off is a
  **human-picked set of N signature corners**, chosen by code outside this repo — a
  partial grid, which no axis declaration can express. A list can express a product
  by enumerating it; a product cannot express an arbitrary list. So
  `PVTCorners.corners` is the single truth, `__post_init__` expands an axis-only
  construction once, and `all_corners` is the **identity function**. Do not
  "optimize" it back into a product. When declared as an explicit list,
  `process`/`voltage`/`temperature` stay **empty** rather than being back-derived.
  With `M := N`, "the full sweep is the verdict" survives verbatim at scale.
- **Sign-off corners are opaque include files, not coordinates**, and the label-only
  half of that shipped 2026-07-29. `CornerPoint` is `corner_id` (required, derived
  from coordinates when there are any) + `payload` (absolute path to the file that
  realises the corner, **whose contents are never read**) + the three coordinates,
  optional and filled only by an axis declaration. The axis half stays deliberately
  undone until physical confirmation, because deriving axis identity from a filename
  would be the third instance of a mistake already shipped twice. `_as_point` and
  `raw_label` had to change in the **same commit**: fix one and every corner becomes
  `"(deck)"`, and `cli._argmax_drift` compares two label strings, so `moved_count`
  would be **permanently 0** — a metric that runs, never crashes, and reports a
  conclusion nobody measured.

### The composed deck model

`compose.py` / `spec.py`'s `compose:` block — a testbench can be declared as
**fragments** (`signal declaration + corner + netlist`) instead of one file,
because that is how the production flow builds its final deck.

- On that path corner rendering is **slot filling, not rewriting**:
  `render_corner_report`'s three regexes do not exist there — the corner file sets
  models, temperature and supply itself, and we place one `.include` naming it into
  the slot and **count** that (`corner_slot_filled`). The composed model removes the
  "recognise the rail by `vdd`" guess here.
- **Only fragments are versioned.** Composition happens just before simulation and
  the `tunable: true` fragment is `Testbench.netlist_path`, so `RunState`,
  checkpoint and `resolve_includes` consumers are untouched. Each fragment
  absolutizes its includes **against its own directory**.
- **The regexes leave and a quieter failure family arrives.** Every check came from
  a failure reproduced against real ngspice-46: a fragment whose first line is a
  statement is eaten as the deck **title** and vanishes (gain_db 19.999 → 100.0,
  zero warnings); directive-collision winner rules differ per directive
  (`.model`/`.option`/`.subckt` first wins, `.param`/`.temp` last wins, all silent)
  so **no safe fragment order exists** and the collision itself is refused; a
  relative `.include` resolves against cwd; a missing boundary newline after a
  comment absorbs the next fragment's first line; a `.ends` **name** mismatch is
  silent (a count mismatch is loud).
- **`records` counts what was checked; the counts are not the gate.**
  `corner_slot_filled == 1` says this path ran, not that 0 was reachable — a
  composed testbench with 0 or 2 slots is refused at the declaration. The counts
  exist so "composed, fine" and "the compose path is gone" differ.
- **Measured divergences from hand-copying `netlist.py`'s include rule**, all found
  by whole-branch review: `.inc` bypassed the absolute-path gate entirely while
  `includes_checked` logged `0`; `.lib <section>` … `.endl`, the definition form
  that names no file, was read as a path and falsely rejected; and
  `.param rf = 10k` (spaced, which ngspice accepts) made the collision key an
  **empty string**, so a real collision was missed while two spaced `.param`s with
  different names collided falsely.
- **A boundary written in one entry point is not written.** The tuning loop refuses
  a composed spec (`current_netlist_paths()` points at a single fragment — no
  stimulus, no corner, not a circuit; measured on the fragment view,
  `check_stimulus_untouched` approves a `Vin` change, `supply_nets` empties and
  `roles_on('vdd')` revives the `AMP drives vdd` false claim, and a `.option scale`
  on another fragment flips the area verdict). That refusal lived only in
  `cli._run`, so `analogcoder-curate` still opened the fragment. One sentence now,
  in `spec.refuse_composed_testbenches`.
- **A corner-rendering log that skips NOMINAL is wrong on this path.** `_run_point`
  composes for NOMINAL too, so a composed testbench with a NOMINAL-only set used to
  compose and log nothing.

### Theory adoption — what has been tried and rejected

`2026-07-29-theory-adoption-roadmap.md` is the plan; each stage is adopted only
against a **pre-registered** rule, and a rejected stage's negative result is
recorded rather than deleted.

- **Stage 1 (submodular max-coverage): rejected before starting** — the coverage
  sets are disjoint, so greedy is already exactly optimal (see the corner section).
- **Stage 2 (Plackett–Burman screening): rejected, 12/22.** PB stays a diagnostic
  and is never used to delete an axis.
- **Stage 3 (trust-region DFO / MADS, `mads.py`): rejected** by the pre-registered
  rule (corner-confirmed objective 212.2517 vs 212.4025), **but the negative result
  is far narrower than the rule's sentence.** Positive-direction power was ~0: the
  best improvement any searcher could show was 0.059%, below this repo's measured
  0.1% noise tolerance. Two of the three targeted weaknesses (coupling, mixed
  integer) could not fire at all — the ranking held one knob. And corner blindness
  is undecidable in that harness by construction. So **stage 4's precondition is
  left open** — do not read this as "search is not the bottleneck". Before the next
  verdict: pre-register a minimum effect size, use ≥2 knobs, and pick a
  configuration where recovery-chain density does not dominate the metric.
- **Coupling exists, so stage 4's precondition is met.** A 528-pair exhaustive scan
  on `two_stage_opamp`'s `ac_loop_gain` confirms coupling on the **31 DUT pairs**
  that carry zero bistability deviation (e.g. `X5.L × X7.L` on `ugbw_hz`, median
  `I_rel` 0.488). Read the claim on those 31, not on the 38 zero-deviation pairs —
  7 of those pair a top-level testbench element (`Lfb`/`Cload`), which is not a
  design variable. Two-stage design: a 3×3 screen then a 7×7 confirm taking the
  **median** of all interior 2×2 contrasts (a max lets one step through). Full
  numbers: `2026-07-30-knob-coupling-scan.md`.
  - **That scan ran on the pre-2026-08-04 deck, i.e. on a circuit with three DC
    solutions, and the whole "bistability deviation" filter exists because of it.**
    The qualitative conclusion survives — coupling was confirmed on pairs where the
    operating state provably did not move, and it was independently confirmed on
    bandgap. **The specific pairs and `I_rel` values do not**: they were measured on
    a bias chain that no longer exists. Re-run the scan before quoting a number from
    it, and note that the filter itself is now unnecessary.
- **Bandgap's coupling was then measured too, and it crosses block boundaries.**
  The scan above covers one deck, so a strategy tested only on bandgap would be
  tested where no coupling had been measured. On `amp_loops` (**not** `canonical`
  — see below) 66/66 stage-1 candidates advanced and **15/15 confirmed at stage 2,
  9 of them across blocks**: `ERRAMP.Xcc × BGR_CORE.Xcc` (×4),
  `TRIMAMP.Xcc × BANDGAP.XRl2` (×4), `TRIMAMP.Xcc × BGR_CORE.Xcc` (×1).
  `TRIMAMP.Xcc.L × W` moves 27.7° per axis but **66.0° jointly**. The prediction
  that four instances of the same block would couple weakly was **falsified**.
  Two limits are on the record: 51 pairs went untested (capped by stage-1 max
  `I_rel`, never alphabetically), and only 2 of bandgap's 5 testbenches were
  measured. `2026-08-02-bandgap-coupling-precondition.md`.
  - **Running that scan on `spec.canonical` could not have returned a different
    answer.** Bandgap's canonical is `dc_tc`, a DC temperature sweep, and 10 of the
    12 top-ranked knobs are compensation capacitors — inert there by construction
    (verified with 7 single-knob probes: six outputs byte-identical, with
    `BANDGAP.XRl2.w` as a moving control). The brief that specified `canonical` was
    the controller's, and the sub-agent caught it. **Choosing the testbench is
    choosing whether the metric can move.**
- **Stage 4a (compound steps, `_compound_fallback`): rejected**, tagged
  `single_deck`. `partners ∈ {1,3}` each failed **both** acceptance clauses
  independently, so the 1.0 %p effect size is not what decided it. The useful part
  is *why*, and it is arithmetic, not statistics: **the strategy proposes candidates
  whose objective cannot change.** `_next_value` moves a knob `×0.9` down and
  `/0.9` up, so pairing a device's `L` with its own `W` gives
  `0.9L × W/0.9 = LW` — **area exactly unchanged** (`0.9 × 17.2186` times
  `40 / 0.9` against `17.2186 × 40`, difference `0.0`; the equality holds at full
  precision only, and the digits the step event prints are truncated to 6 — that
  truncation is the residue below). And the partner is *usually* that same device:
  `rank_by_area_gain` gives `L` and `W` identical gain, so sorting puts them
  adjacent — **83 of 83 devices on slot A, no exceptions** — and `_try_partners`
  takes `knobs[i+1:i+1+partners]`. So when the lead is the **first** of its own
  `(L, W)` pair the partner is that pair's other half and the candidate is
  area-neutral; when the lead is the second, the partner is the *next* device
  (**2 of the 8 `partners=1` attempts**, both refused on a criterion, not on area).
  `accept_step` requires the objective to fall, so the 5 acceptances — **all of
  them same-device** — came from the proposal being written at **6 significant
  figures** (relative `2.7e-07`); the pair whose rounding went the other way was
  refused as *"not below the current best"*. **Acceptance was decided by rounding
  direction, not by search.** Slot A's apparent +0.855 %p win is not a search win
  either: its nominal landing (9.6274e-09) is *worse* than the control's
  (8.8898e-09), and it only led on the final number because the control's extra
  steps failed corner confirmation and bisection discarded six. **12** cross-device
  compound attempts fired (A/1 1, A/3 5, B/1 1, B/3 5) and **all 12 were rejected**,
  and none of them reached the block-crossing pairs above. Full numbers:
  `2026-08-02-compound-step-search-results.md`.
  - **The pre-registration's precondition counted acceptances without asking
    whether they moved the objective**, so it was satisfied by those five
    rounding-residue steps. Applied literally, as the rules require; recorded as
    the defect the next pre-registration must fix.
  - **Reparametrization (option a) was rejected on measurement, before this ran**:
    of the 38 valid confirmed pairs, 5 are same-device, 26 are cross-device and 7
    involve a top-level testbench element, so rewriting a device's `(w, l)` as
    `(area, aspect)` would decouple **5 of the 31 DUT pairs — 16%**.
  - Reopening needs a **new** pre-registration whose partner rule does not read the
    ranking's neighbours (the `w × l × m` symmetry guarantees that neighbour is the
    same device), which excludes same-device dimension pairs outright, and whose
    slot baselines are verified on **the grid the verdict uses**. The
    `compound_fallback_1`/`_3` entries stay in `SEARCH_STRATEGIES` — reachable only
    by an explicit `search_strategy`, never by a default path — so that
    pre-registration can re-run them without rebuilding the arm.
- **Repair-loop block ordering: layer 1 passed its rule but the pass carries almost
  no information.** A distance-based block *render order* (ordering only, never
  filtering) scored 2·2·2 against a baseline of 5·5·4 — yet **552 of 720 random
  orderings (76.7%) pass the same rule**, because today's baseline is an unordered
  focus set covering 4–5 of 6 blocks and all three perturbations make `TRIMAMP`
  sufficient. Layer 2 (LLM paired probe) is **not** run until its own precondition
  is checked: do the replayed run's proposals fall both inside and outside the
  sufficient set. Two by-products are more useful than the verdict: the perturbed
  site is **not** the site that must be fixed (a perturbed block came out `capped`
  while an unperturbed one was confirmed), and **today's focus folds a block that
  provably contains a fix**. Full numbers:
  `2026-07-30-task6-block-order-results.md`.

Design docs live in `docs/superpowers/specs/`, implementation plans in
`docs/superpowers/plans/`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Requires `ngspice` on PATH for the real-simulator tests and any actual run
(`brew install ngspice` on macOS).

## Running

```bash
.venv/bin/analogcoder --spec benchmarks/inverting_amp/spec.yaml --run-dir runs/r1
```

`spec.yaml` declares the testbenches, each with its own netlist file (resolved
relative to the spec file), control block and criteria — there is no separate
`--netlist` flag.

A second console script judges whether a candidate topology may join
`TOPOLOGY_LIBRARY`. It writes artifacts and never touches the library.

```bash
# extract a block from a verified deck
.venv/bin/analogcoder-curate --from-deck benchmarks/bandgap/netlist_loops.cir \
  --from-block BUF_P --slot-spec benchmarks/bandgap/spec_curate_slot.yaml \
  --slot-block BUF_P --id folded_cascode_pmos_in_cs --out-dir runs/curate1

# let an agent modify the incumbent block by technique name (provenance=authored,
# so this one must pass corner verification)
.venv/bin/analogcoder-curate --technique "cascode (indirect) compensation: ..." \
  --slot-spec benchmarks/bandgap/spec_curate_slot.yaml --slot-block TRIMAMP \
  --id folded_cascode_indirect_comp --out-dir runs/curate2
```

`--max-knobs` and `--knobs` are opt-in speed caps; by default every knob is swept.

Default backend is Claude (`--agent-backend claude` — uses whatever `claude` CLI
auth is configured, no env var needed). To run against a local OpenAI-compatible
server instead:

```bash
LOCAL_LLM_API_KEY=<token-or-dummy> .venv/bin/analogcoder \
  --spec ... --agent-backend openai-compatible \
  --llm-base-url http://localhost:11434/v1 --llm-model <model-name>
```

Verified against a real Ollama server (`qwen2.5:7b-instruct`) — full pipeline
including real tool calls, not just the no-tuning-needed happy path.
`tests/integration/test_local_llm_backend.py` is skip-gated on
`LOCAL_LLM_BASE_URL` and is the fastest way to re-verify that path. That
verification predates the LLM `analyze` agent's removal, so a re-run today has one
fewer LLM call in the chain — not a different pipeline.

On `two_stage_opamp`, Claude converges to PASS in 3 iterations (correctly
identifies that increasing `Cc` improves phase margin). Ollama ran the full
10-iteration budget and ended in a clean `FAIL` (`max iterations reached`), not a
crash — schema validation, refdes/param checks and rollback all worked. It failed
because the model's reasoning had the trade-off backwards: it repeatedly
*decreased* `Cc`. Every bad proposal was rolled back, so the run ends at the safe
baseline — a genuine model capability gap, not a pipeline defect.

## Benchmarks

- `benchmarks/inverting_amp/` — ideal op-amp (VCVS), single gain criterion, passes
  immediately. The golden-path smoke test.
- `benchmarks/two_stage_opamp/` — real transistor-level 2-stage CMOS op-amp
  (**sky130**, `.option scale=1.0u` plus `pdk_corner.inc`), three criteria (DC
  gain, UGBW, phase margin) with a genuine trade-off: increasing the Miller cap
  `Cc` improves phase margin but reduces UGBW. Starts with phase margin failing by
  design, so it exercises the tune → verify → re-simulate loop. See
  `2026-07-25-two-stage-opamp-benchmark-design.md`.
  - **`Cc` alone has never been able to pass `spec.yaml`, on either deck, and the
    2026-08-04 bias fix made it 9° less short.** Measured by sweeping `Xcc.w`=`l`:
    holding `ugbw >= 1.5 MHz` caps `Cc` at ≈15.8 before the fix (`phase_margin`
    ≈45.8°) and ≈20.2 after (≈55.0°), against a 60° threshold — the trade-off is
    monotone and clean in both, it just runs out. So any run that reaches PASS here
    does it with `Cc` **plus** at least one more knob (the recorded runs kept
    `Xcc` together with `X7.W`), and the benchmark got *easier*, not harder.
  - **Its bias chain had THREE DC solutions and was replaced on 2026-08-04. Any
    number quoted from this deck before that date carries an unidentified operating
    state.** The old chain was a self-biased beta-multiplier (`Xp4`/`Xn1`/`Xn2` +
    `Rdeg` 20k, `Rstart` 3Meg as start-up). Measured by counting zero crossings of
    the current a swept `nbias` source must supply — a count no solver path can
    bias — it had **exactly 3 self-consistent solutions at all 45 corners**, and the
    corner only decided *which one the solver landed on*: A `degn` 0.0119 V
    (~0.6 µA, 20 corners), B 0.0626 V (~3.1 µA, 11), C **0.85 V (~42.5 µA, 14)**, a
    latched-high state where `gain_db` collapses to 3–26 dB. `X6.W` 5.999999 →
    **6.0** → 6.000001 flipped `ugbw_hz` 2.07e6 → **2.70e7** → 2.07e6 on a 1e-6
    change, so **the tuner could flip the state mid-run with no record.**
    Diagnosis: `2026-08-03-tso-third-bias-state.md`.
    - **The replacement is a resistor+diode reference (`Rbias` 1Meg into the
      diode-connected `Xn2`), and the single solution is arithmetic, not luck**: the
      resistor's current falls monotonically in `V(nbias)` while the diode's rises,
      so there is exactly one crossing. Measured **1 crossing at all 45 corners** and
      at all 8 swept `X6.W` values, where 5.999999 / 6.0 / 6.000001 now give
      byte-identical `gain` 72.0156 / `ugbw` 3000750 / `pm` 33.4184.
    - **Direction was decided by theory before measurement, and the measurement
      agreed.** A self-biased loop determines itself, so its fixed points are
      multiple by construction — which is *why* a beta-multiplier needs a start-up
      element. Adding devices cannot make it single-solution. Measured accordingly:
      an anti-latch clamp gated on `degn` left 3/45 untouched at W=1 **and** W=4
      (the latch survives because `pbias` bottoms out, opening `Xp4` to ~240 µA —
      more than any clamp sinks), and weakening the PMOS mirror reached only 20/45.
    - **The clamp contributed nothing, and only a single-variable control showed
      it**: `clamp_rdeg_3k` and `rdeg_3k` have *identical* histograms, as do
      `pmos_l8` and `pmos_l8_clamp`. Ship two changes together and the effect gets
      attributed to the wrong one. Same control killed a second false attribution:
      `si_lowI` (narrow-long NMOS + `Rdeg` 200k) is 45/45, but so is `rdeg_200k`
      **alone** — the resizing was doing nothing.
    - **A single DC solution is necessary, not sufficient.** `Rdeg` ≥ 100k also gives
      1/45 and **destroys the amplifier**: `dc_gain` 34.9 dB at every corner and
      `psr_plus` **+13.9 dB (positive — it amplifies supply noise)**. Judge a bias
      fix on the amplifier, never on the solution count alone.
    - **The fix moved nothing that defines the benchmark and improved everything
      else**, which is why **no threshold was changed**: nominal `dc_gain`
      71.0861 → 71.4929 and `phase_margin` 34.5636 → 33.9776 (still failing by
      design), while corners with `dc_gain < 60` went **14/45 → 0/45**, `psr_plus`
      10 → **32/45**, `psr_minus` 34 → **42/45**, and **NaN 11 → 0**. The
      supply-independence loss was expected to cost PSRR and **measurably did not** —
      most old PSR failures were the latch. `2026-08-04-tso-bias-fix-results.md`.
    - **`TOPOLOGY_LIBRARY` carried the same bias block twice and had to change in the
      same commit.** `miller_basic` and `miller_nulling_resistor` both embedded the
      old chain, so a topology swap would have **reinstated the defect**. Fixing both
      also preserves `identical_body` (`miller_basic` ≡ `OPAMP2STAGE`, so the deck
      still offers 1 candidate, not 2).
  - `spec.yaml` also declares two PSR testbenches (`psr_plus`, `psr_minus`) with
    the AC stimulus moved to `Vdd`/`Vss`. Nominal baseline after the 2026-08-04 bias
    fix: `psr_plus_db=-12.7692` against `<=-10.0` and `psr_minus_db=-0.951585`
    against `<=0.0` — **both pass**; what `psr_minus` costs shows up at corners
    (32/45 and 42/45 respectively), because `M6`'s NMOS source sits directly on
    `vss` with no cascode. **The numbers this line used to carry (`-15.12` / `-3.36`
    "fails `<=-8dB`") were doubly stale**: pre-fix, and quoting a threshold no
    committed spec has — `spec.yaml` and `spec_pvt.yaml` both say `-10.0` / `0.0`.
    Increasing `M6.W` improves `psr_minus` but regresses
    `phase_margin` and, with an `M7.W` change, `psr_plus` too — the real motivation
    for verifying every testbench every iteration. See
    `2026-07-25-psr-verification-design.md`.
  - `spec_topology_required.yaml` raises `phase_margin` to **62°** (this line said
    65° until 2026-08-04; the file has always said 62.0), which no value of
    `Cc` alone reaches without failing UGBW — the motivation for topology-swap
    tuning. **Caveat, found by a real run:** it does not reliably force the swap
    for a strong model — a live Claude run solved it in 2 iterations via
    `Cc`+`M6.W` together, a combination outside the original Cc-only sweep. The
    mechanism is still verified correct; this benchmark just is not a guaranteed
    trigger. See `2026-07-25-topology-swap-tuning-design.md`.
    - **`miller_nulling_resistor`'s `Rz` is 160k, re-derived after the bias change,
      and the entry is now marginal here.** Optimal `Rz ≈ 1/gm6` tracks the bias
      current, so the old 220k gives `pm` **57.38° — under the 62° threshold**.
      The swept optimum is 160k at 62.34° / 4.05 MHz / 71.49 dB: it meets all three
      criteria with **0.34° of margin** on a flat peak (150k → 62.13, 170k → 62.20).
    - **`verified_at="corners"` on both miller entries was false from the day they
      shipped, and it is `"nominal"` now.** `agents/tuner.py` puts this field in the
      tuner prompt, so it is a claim, not a note. Measured across 45 corners:
      *before* the bias change (old bias, `Rz`=220k) `pm >= 62` held at 12/45 with
      **7 NaN** and a range of 3.98–132.12° — and 132° is not a good corner, it is
      the latched state where the amplifier became a follower. *After* (`Rz`=160k)
      it is 6/45 with **0 NaN** over 46.17–72.11°: **the count fell and the meaning
      appeared.** `miller_basic` is simpler still — it *is* the deck's body, and the
      deck has never passed `spec_pvt.yaml` at 45 corners (0/45). The two bandgap
      entries were **not** measured and were **not** touched: downgrading an
      unmeasured claim is the same kind of assertion as upgrading one.
  - `spec_search_slot.yaml` — `spec_pvt.yaml` byte-for-byte except `phase_margin`
    `60.0 → 30.0`, authored as a search-strategy validation slot (the same kind of
    thing `spec_curate_slot.yaml` is for curation). **It was abandoned as unusable
    and the 2026-08-04 bias fix revived it, with no threshold touched.** It used to
    pass all 7 criteria at nominal and fail all 7 across the 45-corner grid —
    `dc_gain` 3.13783 dB at `fs/1.98/125` against a nominal 71.0861, and **four
    criteria measuring `NaN`**, which no threshold can admit. That was the three-DC-
    solution bias, not the spec: after the fix the same file passes **25 of 45
    corners** with `dc_gain` and `phase_margin` at **45/45** and **zero NaN**.
    **The lesson that survives is the one that was right at the time** — lowering a
    threshold fixed the nominal failure and could not touch the corner one, because
    the corner failure was never a threshold problem. Do not lower thresholds to
    "fix" a slot; find out what the circuit is doing. Whether 25/45 makes this a
    *good* search-strategy slot is a separate question needing its own
    pre-registration.
- `benchmarks/bandgap/` — five-block Kuijk bandgap reference chain (`BGR_CORE` +
  `ERRAMP` → `TRIMAMP` → resistor ladder → `BUF_N`/`BUF_P`), producing `vbg1`=1.2V
  and `vbg0`=0.5V. Unlike every other benchmark this one is **multi-block**, and
  its purpose is to measure whether the tuner changes the *correct* block — which
  is what made subckt-scoped refdes a prerequisite. Every amplifier is a folded
  cascode plus a common-source output stage, which is also what lets
  `spec_pvt.yaml` sweep the full ±10% supply axis (a plain mirror load leaves a
  1.2V-common-mode pair no saturation margin at 1.62V: trim loop gain −45dB,
  `vbg1` = 1.084V). Uses `pnp_05v5` and `res_high_po`; every capacitor is an nfet
  or pfet MOS cap, never MiM. See
  `2026-07-26-bandgap-benchmark-and-scoped-refdes-design.md`.
  - `spec.yaml` passes everywhere; the four `spec_seed_*.yaml` criterion-tightening variants each
    tighten one criterion whose only fix lives in one subckt, verified both
    solvable and localised — growing `BUF_N.Xcl` does nothing for `vbg0_droop`,
    only `BUF_P.Xcl` does. `spec_seed_tc.yaml` is deliberately the coupled one:
    `Rp/R1` fixes TC but drags `vbgout` and `vbg1` with it.
  - `spec_seed_buf0_droop_45.yaml` — `spec_seed_buf0_droop.yaml` plus the 45-corner
    grid and a `corner_reduction:` block, and **nothing else** (the added blocks are
    byte-identical to `spec_corner_reduction_45.yaml`'s). It is the measurement slot
    for the 2026-08-03 reduction A/B, not a tuning benchmark: its 45-corner baseline
    fails **only** `vbg0_droop` (31.6032 at `ss/1.62/-40`), which is what makes both
    arms have work to do. **Do not quote an area or timing number from this slot as
    shipped-spec performance.**
  - `spec.yaml` and `spec_pvt.yaml` carry the `optimize:` block;
    `quiescent_current` sits at 212.99 µA against a 300 µA threshold on purpose.
    `spec_pvt.yaml` is the one that can optimize; the corner-less `spec.yaml` is
    pinned as the counter-case. The corner-anchored run costs **1790 s** (six
    45-corner sweeps, because the confirmation fails and bisection runs), which
    makes that test by far the longest in the suite.
  - **Until 2026-07-29 the `startup` testbench had no voltage axis at all** in
    `spec_pvt.yaml` and `spec_corner_reduction.yaml` both — see the corner-renderer
    entry above. Every other testbench in both specs carries a `DC` supply and
    their renders are byte-identical across the fix.
  - `spec_corner_reduction.yaml` — same testbenches and thresholds, a **9-corner**
    grid, a `corner_reduction:` block and **no** `optimize:` block (an optimizer
    search would make its runtime unpredictable). The 9-corner choice is measured,
    not arbitrary (a 3-corner grid seeded all 3).
    `spec_corner_coverage.yaml` is that file plus a `coverage:` block — the
    ε-coverage counterpart, deliberately the spec where ε buys no wall clock,
    because the question it answers is safety, not speed.
    `spec_corner_reduction_45.yaml` is the 45-corner grid with `corner_reduction:`
    and no `optimize:` — the first spec to declare reduction on that grid.
  - `netlist_seed_topology.cir` / `spec_seed_topology.yaml` — the seed that **only
    a topology swap fixes.** The deck is `netlist_loops.cir` with `BUF_P`'s body
    replaced verbatim by `BUF_N`'s (an NMOS-input fold where the complementary
    PMOS-input fold belongs) and `buf0_loop_gain` raised 60 → **90 dB**. The block
    it buffers sits at 0.4999 V, below an NMOS pair's reach: the tail current
    source is left with **10.1 mV** of Vds and `buf0_gain_db` collapses 100.16 →
    **73.52 dB**. Widening the input pair helps but only ~7 mV of tail headroom per
    doubling, reaching 83.45 dB at W=80 before W=150 aborts on sky130's 100 µm bin
    ceiling. So 90 dB sits strictly between what sizing reaches and the 100.16 dB
    the swap reaches. The seed is localised: on the DC testbench all 8 criteria
    still hold (`iq` 212.99 → 178.95 µA — the starved fold draws less). Pinned
    without any LLM in `test_topology_seed_ngspice.py`. **Do not "unify" the seed
    body with `folded_cascode_nmos_in_cs`** — that one came from `TRIMAMP` and
    differs in `Xcl` and the `Xcc`/`XRz` sizes, so the numbers above hold only for
    `BUF_N`'s body.
  - **Cascode (Ahuja/indirect) compensation was tried here and rejected, with
    data.** Moving the compensation cap to the cascode source node peaked at
    89.4° / 5.45 MHz on `TRIMAMP`, while Miller+`Rz` at the same cap area reaches
    **99.7° / 27.0 MHz** — better on both axes. The cause was not the technique but
    the shipped sizing: `TRIMAMP.XRz.l = 15` is badly under-set, and raising it to
    60 lifts phase margin 81° → 125° and UGBW 4.8 → 24.8 MHz together (it collapses
    again by 120, so the optimum is not monotone). Same shape as
    `spec_topology_required.yaml`'s caveat, opposite direction. See
    `2026-07-28-topology-applicability-design.md`.
  - `spec_curate_slot.yaml` is the **curation validation slot**: one testbench, the
    9-corner grid, 8 criteria across all four amps, 30 tunable knobs on `TRIMAMP`.
    It is the only spec against which an authored candidate can reach
    `verified_at="corners"`, and where the gate reproduced F1's hand judgement: the
    Ahuja body for `TRIMAMP` gives **REJECT**, dominated by the swept point
    `TRIMAMP.XRz.l = 25.98` at **99.90° vs 89.42°**, with `addresses` narrowed to
    `['trim_phase_margin']`. 120 simulations, 2 min 41 s.
  - **Bandgap is not contaminated by the multi-solution bias that affected
    `two_stage_opamp` (fixed 2026-08-04), and that was measured independently.** `scripts/dc_solution_uniqueness.py`
    pushes the bias chain's initial guess five ways — including explicitly to the
    off state — across four device sizes and **four of the six testbench decks**:
    all six probes come back identical every time. `netlist_seed_topology.cir` is
    one of the four, which matters, since its `BUF_P` body is a starved NMOS fold.
    Not a proof of uniqueness — five directions, not a dense size sweep. The
    control case is the useful half: shrink `BGR_CORE.Xsu_b` from `W=0.42` to `0.2`
    and **no DC solution comes out at all**; that row is recorded as **void**, not
    as agreement.
    - **Writing that script cost five silent failures with exit code 0 each time**,
      and they are why it exists in this shape: a control block after `.end` is
      ignored; one bad name in `print v(a) v(b)` discards the whole line;
      re-reading the injected `.nodeset` reports your own value as a measurement;
      **`op` on a deck whose supply is a time-dependent source measures a state the
      circuit never uses** (the PWL ramp is 0 V at `t=0`, and the script reported
      "multiple solutions" across four rows there — a result that did not even
      reproduce when the deck was run alone); and a relative deck path left
      includes unresolved, so every row came back **void** with wording identical
      to the genuine control case. The fourth and fifth produce a *wrong* answer
      rather than none, which is why they are the dangerous shapes. The refusal for
      the time-dependent case is keyed on the parsed fact and deliberately does
      **not** try to tell supply from stimulus — that needs recognising the rail —
      so it fails closed and costs one valid measurement, recorded in a **refused**
      column distinct from *void*.

## Gotchas found by running, not by inspection

### Weak (local) models and the agent loop

- **`response_format` + `tools` together breaks tool-calling on some
  OpenAI-compatible servers** (observed on Ollama): the model skips calling the
  tool and fabricates schema-shaped output. `OpenAICompatibleBackend` only sends
  `response_format` on turns where no tools are offered — do not "fix" this.
- **The tuner needs the actual current netlist**, not just structural facts — it
  cannot compute a concrete new value otherwise.
- **`param` must be exactly `"value"`** for a plain positional token, or the exact
  `name` as it appears in an existing `name=value` token. `TUNER_SCHEMA` enforces a
  bare identifier and `verify_pre` is instructed to reject anything not matching an
  existing token, but a weak model can still get this wrong — do not assume a
  proposal that passed schema validation is applicable.
- Local models are noticeably more reliable at agents with **no tool calls** (tuner,
  verifier) than at tool-calling agents (simulator, judge). If a weak-model run
  fails, check which agent failed first.
- If structured output still does not validate after retries, `orchestrator.py`
  catches `AgentExecutionError` and returns a clean `FAIL` instead of crashing.
  **Don't remove it** — and the same `try` catches `ValueError` (belt-and-braces
  against the netlist-apply path) and `OSError`.

### SPICE, sky130, and circuit physics

- **sky130 device models are binned and exceeding a bin is a hard error.**
  `wmax`/`lmax` are 100 µm. `W=120` aborts with `could not find a valid modelname`
  — not a warning.
- **`mult` on a sky130 `pnp_05v5` does nothing.** It scales only mismatch terms,
  which are zero without Monte Carlo. The emitter-area ratio a bandgap needs is the
  *instance* multiplier `m=8`; `mult=8` yields ΔVbe ≈ 0 and no PTAT current at all,
  silently.
- **An nfet MOS cap cannot float** — its body is the p-substrate, so one plate is
  pinned to `vss`. A Miller cap must be a *pfet* MOS cap (isolated nwell body).
- **The first line of a SPICE deck is the title.** A `.temp` placed there is
  silently consumed and the run happens at 27 °C.
- **`Lfb`/`Cin` loop breaking does not survive a cascode.** A 1 MH inductor is only
  a 6.3 MΩ open at 1 Hz, so against tens of megohms the loop is never broken;
  raising it to 1 GH spans the matrix over ~20 decades and the solver returns
  garbage. Where the break point drives a MOS gate, use series voltage injection
  (`DC 0 AC 1`) and read loop gain as `vdb(out)-vdb(in)` — exact, no reactive
  elements. See `benchmarks/bandgap/netlist_loops.cir`.
- **A cascoded amp with a CS output stage can latch itself off.** If the bias chain
  collapses with the core, every CS stage's NMOS sink turns off while its PMOS is
  fully on, pinning each amp output HIGH; a startup pull-down then has to outfight a
  much larger PMOS and loses. Keep a trickle current in the bias chain
  (`BGR_CORE.Xsu_b`).
- **`.option scale` must be declared in the netlist itself, not only in an
  include.** `parse_netlist` never follows includes, so a deck that gets its scale
  from `pdk_corner.inc` alone reads `W=30` as thirty *metres* — which is how the
  area tiers came to be inert on every PDK-backed benchmark.

### Netlist parsing, parameters, and the area gate

- **An inline `$` or `;` comment is stripped before parsing, and re-appended by
  `apply_changes`.** Leaving it in swallowed the model name into the node list and
  made `param="value"` replace the comment's last word.
- **A parameterised value is resolved before the area gate reads it** (`params.py`).
  Without this, `W='wn*2'` was unparseable and the "cannot judge, do not block"
  fallback fired on every device. The resolver's subset is deliberately narrow
  (arithmetic only); anything else resolves to `None`.
- **Tokenise with `netlist.split_tokens`, never `str.split()`.** It keeps `'...'`
  and `{...}` whole, so `W='wn * 2'` stays one token. Plain `.split()` pushed the
  model name into the node list, made `value` become `2'`, and rewrote
  `W='wn * 2'` → `W=50 * 2'`. Every gate passed the corrupted deck, so it reached
  ngspice and looked like a bad tuning proposal rather than a parser bug. `{...}`
  nests; `'...'` does not.
- **Fold `+` continuations with `netlist.logical_lines`.** It returns
  `(code, [physical line indices])`, so parsing sees the joined statement while
  `apply_changes` edits the physical line the token sits on. Treating a `+` line as
  its own statement produced a component with refdes `+` that *stole* the real
  device's parameters — leaving `M1` with no area baseline — and made
  `apply_changes` write `W` twice.
- **A parameter's scope decides whether it resolves, and a contested name resolves
  to nothing.** Precedence is global < subckt body `.param` < `.subckt`-line
  default < instance override. When a name is declared both in the body and on the
  `.subckt` line — or when instances disagree — it is dropped *and* masked from the
  global environment, so the caller sees "unknown" rather than a global standing in
  for a local.
- **`m` multiplies area, `nf` does not.** `m` is a count of parallel devices; `nf`
  splits one device into fingers, so total width and area do not change. So the gate
  gives `w`/`l` the size-graded tiers, `m` a **flat 2.0×**
  (`COUNT_ALLOWED_MULTIPLIER` — a count, not a length), and `nf` **no tier at
  all**. That last is "nothing to judge", not "cannot judge" — do not "fix" an
  unconstrained `nf` by adding a tier. In a wrapper-cell flow widths are fixed and
  `m` is varied per instance, so the flat count tier is what actually binds, and the
  25/50 µm geometry boundaries rarely will. `m` and `nf` are counts, so a
  non-integral proposal (`m=6.5`) is rejected outright.
- **Total width is `w × m`, so the gate evaluates the product per physical device.**
  Changes are grouped by the device they reach and their ratios multiplied; the
  allowed multiplier is the tightest tier among the parameters involved (`nf`
  excluded from the product entirely). Without this, one proposal growing `w` 3× and
  `m` 2× grew total width **6×** and nobody looked. The group key carries the
  intermediate instance chain (`TracedTarget.chain`), because a wrapper
  instantiating the same unit cell twice returns two targets holding the **same
  `Component` object**, and without the chain their one shared ratio was multiplied
  twice — a legitimate 2.5× reported as 6.25×. **`ratio^N` is not a quantity at
  all**: if both devices grow 2.5×, the per-device ratio is 2.5× *and* the
  total-area ratio is 2.5×. Do not "restore" the squaring thinking it was the safe
  choice.
- **`m` multiplies the tier baseline for every device class except `Q`.** A MiM cap
  with `m=4` occupies four times the area, so tiering on the single-unit `w` handed
  it the loosest tier. `Q` is the exception because its `m` *is* the tier key.
- **An instance parameter can also reach a device's positional value.** `R`/`C` size
  knobs are positional, which is why their tiers are keyed on the value. Tracing
  only `device.params` left a *wrapped* resistor unbounded while the identical bare
  one was blocked — the same 1000× growth decided by whether the designer wrapped
  it. `params._positional_target` accepts the positional value only when it is a
  **bare identifier** matching the parameter name; an expression like `{rv*2}` is
  refused, because assuming the parameter's ratio equals the device's is exactly the
  guess this layer forbids.
- **The trace needs the wrapper cell's definition *in the deck*, and a wrapper cell
  library normally arrives as an `.include`.** This is *not* fixed by making the
  parser follow includes. Instead the blindness is recorded: `evaluate_area_growth`
  returns a per-change **visibility state**, logged in `area_check` as `states`. The
  four states are different facts and must stay distinguishable — `bounded` (a tier
  applied), `neutral` (nothing to bound: `nf`), `blind` (instantiates a subckt this
  deck does not define; `Component.undefined_subckt`), `unjudged` (a value could not
  be resolved). A sky130 primitive is `bounded`, not `blind`.
- **Per-instance parameter resolution is a different tool from
  `build_param_envs`.** The latter resolves per subckt *definition* and drops any
  name the instances disagree on — and in wrapper-cell decks disagreement is the
  normal case, so it returns `None` exactly where a number is needed.
  `params._instance_env` resolves for one instance: that instance's own override →
  the `.subckt` line default → a literal in the body, with the override's expression
  evaluated in the *outer* scope. It **applies the same shadowing rule**. Narrowing
  to one instance removes the "which instance?" ambiguity but not the dialect
  ambiguity, so the two resolvers must not disagree — they did, and the gate acted
  on the one that guessed (picking a `.subckt`-line 10 µm over the body's 60 µm,
  choosing a 3.0× tier instead of 1.5×). Tracing follows a body token into a nested
  instance, bounded by `_MAX_TRACE_DEPTH`, and falls back to "cannot judge, do not
  block". A subckt the deck does not define is a leaf, not a dead end.
- **`netlist.py` tracks subckt scope.** A component inside a `.subckt` is
  addressable as `<SUBCKT>.<refdes>`; an unqualified refdes works when it matches
  exactly one component netlist-wide and raises `ValueError` when ambiguous. The
  scope is the subckt *definition*, so a change applies to every instance — two
  differently-tuned instances require two subckts. Nested subckts **are**
  scope-tracked with dotted paths, and a qualified refdes must match a path exactly
  — a partial path like `INNER.M1` is rejected rather than guessed at.
  `check_refdes_resolution` rejects an unresolvable or ambiguous refdes before the
  proposal is applied and before any LLM call, logged as `refdes_check`;
  `apply_changes` still raises `ValueError` as a second line of defence.
- **`param="value"` misapplied to a non-numeric positional token** used to crash the
  run with an uncaught `ValueError` from `check_area_growth`, found by review before
  it shipped. Fixed by treating an unparseable baseline as "can't judge, don't
  block" (skip that change). If you touch `check_area_growth`, keep this guard.

## Testing conventions

- TDD throughout; every module has a paired test file in `tests/unit/`.
- Agent tests mock `run_agent`/`AgentBackend`, never hit a real LLM.
- `tests/integration/` holds two skip-gated real-backend tests
  (`ANTHROPIC_API_KEY`, `LOCAL_LLM_BASE_URL`) — skipped by default.
- `tests/unit/*_ngspice.py` assume `ngspice` on PATH rather than skipping, and all
  but three finish in seconds. The long one is
  `test_optimizer_bandgap_ngspice.py`'s corner-anchored case at ~30 min (six
  45-corner sweeps); deselect it for a normal TDD cycle and run it before merging
  anything under `optimizer.py`, `area.py`, `pvt.py` or `judge_tools.py`. Another
  is `test_corner_reduction_bandgap_ngspice.py` at ~129 s, dominated by two
  9-corner × 5-testbench sweeps shared through module-scoped fixtures. The third is
  `test_optimizer_area_phase_ngspice.py` (Task 6 of the area-optimization-phase
  plan, ~148 s total: bandgap ~124 s over 5 testbenches per step,
  two_stage_opamp ~24 s over 4) — it measures `optimizer.run_area_optimization`
  against both benchmarks and its numbers are the 2단계 baseline table in
  `docs/superpowers/plans/2026-08-02-area-optimization-phase.md`.
  **That table's two_stage_opamp row was measured before the 2026-08-04 bias fix and
  the plan is left as the historical record, so re-measure rather than quote it**:
  what still holds is `UNCHANGED`, 0 accepted / 20 rejected, ~23 s / 21 sims; what
  moved is the area (2.38037e-10 → 2.37037e-10) and the zero-gain knob list, where
  `OPAMP2STAGE.Rdeg.value`/`Rstart.value` became `OPAMP2STAGE.Rbias.value` (7 → 6).
  `test_curation_ngspice.py` (~18 s) stays unmarked because the `slow` marker here
  means minutes, not seconds.
- **`pytest -m "not slow"` is the normal TDD cycle. Measured 2026-08-05: 1666
  passed, 2 skipped, 9 deselected, 101.29 s** — and on 2026-07-30 at 1468 tests, two
  runs on the same commit came out 98.5 s and 120.6 s, so read the budget as ~2 min.
  **The spread between two identical runs is wider than a year of count growth**
  (1273 → 1473 → 1499 → 1529 → 1546 → 1548 → 1573 → 1605 → 1613 → 1636 → 1635 → 1646 → 1666), so do not treat a single timing as a
  regression signal. A
  plain `pytest -q` is ~3 min and ~33 min with everything. All three slow files
  carry the `slow` marker, registered in `pyproject.toml`. **Re-measure this line
  when you add a real-simulator test** — it has drifted four times now
  (`test_optimizer_area_phase_ngspice.py` is the latest addition, and being
  `slow`-marked it does not itself change this count — confirmed by collecting
  under `-m "not slow"` before touching this line: deselected went 7 → 9, exactly
  the two new test functions it adds).
- **A drift guard is updated in one order only**: confirm the new inputs pass the
  gate, *then* raise the count.
  `test_every_shipped_benchmark_control_block_is_accepted` counts 61 control blocks
  across 16 specs (42 → 47 → 52 → 56 → 61); each bump verified 0 rejections first.
  Reverse the order and the guard stops preventing unaudited additions and becomes
  a comment that follows a number.
