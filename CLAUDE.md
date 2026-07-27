# analogcoder

CLI that automates iterative analog circuit verification and repair: run a SPICE
simulation, judge the result against pass/fail criteria from a spec, and if it
fails, propose and apply netlist parameter changes, then re-verify — repeating
until it passes or hits iteration/retry limits.

## Architecture

Four independent LLM agents (simulator, judge, tuner, verifier) coordinated by
a deterministic (non-LLM) Python orchestrator in `orchestrator.py`.
The orchestrator never parses free text — every agent call returns JSON validated
against a fixed schema (`schemas.py`).

Most bullets below are not module summaries — they are facts a run or a review
disproved an assumption with, kept because re-deriving them costs a
45-corner sweep or a wrong tuning proposal. Where a bullet names a number,
that number was measured.

### Backends and agents

- `agents/backend.py` — `AgentBackend` interface, `ToolSpec`, `AgentExecutionError`.
  All LLM execution is behind this interface; agent modules never call an LLM
  SDK directly.
- `agents/backends/claude_sdk.py` — `ClaudeSDKBackend`, wraps claude-agent-sdk.
  This is the default backend and rides on a Claude Code subscription — no
  separate Anthropic API key/billing needed for normal use.
- `agents/backends/openai_compatible.py` — `OpenAICompatibleBackend`, talks to
  any OpenAI-style `/chat/completions` endpoint (base URL + bearer token env var
  + model name). Built for eventually running against a lower-capability
  local/company LLM instead of Claude. Has its own tool-call loop and a
  schema-validation-with-repair retry loop, since local models are much less
  reliable at strict structured output than Claude.
- `simulators/base.py` / `simulators/ngspice.py` — `SimulatorBackend` adapter,
  same pattern, for swapping the SPICE engine (only ngspice implemented; HSPICE
  is a documented future backend).
- `agents/*.py` (judge, simulator_agent, tuner, verifier) — one file per agent:
  system prompt + schema + tool declarations (`ToolSpec`, not
  provider-specific). Every public function takes a required `backend:
  AgentBackend` as its last positional arg.

### Spec, topologies, and the area gate

- `topologies.py` — a small curated library of pre-verified amplifier
  topologies the orchestrator can swap in as a last resort after repeated
  parameter-tuning rollbacks (`TOPOLOGY_SWITCH_THRESHOLD`), instead of only
  ever changing existing component values. See "Benchmarks" below.
- `area_limits.py` — a deterministic gate the orchestrator runs before every
  parameter-tuning proposal is applied: rejects (with retryable feedback)
  proposals that grow a component's size beyond a size-tiered limit relative
  to where it started the run (`netlist_v0`), since there's no PDK in this
  project to derive a real physical area model. Motivated by a real run
  where Claude fixed a phase-margin failure by widening a transistor 2.5x
  instead of finding a smaller fix. Runs before the LLM-based `verify_pre`
  call, so an obviously oversized proposal never spends an LLM call.
  Exhausting all retries on area rejection alone is treated like a
  parameter-tuning rollback (not an immediate run failure), so it composes
  with the topology-swap threshold above — repeated area-blocked tuning can
  escalate into a topology swap instead of just giving up. See
  `docs/superpowers/specs/2026-07-25-area-aware-tuning-design.md`.
  Size tiers are keyed on *scaled geometry* for sky130 primitives (`W`/`w`/`l`
  times the deck's `.option scale`), and on the component's own value for
  generic M/C/R. Reading a bare `W=30` as an absolute value had put every PDK
  device in the unbounded 1.5x tier, so the tier table did nothing on exactly
  the benchmarks that use a real PDK. A `pnp_05v5` is tiered on its emitter
  multiplier `m`, a count rather than a length.
  Where a deck is built from **wrapper cells** — a generic cell that
  declares its geometry as parameters (`ma1 d g s b UNITDEV_N_LVT w=wn l=ln m=ma1
  nf=nf_n`) with the real numbers arriving on the *instance* line
  (`xwrap1 ... WRAPCELL_A wn=2e-6 ma1=4`) — the gate **traces** each instance
  parameter to the body token it lands on (`params.annotate_traced_params` →
  `Component.traced_params`) and tiers on that. It never reads meaning out of
  the instance parameter's *name*: `wn`/`ma1`/`nf_n` are the designer's naming
  convention, and guessing them is the same class of error as recognising a
  supply rail by the name `vdd`. The **body token** name (`w`, `l`, `m`, `nf`)
  is standard SPICE device syntax, so it is a fact. Before this the wrapper
  instance's `value` was just a subckt name, `_classify_ctype` left ctype `X`,
  `TIERS_BY_CTYPE` had no `X` entry, and every sizing knob on such a deck was
  completely unconstrained — the third time this gate has been silently inert.
- `spec.py` — `spec.yaml` declares one or more **testbenches** (`TargetSpec.testbenches`),
  each with its own netlist file, control block, and criteria — not a single
  implicit testbench. `TargetSpec.canonical` (`testbenches[0]`) is the netlist
  text handed to the tuner, `verify_pre`, and the area-growth baseline indexer;
  `TargetSpec.all_criteria` flattens every testbench's criteria for one `judge`
  call per iteration. `simulate` fans out to one call per testbench (in
  `cli.py`'s `simulate_fn`, merging measurements) while `judge`/`tune`/
  `verify_pre`/`verify_post` stay at one LLM call per iteration — every
  iteration re-simulates and re-judges *all* testbenches together, so a change
  that fixes one testbench's criterion can't silently regress another's.
  `RunState` (`state.py`) versions each testbench's netlist independently but
  always in lockstep (`push_netlist_version`/`rollback` operate on the whole
  set atomically — never partially applied across testbenches). See
  `docs/superpowers/specs/2026-07-25-psr-verification-design.md`.

### The optimization phase

- `optimizer.py` / `agents/optimizer.py` / `area.py` — a second phase that runs
  after the loop returns PASS and before the final PVT sweep, spending the
  spec's remaining margin on the objective declared in `spec.yaml`'s
  `optimize:` block. The agent only *ranks knobs*: `OPTIMIZER_SCHEMA`
  structurally forbids a value, and a deterministic search decides how far to
  move each one (`×0.9` per step for a geometry, `±1` for a count) and measures
  the result. **The accept rule is deterministic and deliberately does NOT
  reuse `verify_post`**: that contract is "roll back if regressed", and a good
  optimization step consumes margin on purpose, so reusing it would roll back
  every successful shrink. A step is kept only if every criterion still passes
  with its guarded margin, the objective fell, and total area is inside the
  budget. **Optimization has no FAIL outcome** — failing to improve returns the
  design that already passed, so a run cannot end worse for having optimized.
- **The margin allowance is measured, not guessed.** Each criterion's allowance
  is `|worst corner − nominal|` read off the *entry* corner sweep, which is why
  that sweep is an anchor rather than an overhead. The `guard_band` ratio
  declared in the spec only fills criteria the sweep produced no value for —
  and it has to, because `corner_allowances` omits those, while the consumer
  reads a missing name as allowance `0.0`, i.e. **no guard at all** on exactly
  the criteria whose corner behaviour is unknown. Nominal measurements come out
  of `cli.py`'s LLM-mediated `simulate_fn` and sweep measurements out of
  `sim_backend.run`, so the two key sets really can differ.
- **The guard band is `T ± g·|T|`, never `T·(1±g)`.** The latter inverts on a
  negative threshold: `psrr_dc <= -25` with `g=0.2` would become `<= -20`,
  *looser* than the original. Each criterion is judged against its own
  threshold, so a two-sided window on one measurement keeps both sides —
  `pvt.py` lost one side of exactly that shape twice.
- **The ratio fallback alone is not a usable guard on a real spec, which is the
  measured case for deriving allowances.** On `benchmarks/bandgap`, `g=0.2`
  demands `vbgout_v >= 1.44` *and* `<= 1.024` — an empty interval the 1.2389 V
  baseline already violates, so the corner-less `spec.yaml` can never accept a
  step. `spec_pvt.yaml`'s sweep replaces that 0.24 with a measured 0.0051 and
  the same search then accepts ten at nominal (four of which survive the
  confirmation — next entry). Both outcomes are pinned in
  `tests/unit/test_optimizer_bandgap_ngspice.py`.
- **On a failed confirmation the loop bisects the accepted versions**, it does
  not re-search with a bigger guard. Re-searching is a retry with a larger
  guess and no cost ceiling; bisection is bounded (`ceil(log2 n)` sweeps),
  directed, and lands on a version whose sweep was actually observed to pass —
  worst case the anchor, i.e. the design the main loop already shipped.
- **A guard band measured at the starting point does not hold once the circuit
  has moved, and the first real run proved it.** On `benchmarks/bandgap` the
  nominal search accepted 10 steps on `TRIMAMP.Xt.W` (8 → 2.78943, `iq_ua`
  212.99 → 211.68 µA) and the confirmation sweep then failed **six** criteria
  — draining that tail widens the very corner spread the allowance was read
  from. Bisection probed v5 (fail), v2, v3, v4 (pass) and landed on v4
  (W=5.2488, 212.25 µA, corner-confirmed): 4 of 10 steps survived. So the
  confirmation is not a formality — it is what keeps this phase honest, and a
  nominal-only optimizer would have shipped a design that fails at corners.
  Cutting the search's corner blindness is sub-project B; until then, expect
  the confirmation to walk most of a long descent back.
- **`check_stimulus_untouched` is a prerequisite of this phase, not a reuse.**
  The cheapest way to cut quiescent current is to lower a supply, and an
  explicit current objective puts that degenerate answer far closer to hand
  than it ever was for repair tuning. All four addressing gates run on the
  optimization path, on the full deck rather than the folded prompt view.
  **They do not close the degenerate-answer surface, and reading them as if
  they did is the mistake.** The stimulus gate covers top-level `V`/`I` only,
  because that is the part that can be decided by a *fact* (refdes prefix plus
  top-level scope). `benchmarks/two_stage_opamp/netlist.cir` also has
  top-level `Lfb` (the 1 MH loop-break inductor), `Cin` and `Cload` — pure
  testbench apparatus that sits in the tunable index and is reachable by the
  optimizer. Shrinking `Cload` improves phase margin and UGBW without touching
  the DUT: manufactured margin the phase then spends on current, the same shape
  as the `Vin AC 1 → AC 100` finding that motivated the gate. Widening the gate
  to "top-level passive" is *not* the fix — on `benchmarks/inverting_amp` the
  top-level `Rin`/`Rf`/`Eopamp` **are** the circuit, so that rule is the same
  class of guess as recognising a rail by the name `vdd`. It is handled where a
  judgement call belongs: a paragraph in `OPTIMIZER_SYSTEM_PROMPT`. Only the
  bandgap specs declare `optimize:` today, so this is not currently
  exploitable — but the next spec that does is where it becomes live.
- **Area is derived, the objective is measured.** `area.total_area` sums
  `w × l × m` over resolvable devices — `m` multiplies area, `nf` does not,
  since finger splitting leaves total width unchanged — so an over-budget
  candidate is discarded before it spends a simulation. That asymmetry is why
  the loop is simulation-bound and why the agent ranks a few knobs instead of
  sweeping. Two things it is *not*: it sums over subckt **definitions**, so a
  definition instantiated N times is counted **once** — a per-definition sum,
  not a physical total. No benchmark has `instance_count > 1`, but the
  production wrapper flow instantiates the same unit cell repeatedly, and
  `structure.blocks[path].instance_count` is what a weighted version would
  read. And the budget compares `area / area_before`, so **`area_before == 0`
  disables it entirely** — reachable, because `total_area` resolves via
  `build_param_envs`, which drops any name the instances disagree on, and on a
  wrapper-cell deck that is every device (`tests/unit/test_area_total.py` pins
  `counted == 0, skipped == 2`). That silence is now recorded rather than
  merely true: `AreaTotal.counted`/`skipped`, the enforced flag and an explicit
  reason go into the `optimize_baseline` event and `result["area_coverage"]`.
  This is the **fourth** silently-inert area gate in this repo (`.option scale`
  read as metres, include-only wrapper cells, wrapper instance parameters, and
  now a zero baseline), and the first three were invisible in every run log.
  A new gate ships with the record of when it did nothing, not just the rule.
- **The optimization phase has no FAIL outcome, and that has to include
  crashing.** `_run_simulation` and `_run_sweep` each swallow a bare
  `Exception` for that reason; the module's one LLM call (`agents.propose`) did
  not, and `ClaudeSDKBackend.run` raises `AgentExecutionError` on any error
  `ResultMessage` — rate limit, transport error, `structured_output is None`,
  or a weak local model missing the schema (documented below as an *expected*
  case). It escaped `asyncio.run` in `main()`, so `write_result_json` and
  `write_report_md` never ran: a run that had already PASSed ended as a
  traceback with no `result.json` and no `report.md`. `run_optimization` is now
  a guard wrapping `_optimize` in
  `except (AgentExecutionError, ValueError, OSError)`. The first two are
  `run_orchestration`'s documented pair (`ValueError` for an `apply_changes`
  failure on a *non-canonical* deck, which the addressing gates cannot see
  because they only read the canonical text). **`OSError` is a third that only
  this phase needs**: bisection re-reads a version deck from disk (`_texts_at`),
  and a failed `open` is neither of the other two — `run_orchestration` never
  re-reads, so it has no such case. The guard rolls back to the version the
  phase started from (an unconfirmed pushed version must never be what the run
  returns), logs `optimize_failed`, and returns a well-formed `UNCHANGED`.
- **The guard band can be infeasible at the baseline, and then no candidate can
  ever be accepted.** `benchmarks/bandgap/spec.yaml` is the measured case (see
  the ratio-fallback entry above). The condition is *not* "`pvt_corners` is
  absent" — the measured path reaches it too, whenever nominal is worse than
  every corner for some criterion, since `allowance = |worst − nominal|` then
  makes the guarded limit stricter than nominal itself.
  `guard_band_violations` runs on the baseline right after the allowances are
  built and is logged **unconditionally** as `optimize_guard_infeasible`
  (logging only on violation makes "checked, fine" and "the check is gone"
  identical in `history.jsonl`) plus `result["guard_infeasible"]`. It
  deliberately does **not** early-return: a step can in principle push the
  violating criterion back inside, the cost ceiling is already one simulation
  per candidate (a rejection exhausts its candidate), and the failure this repo
  keeps repeating is doing nothing silently, not doing too much.
- **The result must describe the deck it returns.** `result["final_criteria"]`
  is the main loop's judge output — the deck *before* optimization — and
  `cli.py` updated only `final_netlist_paths`, so the measured bandgap run
  printed `iq_ua` 212.99 µA beside a netlist that measures 212.25 µA and never
  said the phase ran. `_search` now stores each version's `evaluate_criteria`
  verdict in `records`, so what is reported is the version **bisection landed
  on**, not the last accepted step, and `report.md` carries an Optimization
  section (objective/area before→after, steps, corner confirmation, and the
  guard-infeasible / area-coverage / phase-failure reasons — without those the
  run still says PASS while the phase did nothing).

### Corner reduction and re-entry

- `corner_selection.py` / `corner_sim.py` — the tuning loop no longer simulates
  one nominal point. `seed_from_sweep` takes the *entry* corner sweep (already
  paid for, see the optimization phase's measured-allowance entry) and seeds a
  `CornerSet` with the union of each criterion's worst-case corner; `corner_sim.
  build_corner_simulate` wraps the existing `simulate_fn` contract so `judge`
  sees the **worst value across that set** plus `corner_worst`/`probe`, and
  `cli.py` re-enters the whole loop when the final sweep fails, having first
  grown the set with the corners that failed. Declared per spec via
  `corner_reduction: {enabled, retry_budget, probe}`; absent block means today's
  behaviour, and *why* it is off is logged (`corner_reduction_inactive`) because
  silently doing nothing is this repo's recurring failure shape.
- **The reduced set is always optimistic, and that is the locked constraint.**
  A mid-loop FAIL is real — some corner in the set genuinely violates a
  criterion, and a corner is a corner whether or not you looked at the other 40.
  A mid-loop PASS can be wrong, because a corner outside the set may fail. The
  asymmetry is what makes the reduction safe: the full sweep still runs before
  anything is reported, so the only cost of a wrong PASS is an iteration, never
  a wrong verdict. Every design decision here defends that direction — which is
  also why the probe does not vote (below).
- **Nominal is the deck itself, not a name.** `corner_selection.NOMINAL is None`
  and `_run_point` simulates the file on disk unrendered. `tt/27` is a real
  corner and is *not* nominal: rendering the deck through `tt` rewrites its
  include and injects a `.temp`, so it is no longer the deck whose thresholds
  were set. `pvt._corner_fields` reports it as `"(deck)"` with no numbers, and
  `corner_selection._as_point` **rejects** that shape — detected by the *absence*
  of voltage/temperature coordinates, never by matching the string `"(deck)"`.
  Without the rejection a `CornerPoint(process="(deck)", ...)` reaches
  `render_corner_netlist`, which writes `.include ".../pdk_corner_(deck).inc"`
  and hands ngspice a file that does not exist: "this point has no coordinates"
  silently becoming a coordinate.
- **Never put a NaN in a `CornerPoint`.** It is a frozen dataclass, so it
  compares and hashes by field, and `NaN != NaN` — such a value is not equal to
  *itself*. Every set operation in this module then breaks silently:
  `point not in cs.corners` is true for a corner that is already there, so
  `grown_with` re-adds it and `CornerSet.__post_init__`'s duplicate check turns
  a diagnosis into a `ValueError`; `next_probe` can hand back a corner that is in
  the selected set. Nothing constructs one today (coordinates come from the
  spec's `pvt_corners`), so this is a rule for whoever first derives a corner
  from a *measurement*.
- **The probe does not vote. It only promotes.** Each iteration also simulates
  one corner from outside the set (`next_probe`, rotating in ascending severity
  so the tightest goes first), and if it fails, `promote` moves it into the set
  permanently. Its measurements are deliberately **not** merged into the judged
  worst case — mixing them would destroy the optimism argument above, since a
  probe result is one sample of a rotation and the set would then be judged on
  a corner it will not see next iteration. A probe that crashes is recorded
  (`error` in the `corner_probe` event) and judges nothing; the rotation is
  committed in a `finally` so a raised judge-path exception cannot pin the box
  on one probe corner forever.
- **A verdict failure that can add no new corner is a path disagreement, and it
  is not retried.** If every failing criterion's worst corner is *already* in
  the mid-loop set, then the two execution paths judged the same deck at the
  same corner differently, and retrying re-runs identical information. `cli.py`
  reports that (`corner_path_disagreement`) instead of looping. The diagnosis is
  not vacuous — there are three real channels for it. Two are stochastic: the
  mid-loop uses the control block the **simulator agent converged on** while
  `run_full_pvt_sweep` uses the spec's text, and the mid-loop's judge is an
  **LLM** while the sweep calls `evaluate_criteria` directly. The third is
  **deterministic and always present**: on any criterion that shares a
  measurement name with another (a two-sided window), the mid loop is
  structurally blind to one side — see the collapse entry below. **Check that
  one first.** If the disagreeing criterion is one half of a `_min`/`_max` pair,
  the cause is the collapse, not the LLM, and no amount of re-reading agent logs
  will show it. A failing criterion with no worst corner at
  all is a *different* fact (`corner_unattributed_failure`): the circuit produced
  no measurement anywhere, so there is nothing to add. Calling that a path
  disagreement would be an unsupported structural claim, the same error shape as
  `OPAMP2STAGE drives vdd,vss`.
- **The allowance baseline moved from nominal to whatever the search actually
  measures.** `judge_tools.corner_allowances`' first argument is now `reference`,
  not `nominal`, and `optimizer.py` passes `baseline_measurements` — the value
  `_search` really sees. Once the search reads the reduced set's worst case,
  re-measuring nominal separately would count the same corner spread twice and
  over-tighten the guard, since the reduced-set worst is already near the
  extreme. The function does not decide what the reference is; the caller does.
- **Measured on `benchmarks/bandgap/spec_corner_reduction.yaml` (9 corners:
  tt/ss/ff × 1.62/1.8/1.98 V × 27 °C), and the numbers are unflattering.**
  Seed = **6 corners + the deck = 7 selected points**, and the mid loop also
  runs **1 probe**, so an iteration costs **8 simulated points out of a 9-corner
  grid — per testbench**. That per-testbench qualifier is the whole cost story:
  the loop in `corner_sim` is testbenches-outside, corners-inside, so this spec's
  5 testbenches make it **40 direct simulations per iteration** (~250 s) against
  5 before the branch, not the ~4 the design's cost table reads as. LLM calls per
  iteration are unchanged. The saving against the full grid is one point per
  testbench, not two. It compounds with criteria count: the seed is bounded by
  `min(#criteria, #corners)`, so enabling this block on `spec_pvt.yaml` (45
  corners, 22 criteria) projects to ~125 direct sims per iteration — which can
  cost *more* than the 286 s full sweep it is meant to pre-empt. A `max_corners`
  ceiling is out of scope here and is a prerequisite for that spec. The 3 corners
  left outside the set are all `tt`. Re-entry fired **zero** times. argmax drift
  between the entry sweep and the verdict sweep of a moved deck
  (`TRIMAMP.Xt.W`/`BUF_P.Xt.W` 8 → 4, which does fail `buf1_loop_gain` and
  `buf1_phase_margin`): **5 of 22 criteria moved**, and **all 5 landed on corners
  already inside the set**, so `grown_with` added nothing. Pinned in
  `tests/unit/test_corner_reduction_bandgap_ngspice.py`.
  **Those 6 corners do not cover all 22 criteria.** This spec carries three
  two-sided windows — `vbgout_min`/`vbgout_max`, `vbg0_min`/`vbg0_max`,
  `vbg1_min`/`vbg1_max` — and on each pair the mid loop can see only the `_max`
  half (collapse entry below), so **`vbgout_min`, `vbg0_min` and `vbg1_min` are
  never actually judged in the mid loop** no matter which corners are selected.
  Two of the five drifting criteria (`vbg1_min`, `vbg1_max`) are the two halves
  of one such window. Read "the mid loop watches these 6 corners" as covering
  19 of 22 criteria, not 22.
- **The seed is bounded by `min(#criteria, #corners)`, which is why the first
  grid reduced nothing.** The planned grid was process-only (tt/ss/ff, 3
  corners); measured, the seed was **all 3** — `ff` for the voltages/PSRR/loop
  gains, `ss` for TC/startup/droop, `tt` for `vbg1_residual`. Tightening a
  criterion does **not** fix that: `seed_from_sweep` reads neither `overall_pass`
  nor any threshold, only each criterion's argmax. With 22 criteria the union
  saturates any small grid, so the only lever is a bigger grid — hence the
  voltage axis came back. Expect real reduction only where corners ≫ criteria.
- **Re-entry is not dead code, but this benchmark cannot reach it.** Growth
  requires a *failing* criterion whose argmax sits **outside** the set — and if
  a criterion's argmax is inside, the mid loop measured that same corner and
  would have failed there first. On this deck the only outside corners are `tt`,
  the typical corner, which is nobody's worst case. That is structure, not luck.
  The mechanism does fire in principle: a smaller move (`TRIMAMP.Xt.W` 8 →
  5.2488, the value the optimizer's bisection landed on) drifted exactly one
  argmax — `vbg1_residual`, `ff/1.98` → **`tt/1.62`, outside the set**. It just
  was not a failing criterion. Re-entry needs an argmax to leave the set *and*
  that criterion to fail.
- **A two-sided window shares one judge slot, so the slot is resolved rather
  than overwritten — and the corner path is what exposed it.**
  `worst_case_measurements` returns a dict keyed by *measurement* name, so
  `vbgout_min` (`>=`) and `vbgout_max` (`<=`) — two criteria over one
  measurement — cannot both be represented. It used to write per criterion, so
  the later-declared one simply won: the mid loop handed the judge
  `vbgout_v = 1.24512` (the ss/1.62 **maximum**) while the value `vbgout_min`
  should be judged against was 1.233753 at ff/1.98. That is the **third** time
  this file records `pvt.py` losing one side of a two-sided window.
  The cost was not one iteration but the whole `retry_budget`: a low-side
  violation left the judge holding the maximum, which satisfies both `>= 1.20`
  and `<= 1.28`, so the mid loop reported PASS; the verdict sweep failed,
  `grown_with` added the offending corner, and the enlarged set's **maximum**
  overwrote the slot again — the loop **cannot converge on the blind half of a
  two-sided window**, it re-derives the same PASS until the budget is gone.
- **The fix keeps the judge's contract and does not fabricate a value.** The
  dict is still exactly one float per measurement name — `judge`,
  `evaluate_criteria`, `guard_band_violations`, `optimizer._search` and
  `run_full_pvt_sweep` are all untouched (the last one reads
  `combined_worst_corners`, not `measurements`, and evaluates one criterion at
  a time against its own worst value; that is where the trap was already
  solved 70 lines away). Candidates are accumulated per measurement name and
  the collision is resolved by **preferring a value that violates one of the
  criteria sharing that name**, falling back to the old last-writer when they
  all pass. Every candidate is a real measurement at a real corner of the
  selected set, so nothing is synthesised, and because every candidate lies in
  `[min, max]` while a threshold comparison is monotone, substituting one
  criterion's worst case for another's **can only reveal a violation, never
  invent one**: if a `<=` criterion's own max passes, every candidate below it
  passes too, and symmetrically for `>=` against the min. So this branch's
  claim 2 still holds in the direction it is claimed — a mid-loop FAIL is
  genuine, a mid-loop PASS is still merely optimistic — and the loop can now
  converge on the previously blind half. The corner-less single-criterion case
  (every other spec here) takes the same code path with one entry and is
  unchanged. Pinned in
  `test_a_violated_side_of_a_two_sided_window_wins_the_shared_measurement_slot`
  (violating side wins) and
  `test_a_two_sided_window_with_both_sides_passing_keeps_the_last_writer`
  (fallback), with the measured bandgap case — where *both* sides pass, so the
  fallback is what runs — in `test_corner_reduction_bandgap_ngspice.py`.
  `corner_worst` still carries both sides per criterion either way.
- **Best-arm identification was considered and rejected.** Pure-exploration
  bandits (successive halving, LUCB, racing) exist to spend a sampling budget
  well when each evaluation is *noisy* — their entire gain structure comes from
  needing repeated samples to separate arms whose means are close. SPICE is
  deterministic: one evaluation of a corner is that corner's exact value, there
  is no confidence interval to shrink, and re-sampling is pure waste. What
  remains is a covering problem over a known finite grid, which is what
  `seed_from_sweep` (union of argmaxes) plus a rotating probe already is.

### Deterministic netlist derivation, and what the tuner is shown

- `structure.py` / `signal_path.py` / `patterns.py` / `control_block.py` /
  `structure_view.py` — the deterministic replacement for what used to be an
  LLM `analyzer` agent. That agent contributed nothing measurable: a run
  passed in 4 iterations on an analysis that was `{"circuit_type": "test",
  "component_roles": {"a": "b"}, ...}`, and across runs on one bandgap
  netlist it produced 93, 26 and 1 component roles. The tuner succeeds by
  reading `netlist_text`, which it still receives. `structure.py` derives
  flat per-scope facts (inventory, device classes, the tunable
  `(refdes, param)` index); `signal_path.py` maps
  ports to nets across hierarchy and labels each net's drivers and sensors
  by *definition* name, since a definition is what the tuner can address.
  A net's entry holds a **set** of roles per definition, not one winning role:
  collapsing to "drive wins" is right for a diode-connected device but wrong
  for a feedback amplifier, and it printed `BUF_P … drives vbg0 senses -` -
  affirmatively denying the loop on a feedback buffer, and disabling
  `select_focus`'s reverse 1-hop from exactly the blocks the design doc's
  worked example starts from. `paths.supply_nets` holds the nets a **top-level**
  `V`/`I` touches, and `paths.roles_on(net)` is the reporting view over
  `net_blocks`: on a supply/stimulus net it drops the **`drive` role only**.
  The source drives that net, so no block can be its driver -
  `OPAMP2STAGE drives vdd,vss` was a false structural claim produced by
  two-terminal devices whose rail end is a `drive` terminal. A block *sensing*
  such a net is true and useful, so it stays: `OPAMP2STAGE senses vinn,vinp`.
  Level-0 rendering and focus seeding both go through `roles_on`, so a rail
  cannot seed a block that merely hangs a resistor on it, while a criterion
  measured on the stimulus net can still seed the block that senses it.
  Recognising a rail by *name* (`vdd`/`vss`/`gnd`/`0`) would be the forbidden
  guess; "a top-level independent source connects here" is a parsed fact.
- **`net_blocks` holds top-level nets only, and that bounds the reverse hop.**
  `signal_path.walk` drops a net that stops being a port at an intermediate
  definition, because pushing it outward under its local name would attach
  roles to an unrelated same-named top-level net. The consequence, documented
  in `select_focus`'s docstring: a seed block whose *input* comes from a net
  internal to a parent definition produces no upstream hop. On `benchmarks/
  bandgap` that is exactly `BUF_P` - its input `vt05` lives inside `BANDGAP`,
  so the design doc's worked example `vbg0 → BUF_P → upstream` does not reach
  the resistor ladder on that deck. The hop itself is correct and pinned
  synthetically; the deck offers no hop. What keeps `XRl1`/`XRl2` reachable
  there is the tuner prompt saying a folded block may still be named.
- `patterns.py` matches differential pairs, current mirrors, **stacked
  pairs** and Miller compensation. **Patterns never guess** - a match is a
  fact, a non-match is silence, and the acceptance bar is zero false
  positives, not recall. `stacked_pair` is deliberately not called `cascode`:
  a cascode, a source follower over a current sink and a power-gating switch
  have an identical local subgraph, and telling them apart means reading net
  names, which is the forbidden guess. Under a zero-false-positive bar the
  answer is silence or a truthful label, never a wrong label with a footnote -
  so the matcher states the connection (`M2.s == M1.d at mid, M2.g on ncas`)
  and leaves the naming to the LLM, which has the netlist. It is the only one
  of the three modules that can be wrong, which is why it is separate. See
  `docs/superpowers/specs/2026-07-27-netlist-structure-derivation-design.md`.
  **A model-name substring marker must lose to the refdes prefix.**
  `structure._classify_model` used to substring-match a component's model
  name against `_MODEL_CLASS_MARKERS` (`nfet`/`pfet`/`pnp`/`npn`/`res`/`cap`)
  unconditionally, while `area_limits._classify_ctype` already only consults
  those markers when `ctype == "X"` — an X-prefixed instance's positional
  value is a PDK primitive name, the one case where the prefix itself doesn't
  fix the device class. That inconsistency was flagged as a low-risk gap
  ("could false-positive on a model name containing res/cap") on the grounds
  that no benchmark deck exercised it. The first real production deck did,
  three times: a MOSFET used as a MOS capacitor is written `m3 nzero vssi
  nzero vssi UNITDEV_N_DEP_CAP …` — refdes `m`, model name containing `cap`. Old
  `structure.py` read the model name and set `device_class="cap"`, which put
  the device in both `patterns.py`'s cap list and its MOS list, and the
  Miller matcher paired `m3` with itself. Fixed by mirroring
  `_classify_ctype`'s rule exactly: `_classify_model` now returns `None`
  unless `ctype == "X"`, so a refdes prefix that already fixes the device
  class (`M`/`Q`/`R`/`C`/`L`/`D`) is never overridden by what the model name
  merely suggests. Confirmed zero change across all ten benchmark decks
  (every non-null `device_class` in every golden fixture is already on an
  `X`-prefixed component). Independently, `patterns.PatternMatch.__post_init__`
  now rejects any match whose `members` repeats a refdes — a structural
  invariant so a future matcher can't reopen the same self-pairing shape by
  a different route. This is the mirror image of the bandgap case already
  documented above ("every capacitor is an nfet or pfet MOS cap"): there, a
  sky130 MOS cap's model name contains `pfet`, so it is invisible to a
  hypothetical cap-name matcher; here, a MOS cap's model name contains `cap`,
  so it was wrongly visible to one. Same substring rule, opposite naming
  convention, opposite failure.
- **The prompt is focused; the gates never are.** `structure_view.py` picks
  the blocks reachable from the failing criteria's nets (via
  `control_block.py`, which resolves a measurement name to the nets its
  `meas`/`let` lines observe) and renders every block at one line, focused
  blocks in full, and the netlist itself with unfocused `.subckt` bodies
  folded away. `check_area_growth`, `check_refdes_resolution`,
  `check_param_applicability` and `check_stimulus_untouched` always read the
  whole deck, so a wrong focus costs relevance, never correctness. A proposal
  naming a block outside focus is logged as `focus_miss` - that is the signal
  the focus rule missed something. Benchmark decks are small enough that this
  path barely fires; it exists for real production decks of hundreds of lines,
  where the raw netlist no longer fits a context-limited model.
  **The tuner prompt must never restate the focus as a restriction.** Saying
  "only propose changes to parameters listed under tunable" turns the layered
  view back into a filter and deletes the answer whenever focus is wrong -
  e.g. `bandgap`'s `vbg0_min`/`vbg0_max` focus on `{BUF_P}` while the only fix
  (`XRl1`/`XRl2`) lives in the folded `BANDGAP` block. The prompt says the
  `tunable` line is what is *visible*, and that a folded block may still be
  named by its full path.
  **Focusing a nested definition means focusing its ancestors too.**
  `render_netlist` folds a nested definition together with its parent (a bare
  header inside a folded body is a fragment whose parent is unknowable), so a
  focus set holding `OUTER.INNER` but not `OUTER` elides the very block it
  focused. `select_focus` runs every path that adds a scope - seeds, the
  reverse hop, and already-touched refdes - through `_with_ancestors`. No
  benchmark deck nests subckts, so this is dead on every deck here and live on
  the first production one.
- **The testbench's own sources are not tunable, and it takes a gate to say
  so.** A top-level `V`/`I` is stimulus or supply by construction (an exact
  test - refdes prefix plus top-level scope - never a name heuristic like
  "vdd"). `structure.py` omits them from the tunable index, `structure_view.py`
  renders them under `stimulus (not tunable):` so the omission is visible, and
  `netlist.check_stimulus_untouched` rejects a change to one. All three share
  `netlist.is_top_level_stimulus`. The gate is not redundant with the address
  book: found by review, `{"refdes":"Vin","param":"value","new_value":"100"}`
  on `inverting_amp` passes area, refdes and param checks, rewrites the deck to
  `Vin in 0 AC 100`, lifts `gain_db` from ~20 dB to ~60 dB, and reports **PASS
  on an unmodified circuit** - `verify_post` has no reason to roll back a change
  that improved every criterion.
- **A tuning `param` must be able to apply.** `check_param_applicability`
  (in `netlist.py`, run right after `check_refdes_resolution`) rejects
  `param="value"` when the positional token is a model or subckt name, and
  rejects a named parameter that appears neither on the component's own line
  nor on any same-model peer in the deck. The peer rule is what keeps
  `Xq1.m` reachable in `benchmarks/bandgap` - `Xq1` writes no `m=` but
  `Xq8` writes `m=8`, and `m` is the only knob that sets the emitter-area
  ratio. Without this gate a proposal like `param="width"` appends
  `width=55`, changes the netlist, does not change the device, and burns an
  iteration on a rollback nobody can explain.
- **A deterministic gate and the LLM prompt behind it must agree, or the
  belt-and-braces instruction becomes a trap.** `verify_pre`'s prompt keeps a
  paragraph restating what the gates check - the same policy as when the refdes
  gate landed. But the param gate was *widened* by the peer rule and the prompt
  was not, so `verify_pre` was instructed to reject exactly the `Xq1.m` case the
  gate exists to admit. Three such rejections set `verify_pre_rejected_any` and
  hard-FAIL the run, skipping topology escalation - a wrong prompt is worse than
  no prompt, because it converts an approved proposal into a run-ending one. The
  same shape appeared at the netlist view: `verify_pre` was handed a folded deck
  while told to reject anything absent from it, fixed by rendering its view with
  the focus extended to the blocks the proposal names. **Whenever a gate's rule
  changes, re-read the prompt that mirrors it.**

Design docs (with full rationale) live in `docs/superpowers/specs/`, implementation
plans in `docs/superpowers/plans/`.

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

`spec.yaml` declares one or more testbenches (`testbenches:` list), each with
its own netlist file (resolved relative to the spec file), control block,
and criteria — there's no separate `--netlist` flag.

## Benchmarks

- `benchmarks/inverting_amp/` — ideal op-amp (VCVS), single criterion (gain),
  passes immediately with no tuning needed. The "golden path" smoke test.
- `benchmarks/two_stage_opamp/` — real transistor-level 2-stage CMOS op-amp
  (generic ngspice level-1 devices, no PDK needed), three criteria (DC gain,
  unity-gain bandwidth, phase margin) with a genuine trade-off: increasing the
  Miller compensation cap (`Cc`) improves phase margin but reduces UGBW. Starts
  with phase margin failing by design, so running this benchmark actually
  exercises the tune → verify → re-simulate loop instead of passing on the
  first iteration. See `docs/superpowers/specs/2026-07-25-two-stage-opamp-benchmark-design.md`
  for the full circuit rationale and verified Cc-sweep data.
- `benchmarks/two_stage_opamp/spec_topology_required.yaml` — same circuit,
  `phase_margin` threshold raised to 65° (vs the default spec's 60°). No
  value of `Cc` alone reaches 65° without `unity_gain_bandwidth` failing
  (verified in ngspice), which is what motivated **topology-swap tuning**
  (`analogcoder/topologies.py`): after enough repeated parameter-tuning
  rollbacks, the orchestrator swaps in a different pre-verified topology
  (`miller_nulling_resistor`, which adds a nulling resistor `Rz` in series
  with `Cc`) instead of continuing to tweak values. See
  `docs/superpowers/specs/2026-07-25-topology-swap-tuning-design.md` for the
  full design. **Caveat, found via a real run:** this spec doesn't reliably
  force the topology swap for a strong model — a live Claude run solved it
  in 2 iterations via `Cc`+`M6.W` together, a parameter combination outside
  the original Cc-only sweep, without ever attempting a swap. The
  topology-swap mechanism itself is still verified correct (unit tests, a
  real-ngspice test, and an independent whole-branch review all passed) —
  this benchmark just isn't a guaranteed trigger for it.
- `benchmarks/two_stage_opamp/spec.yaml` also declares two PSR testbenches
  (`psr_plus`, `psr_minus`) alongside `ac_loop_gain` — same `OPAMP2STAGE`
  subckt as `netlist.cir`, with the AC stimulus moved from the input to
  `Vdd`/`Vss` respectively (`netlist_psr_plus.cir` / `netlist_psr_minus.cir`).
  Verified against real ngspice: baseline `psr_plus_db=-15.12dB` (passes the
  `<=-10dB` threshold), `psr_minus_db=-3.36dB` (fails the `<=-8dB`
  threshold — `M6`'s NMOS source sits directly on `vss` with no cascode, so
  `Vss` ripple couples almost straight through). Increasing `M6.W` improves
  `psr_minus` but was shown to regress `phase_margin` and, combined with an
  `M7.W` change, `psr_plus` too — the real motivation for verifying every
  testbench together every iteration instead of just the one being tuned
  for. See `docs/superpowers/specs/2026-07-25-psr-verification-design.md`
  for the full Cc/M6.W/M7.W sweep data and a jointly-passing combination.

- `benchmarks/bandgap/` — five-block Kuijk bandgap reference chain
  (`BGR_CORE` + `ERRAMP` → `TRIMAMP` → resistor ladder → `BUF_N`/`BUF_P`),
  producing `vbg1`=1.2V and `vbg0`=0.5V. Unlike every other benchmark this one
  is **multi-block**, and its actual purpose is to measure whether the tuner
  changes the *correct* block. `spec.yaml` passes everywhere; the three
  `spec_seed_*.yaml` variants each tighten exactly one criterion whose only fix
  lives in one subckt, and each was verified both solvable and localised —
  e.g. growing `BUF_N.Xcl` does nothing for `vbg0_droop`, only `BUF_P.Xcl`
  does. `spec_seed_tc.yaml` is deliberately the coupled one: `Rp/R1` fixes TC
  but drags `vbgout` and `vbg1` with it. This benchmark is what made
  subckt-scoped refdes a prerequisite — four amplifiers in one netlist means
  refdes collision is the normal case.
  Every amplifier is a **folded cascode plus a common-source output stage**,
  matching the structure the target design uses. That is also what lets
  `spec_pvt.yaml` sweep 45 corners over the full ±10% supply axis: a plain
  mirror load leaves a 1.2V-common-mode input pair no saturation margin at
  1.62V (measured: trim loop gain −45dB, `vbg1` = 1.084V). Uses `pnp_05v5` and
  `res_high_po` alongside the FETs; every capacitor is an nfet or pfet MOS cap,
  never MiM. Full corner table and the design assumptions ngspice disproved:
  `docs/superpowers/specs/2026-07-26-bandgap-benchmark-and-scoped-refdes-design.md`
  ("Part 2 — as built" and "Part 2 revision").
  Both `spec.yaml` and `spec_pvt.yaml` also carry the `optimize:` block, and
  this is the benchmark the optimization phase was first verified on against
  real ngspice — `quiescent_current` sits at 212.99 µA against a 300 µA
  threshold on purpose. `spec_pvt.yaml` is the one that can optimize (see the
  measured-allowance and failed-confirmation entries under Architecture); the
  corner-less `spec.yaml` is pinned as the counter-case. The corner-anchored
  run costs **1790 s** — six 45-corner sweeps, because the confirmation fails
  and bisection runs — which makes
  `tests/unit/test_optimizer_bandgap_ngspice.py::test_the_optimizer_lowers_iq_while_every_criterion_still_passes`
  by far the longest test in the suite.
  `spec_corner_reduction.yaml` is the third corner-carrying copy: same
  testbenches and thresholds, a **9-corner** grid (tt/ss/ff × 1.62/1.8/1.98 V ×
  27 °C), a `corner_reduction:` block, and **no** `optimize:` block — it measures
  reduction and re-entry, and an optimizer search would make its runtime
  unpredictable. The 9-corner choice is measured, not arbitrary: a process-only
  3-corner grid seeded all 3 and reduced nothing, because the seed is bounded by
  `min(#criteria, #corners)` and this spec has 22 criteria (see "Corner
  reduction and re-entry" under Architecture). `spec_pvt.yaml`'s 45-corner grid
  is untouched.

Default backend is Claude (`--agent-backend claude`, the default — uses whatever
`claude` CLI auth is already configured, no env var needed). To run against a
local OpenAI-compatible server instead:

```bash
LOCAL_LLM_API_KEY=<token-or-dummy> .venv/bin/analogcoder \
  --spec ... \
  --agent-backend openai-compatible \
  --llm-base-url http://localhost:11434/v1 \
  --llm-model <model-name>
```

Verified working against a real local Ollama server (`qwen2.5:7b-instruct`) —
full pipeline (analyze → simulate → judge → tune → verify → re-simulate → pass)
including real tool calls, not just the no-tuning-needed happy path. (`analyze`
was the LLM analyzer agent at the time; it no longer exists — see
`structure.py` above — so a re-run today has one fewer LLM call in that
chain, not a different pipeline.)

On the harder `two_stage_opamp` benchmark, Claude converges to PASS in 3
iterations (correctly identifies that increasing `Cc` improves phase margin).
Ollama (`qwen2.5:7b-instruct`) ran the full 10-iteration budget and ended in a
clean `FAIL` (`max iterations reached`), not a crash — the pipeline mechanics
(schema validation, refdes/param checks, rollback on regression) all worked
correctly throughout. It failed because the model's own reasoning had the
trade-off backwards: it repeatedly *decreased* `Cc` believing that would help
phase margin, when this topology needs the opposite. Every bad proposal was
correctly rolled back by `verify_post`, so the run ends back at the safe
baseline netlist rather than a degraded one — this is a genuine model
reasoning/capability gap, not a pipeline defect.

`tests/integration/test_local_llm_backend.py` is skip-gated on `LOCAL_LLM_BASE_URL`
being set — it's the fastest way to re-verify the OpenAICompatibleBackend path
against a real server.

## Gotchas found by running, not by inspection

Every entry below cost a real run, a real sweep, or a review that reproduced
it. The section began as weak-model notes and outgrew that: most of it is now
SPICE/PDK behaviour and parser facts that bite any model.

### Weak (local) models and the agent loop

Found by actually running the pipeline against Ollama — worth reading before
assuming a weak-model failure is a code bug.

- **`response_format` + `tools` together breaks tool-calling on some
  OpenAI-compatible servers** (observed on Ollama): the model skips calling the
  tool and fabricates schema-shaped output instead. `OpenAICompatibleBackend`
  only sends `response_format` on turns where no tools are offered — don't
  "fix" this by sending it unconditionally.
- **The tuner needs the actual current netlist**, not just the cached
  structural analysis — it can't compute a concrete new value otherwise. It
  receives `netlist_text` directly (see `propose_tuning`'s signature).
- **`param` in a tuning change must be exactly `"value"`** for a component
  whose value is a plain positional token (e.g. `Rf vminus vout 10k`), or the
  exact `name` as it appears in an existing `name=value` token — anything else
  causes `netlist.py:apply_changes()` to silently append a no-op-looking
  `name=value` token instead of updating the component. `TUNER_SCHEMA` enforces
  this is at least a bare identifier via a regex pattern, and `verify_pre` is
  explicitly instructed to reject anything that doesn't match an existing
  netlist token — but a weak model can still get this wrong, so don't assume
  a proposal that passed schema validation is actually applicable.
  `check_param_applicability` (`netlist.py`) now blocks the `param="width"`
  silent no-op deterministically, before the proposal is ever applied — but
  the `verify_pre` instruction above is deliberately left in place as
  belt-and-braces, matching this file's existing pattern for
  `check_refdes_resolution`/`ValueError` (see below).
- **`netlist.py` tracks subckt scope.** A component inside a `.subckt` is
  addressable as `<SUBCKT>.<refdes>`; an unqualified refdes still works when
  it matches exactly one component netlist-wide, and raises `ValueError` when
  it is ambiguous rather than silently editing the first match. The scope is
  the subckt *definition*, so a change applies to every instance of it —
  two differently-tuned instances require two subckts. Nested subckts **are**
  scope-tracked: `Component.scope` and the `ParsedNetlist.subckts` key are
  dotted paths (`OUTER.INNER`), and a qualified refdes must match a path
  exactly — a partial path like `INNER.M1` is rejected rather than guessed at.
- **An unresolvable or ambiguous refdes is rejected deterministically before
  it ever reaches the tuner's proposal being applied.** `netlist.py`'s
  `check_refdes_resolution` runs in the orchestrator's tuning retry loop
  immediately after `check_area_growth` and before `agents.verify_pre` (same
  position/philosophy as the area gate — an unresolvable proposal never
  spends an LLM call). It rejects a proposed change's refdes when it matches
  no component (including a scope that names no subckt, e.g. `M1.W` meant to
  set `M1`'s `W` param but written in the refdes field instead) or, when
  unqualified, matches more than one scope — logged as a `refdes_check` event
  (distinct from `area_check`) in `history.jsonl`, with the rejection fed
  back to the tuner as retryable feedback like the area gate's. `apply_changes`
  itself still raises `ValueError` for the ambiguous case as a second line of
  defense; `run_orchestration`'s `except AgentExecutionError` (see below) also
  catches `ValueError` so that a `ValueError` reaching the apply step some
  other way still ends the run as a clean `FAIL` instead of an uncaught crash.
- **`param="value"` misapplied to a non-numeric positional token** (a
  transistor's model name, a subckt instance's subckt name) used to crash
  the whole run with an uncaught `ValueError` from `area_limits.py`'s
  `check_area_growth`, found by a final-branch review before it ever shipped
  to a real weak-model run. Fixed by treating an unparseable baseline value
  as "can't judge area impact, don't block on it" (skip that specific
  change), matching the existing philosophy for a missing baseline value —
  not by rejecting it, which would have been a different, larger behavior
  change. If you touch `check_area_growth`, keep this guard.
- Local models are noticeably more reliable at agents with **no tool calls**
  (tuner, verifier) than at tool-calling agents (simulator, judge). (Observed
  when the analyzer agent still existed; it's since been replaced by
  deterministic derivation — see `structure.py` above — so this comparison no
  longer has a third no-tool-call agent to include.)
  If a weak-model run fails, check which agent failed before assuming the
  whole pipeline is unreliable.
- If an agent's structured output still doesn't validate after retries,
  `orchestrator.py` catches `AgentExecutionError` and returns a clean
  `{"status": "FAIL", ...}` result instead of crashing — this is intentional
  (see `run_orchestration`'s try/except). Don't remove it. The same try also
  catches `ValueError` for the same reason (belt-and-braces against a
  `ValueError` from the netlist-apply path, e.g. an ambiguous refdes that
  somehow bypassed `check_refdes_resolution`) — don't remove that either.

### SPICE, sky130, and circuit physics

- **sky130 device models are binned and exceeding a bin is a hard error.**
  `wmax`/`lmax` are 100 µm. `W=120` aborts the run with `could not find a
  valid modelname` — not a warning, not a bad number.
- **`mult` on a sky130 `pnp_05v5` does nothing.** It scales only the model's
  mismatch terms, which are zero without Monte Carlo. The emitter-area ratio a
  bandgap needs is the *instance* multiplier `m=8`; `mult=8` yields ΔVbe ≈ 0
  and a core with no PTAT current at all, silently.
- **An nfet MOS cap cannot float** — its body is the p-substrate, so one plate
  is pinned to `vss`. A Miller cap, which must sit between two signal nodes,
  has to be a *pfet* MOS cap (isolated nwell body).
- **The first line of a SPICE deck is the title.** A `.temp` placed there is
  silently consumed and the run happens at 27 °C, producing corner data that
  looks plausible and is wrong.
- **`Lfb`/`Cin` loop breaking does not survive a cascode.** A 1 MH inductor is
  only a 6.3 MΩ open at 1 Hz, so against a folded cascode's tens of megohms the
  loop is never actually broken; raising it to 1 GH spans the matrix over ~20
  decades and the solver returns garbage at many corners. Where the break point
  drives a MOS gate, use series voltage injection (`DC 0 AC 1`) and read the
  loop gain as `vdb(out)-vdb(in)` — exact, with no reactive elements. See
  `benchmarks/bandgap/netlist_loops.cir`.
- **A cascoded amp with a CS output stage can latch itself off.** If the bias
  chain collapses with the core, every CS stage's NMOS sink turns off while its
  PMOS is fully on, pinning each amp output HIGH; a startup pull-down then has
  to outfight a much larger PMOS and loses. Keep a trickle current in the bias
  chain (`BGR_CORE.Xsu_b`).
- **`.option scale` must be declared in the netlist itself, not only in an
  include.** `parse_netlist` never follows includes, so a deck that gets its
  scale from `pdk_corner.inc` alone reads `W=30` as thirty *metres* — which is
  exactly how the area gate's size tiers came to be inert on every PDK-backed
  benchmark.

### Netlist parsing, parameters, and the area gate

- **An inline `$` or `;` comment is stripped before parsing, and re-appended
  by `apply_changes`.** Leaving it in used to swallow the model name into the
  node list, and made `param="value"` replace the comment's last word instead
  of the device value.
- **A parameterised value is resolved before the area gate reads it**
  (`params.py`). Without this, `W='wn*2'` was unparseable, and
  `check_area_growth`'s "cannot judge, do not block" fallback fired on every
  device — so the gate was absent on any parameterised deck. The resolver's
  subset is deliberately narrow (arithmetic only); anything else resolves to
  `None` and takes that same fallback, which is now reached only when it is
  genuinely true.
- **Tokenise a SPICE line with `netlist.split_tokens`, never `str.split()`.**
  `split_tokens` keeps `'...'` and `{...}` whole, so `W='wn * 2'` stays one
  token. Plain `.split()` turned it into `W='wn`, `*`, `2'`, which pushed the
  model name into the node list and made `value` become `2'` — the same shape
  as the `$`-comment bug, silently wrong device class and area tier — and made
  `apply_changes` rewrite `W='wn * 2'` → `W=50` as `W=50 * 2'`. Every gate
  passed the corrupted deck (`check_refdes_resolution` resolved fine,
  `check_area_growth` saw an unresolvable baseline and did not block), so it
  reached ngspice and the failure looked like a bad tuning proposal rather
  than a parser bug. Every tokenisation site in `netlist.py` routes through the
  helper, and `params.py` imports it rather than splitting its own — adding one
  that uses `.split()` reopens this. `{...}` nests (`W={wn * {m + 1} }`);
  `'...'` does not.
- **Fold `+` continuations with `netlist.logical_lines` before reading a deck
  line-by-line.** It returns `(code, [physical line indices])`, so parsing sees
  the joined statement while `apply_changes` still edits the physical line the
  token actually sits on. Treating a `+` line as its own statement produced a
  bogus component with refdes `+` that *stole* the real device's parameters —
  leaving `M1` with `params={}` and therefore no area-gate baseline — and made
  `apply_changes` append `W=99` to the first line while `W=10` stayed on the
  continuation, so the deck carried `W` twice.
- **A parameter's scope decides whether it resolves, and a contested name
  resolves to nothing.** `params.py` collects `.param` at any depth and
  attributes it to the enclosing subckt path. Precedence is global < subckt
  body `.param` < `.subckt`-line default < instance override. When a name is
  declared both in the body and on the `.subckt` line — or when instances
  disagree on it — it is dropped *and* masked from the global environment, so
  the caller sees "unknown" rather than a global value standing in for a local
  one.
- **`m` multiplies area, `nf` does not.** `m` is a multiplicity — a count of
  parallel devices, so `w=2u m=2` is two 2 µm devices, total width 4 µm. `nf`
  is the number of fingers: `w=2u nf=2` is ONE device of total width 2 µm split
  into two 1 µm fingers, so total width and area do not change, and the shared
  source/drain diffusions make more fingers area-neutral to slightly
  favourable. Tuning `nf` is usually meaningless. So the gate gives `w`/`l` the
  size-graded geometry tiers, `m` a **flat 2.0×** (`COUNT_ALLOWED_MULTIPLIER` —
  a count, not a length, same reasoning as `pnp_05v5`), and `nf` **no tier at
  all**. That last one is "nothing to judge", not "cannot judge" — do not look
  at an unconstrained `nf` and "fix" it by adding a tier. In the production flow
  NMOS/PMOS widths are fixed and `m` is varied per instance, so the flat count
  tier is the constraint that actually binds in practice; the 25 µm/50 µm
  geometry boundaries (chosen for sky130's `W`) rarely will. `m` and `nf` are
  counts, so a non-integral proposal (`m=6.5`) is rejected outright — the
  schema only requires a numeric string, and with `m` as the primary knob that
  path is reachable.
- **Total width is `w × m`, so the area gate evaluates the product per physical
  device, not each parameter alone.** Changes in one proposal are grouped by
  the device they reach and their ratios multiplied; the allowed multiplier is
  the *tightest* tier among the parameters involved (geometry tier keyed on the
  baseline `w × m`, 2.0 when `m` is involved, `nf` excluded from the product
  entirely). Without this, one proposal growing `w` 3× (allowed) and `m` 2×
  (allowed) grew total width **6×** and nobody looked. Grouping is per *device*,
  so a wrapper cell's `wn` reaching both `ma1` and `mb1` is checked against
  each one's own baseline. The tier is a **per-device growth ratio, not a
  total-area budget**, so a group must identify one *physical* device. The key
  therefore carries the intermediate instance chain (`TracedTarget.chain`), not
  just the definition component: a wrapper instantiating the same unit cell
  twice (`xl1`/`xl2` → `LEAF.ma1`) returns two targets holding the **same
  `Component` object**, and without the chain their one shared ratio was
  multiplied twice — a legitimate 2.5× was reported and fed back to the tuner
  as 6.25×. **`ratio^N` is not a quantity at all**, and it was never a
  conservative reading of a total-area budget: if both devices grow 2.5×, the
  per-device ratio is 2.5× *and* the total-area ratio is 2.5× (2·2.5A / 2·A).
  What N multiplies is the absolute increment, not any ratio — so under either
  reading, per-device or total, the answer is 2.5×, and the code lands on the
  only defensible number. Do not "restore" the squaring thinking it was the
  safe choice.
- **`m` multiplies the tier baseline for every device class except `Q`.** It
  is a count of parallel devices, so a MiM cap with `m=4` occupies four times
  the area of one — tiering it on the single-unit `w` handed it the loosest
  tier. `Q` is the exception because its `m` *is* the tier key (emitter-area
  ratio); multiplying there would double-count.
- **An instance parameter can also reach a device's positional value.** `R`/`C`
  size knobs are positional (`R1 a b rv`), not `name=value`, which is why
  `RESISTOR_TIERS`/`CAPACITOR_TIERS` are keyed on the value. Tracing only
  `device.params` left a *wrapped* resistor unbounded while the identical bare
  one was blocked — the same 1000× growth decided by whether the designer
  wrapped it. `params._positional_target` accepts the positional value only
  when it is a **bare identifier** matching the parameter name; an expression
  like `{rv*2}` is refused, because assuming the parameter's ratio equals the
  device's is exactly the guess this layer forbids.
- **The trace needs the wrapper cell's definition *in the deck*, and an
  wrapper cell library normally arrives as an `.include`.** `parse_netlist`
  never follows includes (deliberate — see the `.option scale` note above), so
  `xwrap1 … WRAPCELL_A_LVT wn=2e-6` against an include-only definition has no
  traceable target at all and the gate is fully inert for it: `wn` 2 µm → 2 mm
  passes. This is *not* fixed by making the parser follow includes. Instead the
  blindness is recorded: `check_area_growth`'s richer sibling
  `evaluate_area_growth` returns a per-change **visibility state**, logged in
  `history.jsonl`'s `area_check` event as `states`. The four states are
  different facts and must stay distinguishable — `bounded` (a tier applied),
  `neutral` (nothing to bound: `nf`), `blind` (the component instantiates a
  subckt this deck does not define, so no trace is possible;
  `Component.undefined_subckt`), `unjudged` (a value could not be resolved).
  This gate had by then been silently inert twice, neither time visible in a
  run log; it has happened four times now (the running count is under
  `optimizer.py`/`area.py` in Architecture). A sky130 primitive is `bounded`,
  not `blind` — it is classified by its model name and tiered on geometry.
- **Per-instance parameter resolution is a different tool from
  `build_param_envs`.** The latter resolves per subckt *definition* and
  deliberately drops any name the instances disagree on — and in wrapper-cell
  decks disagreement is the normal case (the same cell instantiated with
  `ma1=4/1/2`), so it returns `None` exactly where a number is needed.
  `params._instance_env` resolves for one instance: that instance's own
  override → the `.subckt` line default → a literal in the body, with the
  override's own expression evaluated in the *outer* scope. It **applies the
  same shadowing rule** as `build_param_envs`: a name declared both in the
  subckt body and on the `.subckt` line resolves to nothing. Narrowing to one
  instance removes the "which instance?" ambiguity but not the dialect
  ambiguity, so the two resolvers must not disagree — they did, and the gate
  acted on the one that guessed (it picked the `.subckt`-line 10 µm over the
  body's 60 µm, which chose a 3.0× tier instead of 1.5×). An explicit instance
  override still wins over a contested name, matching
  `build_param_envs`' `shadowed -= set(agreed)`. Tracing follows a
  body token into a nested instance, bounded by `_MAX_TRACE_DEPTH`, and falls
  back to "cannot judge, do not block" rather than guessing. A subckt the deck
  does not define (any PDK primitive — `parse_netlist` never follows includes)
  is a leaf, not a dead end.

## Testing conventions

- TDD throughout; every module has a paired test file in `tests/unit/`.
- Agent tests mock `run_agent`/`AgentBackend`, never hit a real LLM.
- `tests/integration/` holds two skip-gated real-backend tests (`ANTHROPIC_API_KEY`
  for Claude, `LOCAL_LLM_BASE_URL` for local) — skipped by default, meant to be
  run manually when you have real credentials/a real server available.
- `tests/unit/*_ngspice.py` assume `ngspice` is on PATH rather than skipping,
  and all but two finish in seconds. The long one is
  `test_optimizer_bandgap_ngspice.py`'s corner-anchored case at ~30 min (six
  45-corner sweeps); deselect it
  (`--deselect …::test_the_optimizer_lowers_iq_while_every_criterion_still_passes`)
  for a normal TDD cycle and run it before merging anything under
  `optimizer.py`, `area.py`, `pvt.py` or `judge_tools.py`. The other is
  `test_corner_reduction_bandgap_ngspice.py` at **129 s measured**, dominated by
  two 9-corner × 5-testbench sweeps (~57 s each) shared through module-scoped
  fixtures.
- **`pytest -m "not slow"` is the normal TDD cycle (~45 s).** Both files above
  carry the `slow` marker, registered in `pyproject.toml` — per-test on the
  optimizer case (the rest of that file is fast) and file-wide via `pytestmark`
  on the corner-reduction one. A plain `pytest -q` is ~3 min (corner reduction
  included, optimizer case still deselected by node id) and ~33 min with
  everything. Before this marker existed `pytest -q` had silently gone 45 s →
  3 min with no documented way to opt out.
