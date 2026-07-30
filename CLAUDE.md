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

- `topologies.py` / `topology_match.py` — a small curated library of
  pre-verified amplifier topologies the orchestrator can swap in as a last
  resort after repeated parameter-tuning rollbacks
  (`TOPOLOGY_SWITCH_THRESHOLD`), instead of only ever changing existing
  component values. Four entries, each declaring the `ports` its body requires
  and the `assumes_scale` its geometry is written in. See "Benchmarks" below.
  **The swap used to be gated on `len(subckts) == 1`, which meant it was live
  on exactly one benchmark and structurally dead everywhere else** — `bandgap`
  has 6 definitions, the production decks have 21 — and the fact that it was
  off reached no log. `topology_match.compatible_swaps` replaces that: it
  judges each `(block_path, topology_id)` pair on four rules and is used as a
  **candidate generator, not a gate**, so the agent is offered only pairs that
  can actually be applied. `tried` is a set of *pairs* — trying an entry on
  `BUF_N` says nothing about `BUF_P`.
  The rules are ports (**one-directional subset**, order ignored: the body's
  ports must all exist on the block, and the block's *leftover* ports are then
  judged separately by `_leftover_ports_float_reason`. This entry used to say
  "bidirectional set equality" and call the subset check a defect — that was
  backwards and stale. `bc53d9e` relaxed equality to a subset **on purpose**,
  because equality rejects a body that is a legitimate drop-in for a block
  carrying extra bias ports; the floating-node danger the old text described is
  real but is handled by the leftover guard, not by the port rule. Do not
  "restore" equality), models (the body's model
  names must already appear somewhere in the deck — decidable without
  following `.include`, because a deck that instantiates a model has whatever
  provides it), `.option scale`, and `identical_body`. A candidate must be
  compatible in **every** testbench that is versioned together
  (`missing_in_testbench`), which closes the non-canonical-deck `ValueError`
  hole for this path — `push_netlist_version` is atomic, so a swap applied to
  only some decks would have `judge` merging measurements from two different
  circuits.
  **`identical_body` is judged against the *current* deck, so it is not a
  static property of an entry.** `miller_basic` is byte-identical to
  `OPAMP2STAGE`'s own body, so `two_stage_opamp` offers **1** candidate, not
  2 — and after a swap to `miller_nulling_resistor` the roles invert and
  `miller_basic` becomes a candidate again. Likewise
  `folded_cascode_nmos_in_cs` ≡ `TRIMAMP` and `folded_cascode_pmos_in_cs` ≡
  the shipped `BUF_P`, taking bandgap from 8 raw pairs to **6** candidates.
  Without this rule the agent can pick a swap that changes nothing, spending
  an outer iteration and resetting `consecutive_rollbacks` — delaying the very
  escalation that triggered it.
- **A failed escalation must never be worse than not escalating.** When the
  topology proposal loop exhausts its retries, the run does **not** end: it
  logs `topology_unavailable` with a reason, resets `consecutive_rollbacks`,
  and falls through to parameter tuning — the same policy the area gate
  already had ("exhausting all retries on area rejection alone is treated like
  a parameter-tuning rollback"). This is not hypothetical tidying. `block_path`
  is deliberately **not** in `TOPOLOGY_SCHEMA`'s `required` (a required field a
  weak model omits hard-FAILs every spec — this repo hit that with
  `control_block`), and the orchestrator can only resolve an omitted one when a
  single candidate carries that `topology_id`. On bandgap that is *never* true
  — 3 to 4 blocks share each entry — so omission was always ambiguous and the
  old code returned `FAIL` after three retries. Measured on
  `spec_seed_topology.yaml` with real ngspice: with `block_path` supplied,
  PASS at iteration 4, `buf0_gain_db` 100.158; with it omitted, **FAIL at
  iteration 4 with the deck back at `netlist_v0`**, six outer iterations and a
  working tuning path thrown away. The mitigation the optional field bought was
  void exactly where it was needed. It now ends `max iterations reached` at
  81.643 dB, having tuned in the same iteration the proposal failed.
  **The prompt requires `block_path` while the schema does not, and that
  asymmetry is deliberate** — prompt stricter than gate is the safe direction.
  Do not "fix the inconsistency" by relaxing the prompt or by adding the field
  to `required`.
- **`topology_unavailable` carries a reason code, and that is the point.**
  `no_subckt_definitions`, `empty_library`, `all_pairs_already_tried`,
  `all_pairs_rejected`, `proposal_unresolved`. Without it, a deck with no
  `.subckt` at all, an exhausted library, and *a deleted check* produced
  byte-identical history — silently-inert-gate shape #6 in this repo, and the
  same argument `optimize_guard_infeasible` already settled. Known precision
  limit: `all_pairs_already_tried` requires *every* rejection to be
  `already_tried`, and a real multi-block deck always also carries `ports` and
  `identical_body` rejections (measured on `spec_corner_reduction.yaml`:
  `ports` 80, `identical_body` 10, `already_tried` 6), so genuine exhaustion
  there reports `all_pairs_rejected`. Literally true, and the finer fact
  survives per-pair in the `topology_candidates` event.
- **The result must describe the deck it returns — again.** `result.json`
  carries `topology_swaps` (always present, empty when none) and `report.md`
  grows a Topology section, because a swap replaces an entire block body and
  the run otherwise reported PASS beside a 16-device structural change without
  a word. This is the same shape as the optimization phase's
  `final_criteria`/`final_netlist_paths` mismatch, and it recurred **twice on
  one branch**: `cli.py`'s corner-reduction re-entry overwrites `result`
  wholesale while explicitly carrying the *deck* forward
  (`state.current_netlist_texts()`), so a swap kept in attempt 0 vanished from
  the report while its `Rz` was still in the returned netlist. Swaps are
  accumulated across attempts and each record carries an `attempt` index —
  `outer_iter` restarts per attempt, and `tried` resets too, so one block can
  legitimately be swapped in more than one attempt.
- **The area gate's baseline is `netlist_v0` and is deliberately never
  refreshed after a swap** — swapped-in components have nothing in the original
  to compare against. The `topology_swap` event therefore logs *two* lists:
  `unconstrained_refdes` (no baseline entry at all) and `stale_baseline_refdes`
  (has one, but the parameters differ, so it is bounded against geometry that
  is no longer its own). The second exists because the first alone was
  misleading: on four of bandgap's six candidates `unconstrained_refdes` is
  `[]`, which reads as "the gate is intact here" while ~14 of 15 devices are
  tiered against the previous topology's geometry. Concretely, post-swap
  `BUF_P.Xt` is `W=24` against a baseline of `W=8`, so a proposal of `W=48` is
  scored 6.0× instead of its true 2.0×. That direction is conservative for the
  current four entries, but that is an accident of these bodies, not a property
  of the rule.
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

### Topology curation — how a new library entry gets in

- `curation.py` / `agents/curator.py` / `agents/variant_author.py` /
  `cli_curate.py` — a second console script, `analogcoder-curate`, that decides
  whether a candidate topology earns a place in `TOPOLOGY_LIBRARY`. It **never
  writes the library**: it emits `curation_report.md`, `curation.json` and a
  `topology_candidate.py` snippet, and a human commits. "Pre-verified" has to
  keep meaning "a person read the measured evidence", or the library's only
  value is gone.
- **A library entry exists for exactly one reason: to reach where parameter
  tuning cannot.** That is why the gate's centre is not "does it simulate and
  reproduce its numbers" — the cascode-compensation candidate F1 rejected would
  have passed that. Submission is `(candidate, verification slot)`, because
  "better" needs an incumbent to be better *than*. Stages: structure (reuses
  `compatible_swaps`) → characteristic reproduction (measures candidate **and**
  incumbent; `addresses` is **measured** here, never declared) → corner
  verification → scoped comparison. Verdicts are `ADMIT`/`REJECT`/
  `INCONCLUSIVE`; a crash at any stage still writes all three artifacts.
- **Corner verification is required for `provenance == "authored"` only, and
  that asymmetry is the entire licence for letting an LLM write SPICE here.**
  The repo forbids the *tuner* from authoring structure because its proposals
  reach the deck with only text gates in between — the circuit's behaviour
  appears only after it is applied. Curation is the opposite: a three-stage
  simulation gate, a corner sweep, and a human commit sit in between, and the
  authoring is a *local modification of an already-sized working block*, not a
  blank page. The danger was never authoring; it was **application without
  verification**. Two sweeps only (candidate, incumbent) — knobs are never
  swept at corners.
- **The scoped comparison sweeps one knob at a time and says what it looked
  at.** Knobs, ranges, point counts, simulation totals, omitted knobs,
  unresolved knobs and excluded points all land in the record. It never claims
  to have excluded all tuning: this repo has twice found the winning fix in a
  knob combination no single-knob sweep tried.
- **Zero-tolerance Pareto cannot reject on a real multi-block slot, and it
  manufactures claims out of solver noise. Both were measured on the same
  run.** On `benchmarks/bandgap/spec_curate_slot.yaml` (8 criteria over four
  amps sharing bias rails), 30 knobs × 5 points = 120 simulations: the Ahuja
  candidate — this project's own proof case — came out **ADMIT** with
  `dominating: None`, because two criteria physically decoupled from the swept
  knob sat `0.0011°` and `0.0001 dB` short at *every* point. The same
  strictness in the other direction turned `+0.0028°` and `+0.0001 dB` into
  *measured* `addresses`, which `agents/tuner.py` renders straight into the
  swap prompt — defeating the "no unverified claim reaches the tuner" rule via
  noise rather than via an agent.
  `COMPARISON_REL_TOLERANCE = 1e-3` fixes both, and **the value is arithmetic,
  not a round number**: the largest measured noise is `0.0028/66.08 = 4.2e-5`
  and the real improvement is `8.3/81.14 = 0.102`, so 1e-3 sits ~24× above the
  noise and ~100× below the signal. Applied symmetrically to `_is_better` (an
  improvement must exceed it) and `_at_least_as_good` (a near-tie is a tie).
  Set it to 0 and the shipped run ADMITs again — that counter-run is pinned.
  This is the guard-band lesson a second time: a guessed ratio fails, a
  measured one works.
- **The cheapest tuning is changing nothing, and the gate excluded it.**
  `_sweep_values` dropped the baseline point because stage 2 had already
  measured it — and stage 2's measurement was then never passed on. So a
  candidate strictly worse than the incumbent on every criterion was
  **ADMITted**. The incumbent now enters the Pareto test as a labelled
  zero-cost point (`point="incumbent"`, `knob`/`swept_value` `None`,
  `simulated_here: False`). Not a contrived shape: it needs only a knob whose
  baseline sits near an optimum, which is what a shipped design *is*.
- **A count-based knob cap is order-dependent, and the default made the gate
  blind.** `--max-knobs 8` truncated alphabetically; `TRIMAMP.XRz.l` is the
  9th of 30, so the one knob that decides the case was never swept and the
  shipped run ADMITted after 16 simulations. The cap is now opt-in. Curation is
  offline and once per candidate, so the honest default is "sweep everything":
  measured **120 simulations / 2 min 41 s** on a single-testbench slot (the
  estimator's 150 is an upper bound). A 5-testbench slot is ~12 min — the same
  testbenches-outside multiplication corner reduction already paid for, which
  is why single-testbench validation slots are the documented recommendation.
- **`verified_at` is a property of (body × slot), not of the SPICE text.**
  `BUF_P`'s 45-corner PASS was earned inside its original surrounding circuit;
  proposing that body for another slot is a new, unverified pairing. So
  extracted and file candidates ship `verified_at="nominal"` and writing
  `"corners"` would be the misrepresentation. When this was found the fix was
  to the *report*, which printed `nominal` beside a line asserting the source
  had passed a full sweep.
- **Only source B used to check that a declared port is referenced by the
  body.** Sources A and C copied the header verbatim, and the justification —
  "stage 1 judges port compatibility anyway" — was false: the ports handed to
  stage 1 are the block's own, so the subset test is an identity, `leftover
  ports` is empty and F1's floating-net check never runs. An entry whose
  `ports` overstates its body makes that check vacuous for every future match.
  All three sources now share `reject_unreferenced_ports`. It reads
  `component.nodes` only, so a port referenced solely inside a behavioural
  expression (`B1 out 0 V=V(nbias)*2`) is reported unreferenced — it fails
  *closed*, and zero shipped blocks trip it.
- **The area gate never blocks shrinking** (`evaluate_area_growth` short-
  circuits on `ratio <= 1.0`). Any code describing a symmetric
  `[baseline/M, baseline*M]` window as "the area gate's allowance" is wrong on
  the low half; the curation sweep's lower bound is self-imposed and says so.

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
  **The running total is twelve.** #11 is `scripts/search_ab.py`'s corner
  regime (caught in review, never shipped — see "Corner reduction and re-entry")
  and **#12 is `corner_render` never reaching either full sweep**, described
  under the corner renderer in that same section. #12 is the first one found by
  a *measurement run* rather than by review, and the first where the missing
  record covers the sweep that decides the verdict.
  Curation added three more, all of them a gate
  that *passed* without being able to fail: `tunable_range` taking the direct
  branch where the judging path takes the traced one (so every geometry knob on
  a wrapper-cell deck vanished while the record blamed the area gate), the
  zero-tolerance Pareto that could not reject on a coupled multi-block slot,
  and the incumbent's own point being excluded from that same comparison. Ask
  "what does this look like in the log when the gate does nothing?" while
  writing the gate, not after. The six before them came from the topology-swap
  branch: `unconstrained_refdes` logging `[]` while the devices it covered were
  tiered against the previous topology's geometry (#5), and
  `topology_unavailable` carrying no reason, so "no `.subckt` in this deck",
  "library exhausted" and "someone deleted the check" were byte-identical (#6).
  Both are described under "Spec, topologies, and the area gate" above. Six
  occurrences is no longer a run of bad luck — treat "what does this log look
  like when the gate does nothing?" as part of writing the gate.
  **#10 is `render_corner_netlist`'s supply rewrite, and it is the first one
  that made a *measurement* mean nothing rather than a check.** The
  substitution required a literal `DC` token, `benchmarks/bandgap/
  netlist_startup.cir` writes `Vdd vdd 0 PWL(0 0 100n 1.8 1 1.8)`, and
  `re.sub` returns the input unchanged on zero matches — so the voltage axis
  of that testbench was dead in both corner-carrying specs and nothing said
  so. Described in full under "Corner reduction and re-entry" below. The
  lesson that generalises past this file: **`re.sub`/`str.replace` are silent
  by construction.** Any rewrite this repo *asks for* must be counted
  (`re.subn`) and its result recorded, in exactly the way a gate's inert state
  is.
  **The same shape reaches measurements, and the count deliberately does not
  absorb that.** D1's repeat-proposal-rate metric produced `0.000` on its
  baseline run because that run had **zero** rollbacks and zero rejections, and
  the metric only fires on a `(refdes, param)` that already failed — so `0.000`
  was the only value it could return (see "D1's claim was measured" under
  "Deterministic netlist derivation" — the re-measurement that fixed this is
  the paired probe described there). That is this defect class applied to a
  metric rather than a gate. It is **not** counted here and the total stays at
  **ten**: this ledger enumerates gates, and folding a metric in would make the
  number mean two things. The transferable rule is the question, not the count —
  for a gate ask "what does the log look like when it does nothing?", for a
  metric ask **"was the condition under which this could return a different
  answer present in the runs I measured?"**, and ask it while choosing the runs.
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
  because they only read the canonical text). The guard rolls back to the
  version the phase started from (an unconfirmed pushed version must never be
  what the run returns), logs `optimize_failed`, and returns a well-formed
  `UNCHANGED`.
  **`OSError` is the third, and it was wrong to describe it as this phase's
  alone.** This entry used to justify the narrower `run_orchestration` guard
  with "bisection re-reads a version deck from disk (`_texts_at`) …
  `run_orchestration` never re-reads, so it has no such case". The second half
  is false and always was: `orchestrator.py:250` calls
  `state.current_netlist_texts()` at the head of **every outer iteration**, and
  that is `open(path).read()` (`state.py`). Reproduced with mock agents — roll
  back to v0, let the file vanish (tmp reaper, NFS reconnect), and the next
  iteration's head read raised `FileNotFoundError` past both excepts, so
  `_final_result` never ran and the run ended with no `result.json` and no
  `report.md`: the exact outcome the optimization guard exists to prevent, in
  the phase that costs ~103 min to reach. `run_orchestration` now catches
  `OSError` too, with a **distinct** failure reason ("the run could not read or
  write its own files") — "the netlist-apply path failed" and "the run could
  not read its own deck" send the next reader to different places. **The
  handler must not call `state.log_event`**: the same broken disk would fault
  the handler and the guard would buy nothing. `_final_result` is safe there
  because `current_netlist_paths()` reads only the in-memory version list.
  One surface is deliberately left outside: `push_netlist_version`
  (`orchestrator.py:182`) sits *before* the `try`, so an unwritable run-dir
  still escapes. Pulling it inside is **not** the fix — the guard's whole
  purpose is to still write `result.json`/`report.md` into that same directory,
  `RunState.__post_init__` already `makedirs` it, and `entry_netlist_paths`
  would need a placeholder before the `try` that the checkpoint snapshot would
  then carry, trading a clean crash for a corrupt resume.
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
- **Fourth recurrence of the same rule, and the only one caught in a real
  stored artifact: `report.md` drew no line of the sweep that decides the
  verdict.** `report.py` had no `pvt_sweep` string at all. Re-rendering
  `runs/pvt_sonnet_1/result.json` with the old code printed **`**Status:**
  FAIL` above seven criteria all marked `[PASS]`** — zero `[FAIL]` lines, zero
  occurrences of "corner", zero worst-corner coordinates — while the same
  file's `pvt_sweep` failed all seven, `dc_gain` collapsing 71.09 → **3.14 dB**
  at `fs/1.98/125.0`. A reader of the report alone is left with "every
  criterion passed, so why FAIL?". `_pvt_lines` now draws it, **including when
  it passes**: the silence-means-did-not-run rule that the optimization /
  corner-reduction / topology sections follow applies to the **key's absence**,
  never to the value — folding `overall_pass` into it would make "the sweep
  passed" and "the sweep never ran" the same silence.
  **The deeper half was that "Final criteria" never said what it measured.**
  There are three possible provenances and no way to tell them apart from the
  report: the mid-loop LLM `judge` on one unrendered deck point; the same judge
  on the **worst value across the reduced corner set** when `corner_reduction`
  is active (`corner_sim.build_corner_simulate`); or `evaluate_criteria` on the
  version bisection landed on, because `cli.py` **overwrites** the key with
  `optimization["final_criteria"]`. The heading now carries both axes, derived
  from `corner_reduction.active` and `optimization.final_criteria` — the only
  two facts in `result` that decide it. A per-criterion detail worth keeping:
  a `worst_case_corners` entry whose `value` is `None` is **not an argmax** —
  `worst_case_measurements` writes `missing_corners[0]`, the first corner where
  the measurement was absent — so the report says "no measurement at corner X",
  not "worst at X". Calling it a worst case is the `OPAMP2STAGE drives
  vdd,vss` error shape.
- **`result.json` and `history.jsonl` were not RFC 8259 JSON, and the danger
  was silent value corruption, not a parse error.** Both wrote bare `NaN` /
  `-Infinity` (no `allow_nan` argument), and both reach that on the **normal**
  path: `judge_tools.evaluate_criteria` puts `math.nan` in `actual`/`margin`
  for a criterion whose measurement is missing, and `pvt.corner_severity`
  returns `-math.inf` — whose own docstring calls "some corner where the AC
  response never crosses 0 dB so ugbw does not come out" a normal case.
  Measured: `runs/pvt_sonnet_1/result.json` carries **8** literal `NaN`s; node's
  `JSON.parse` rejects the whole file, and **`jq` 1.7.1 does not reject — it
  rewrites `NaN` to `null` and `-Infinity` to `-1.797e308`**, handing a
  consumer a number no simulation ever produced. It shows up only on runs that
  broke at corners, i.e. exactly the runs a human opens. `analogcoder/json_io.py`
  is now the one place: `json_safe` (string markers) + `allow_nan=False`, in
  that order — **normalisation first**, because `allow_nan=False` alone raises
  `ValueError` inside `write_result_json` and takes `write_report_md` down with
  it, the same shape as the optimization crash that lost both artifacts.
  `null` is *not* the encoding: `null` means "no value in this field" and `NaN`
  means "measured, and no value came out" — two different facts this repo has
  paid for keeping apart. The marker is a **wire format**, so `history.read_events`
  (the single reader of `history.jsonl`) restores it with `restore_non_finite`;
  without that, `scripts/paired_tuner_probe.py` subtracting judge values through
  `attempt_log.deltas_between` would `TypeError`. `cli_curate.py` had solved
  this for its own artifacts first, with the rationale in a comment — the
  control case was inside the repo while the main line stayed broken.
  Known limits, recorded rather than fixed: `checkpoint.py` still serialises a
  `judge_result` the same way, and a genuine string whose value is exactly
  `"NaN"` would be restored as a float (no such field exists today).

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
  were set. **`corner_fields` writes `{"corner_id": None}` — an absent
  identity, not the string `"(deck)"`.** This entry used to say
  `pvt._corner_fields` "reports it as `(deck)`", which was true before the
  corner-identity change and is now wrong in a way that matters: putting a
  *name* where a coordinate goes leaves a reader of the artifact seeing
  `"(deck)"` in the slot `ss` sits in, with no way to know it is not one. The
  human-readable `"(deck)"` is `corner_selection.raw_label`'s job, and that
  function is the rule's owner — do not re-derive the dict shape at a call
  site (a slow test did, and rotted).
  `corner_selection._as_point` **rejects** that shape — detected by the *absence*
  of voltage/temperature coordinates, never by matching the string `"(deck)"`.
  Without the rejection a `CornerPoint(process="(deck)", ...)` reaches
  `render_corner_netlist`, which writes `.include ".../pdk_corner_(deck).inc"`
  and hands ngspice a file that does not exist: "this point has no coordinates"
  silently becoming a coordinate.
- **A rewrite nobody counted is a corner nobody ran, and the comment two lines
  above already said so.** `render_corner_netlist` performs three rewrites, and
  the supply one used to be `re.sub(r"^(Vdd\s+\S+\s+\S+\s+DC\s+)\S+", ...)`.
  `benchmarks/bandgap/netlist_startup.cir` writes `Vdd vdd 0 PWL(0 0 100n 1.8
  1 1.8)` — the ramp *is* that testbench's reason to exist, so it is the one
  deck of eleven that cannot carry a `DC` token. Zero matches, and `re.sub`
  returns its input unchanged. That deck is a testbench of **both**
  `spec_pvt.yaml` (45 corners) and `spec_corner_reduction.yaml` (9), so on the
  45-corner grid the startup testbench saw **15 distinct conditions, not 45**,
  and `startup_time`'s worst corner — plus its contribution to the corner
  reduction seed — was an arbitrary label on a 1.8 V run. Measured after the
  fix, at `tt/27`: **5.836 µs at 1.62 V vs 87.03 ns at 1.98 V**, a 67× spread
  the sweep had collapsed to one number. And at the corner the spec's own
  comment names as the slow end: real `sf/1.62/-40` is **9.751 µs** while what
  the sweep actually simulated under that label (`sf/1.8/-40`) is **5.140 µs**.
  The comment's `9.75us` matches the *real* 1.62 V value, so the threshold was
  set from a number the automated sweep could never reproduce — and the
  disagreement was invisible. Still inside the 20 µs threshold, so the
  benchmark's verdict does not move.
  **The part worth carrying: the fix for this exact shape was already written
  in this same function, six lines up.** The include swap carries a comment
  explaining that an exact-match would "silently no-op, leaving all 45 corners
  running the tt models" — the identical accident, diagnosed, fixed, and
  documented — while the line below it kept its own silent no-op. **When you
  fix a shape inside a function, read the rest of that function for it.**
- **The corner renderer now reports what it did, and refuses what it cannot
  do.** `render_corner_report` returns `CornerRender(text, states)` — the same
  richer-sibling shape as `area_limits.evaluate_area_growth` — and a call site
  that passes a logger gets a `corner_render` event **once per testbench,
  unconditionally**, including when all three rewrites applied.
  Per corner would be 45 identical lines; only-on-failure would make "checked,
  fine" and "the check is gone" identical again. **This entry used to claim both
  call sites log it. They do not, and that is silently-inert gate #12.**
  `run_full_pvt_sweep`'s `log_event` defaults to `None` and **every** production
  caller omits it — `cli.py:505` (entry sweep), `cli.py:857` (**the verdict
  sweep**), `cli.py:460` (optimization sweep), `curation.py:914`/`916`. Only
  `corner_sim` passes one, and that runs only where a spec declares
  `corner_reduction:`. So on `spec_pvt.yaml`, `two_stage_opamp/spec_pvt.yaml`
  and all of curation the event **never exists**, and on the reduced specs it
  covers the mid loop only: measured on `runs/perturb_argmax/history.jsonl`,
  all 10 `corner_render` events sit between the two mid-loop iterations and
  **zero** sit at either sweep. The event's whole reason to exist is defect #10
  — the PWL supply rewrite that silently no-op'd a testbench's voltage axis
  across 45 corners — and it is absent from the sweep that decides PASS/FAIL.
  `tests/unit/test_pvt.py:490` proves the logging works by **passing
  `log_event` itself**, a condition the shipped wiring never creates: the
  gate's own failure shape reproduced one layer up, in the test. Recorded in
  `docs/superpowers/specs/2026-07-29-theory-combination-results.md` §7-7.
  Three states, and the split is
  the point: `applied`; `absent` (nothing in this deck to touch — no
  `pdk_corner` include, no `Vdd` line — which is not an error and is exactly
  what `tests/unit/test_pvt.py`'s bare stub decks are); and, when the line **is**
  there in a form that cannot be rewritten without guessing, a raised
  `CornerRenderError`. It subclasses **`ValueError` on purpose**, so
  `run_orchestration` and `run_optimization`'s existing guards fold it into a
  clean FAIL / `optimize_failed` rather than a traceback — the optimization
  phase still has no FAIL outcome. One gap is deliberate and known:
  `cli.py`'s final sweep is not inside either guard, so an unrenderable deck
  crashes there. Reaching it requires a deck no benchmark has, and the
  alternative — rendering it unchanged — is the silent wrong verdict this whole
  entry is about.
- **The PWL supply rewrite is two SPICE facts plus one stated judgement, and
  everything outside them fails loudly.** Facts: a PWL value list is
  alternating `t1 v1 t2 v2 …` pairs, so voltages are the odd (0-based)
  indices; and after the last time point a PWL holds its last value forever,
  so that value is the level the supply settles at. Judgement: **every voltage
  entry numerically equal to that settled level moves to the corner voltage,
  every other entry is waveform shape and is left alone** — which is what keeps
  the startup ramp starting at 0 V while both 1.8 V entries become 1.62 V.
  Comparison goes through `netlist.parse_spice_value`, so `1.8` and `1800m`
  are one level. Refused with `CornerRenderError`, never half-handled: an odd
  token count, any non-numeric token (`TD=`, `REPEAT`, `{expr}`, a comma-glued
  token, the file form), the bare unparenthesised PWL (its value list runs to
  end of line and cannot be told from a trailing `AC 1` without guessing), a
  `+`-continued line, and a settled level of `0` (no plateau to identify). The
  `DC` branch is byte-for-byte what it was — verified by hashing every render
  of all eleven benchmark decks at three corners before and after: **31 of 33
  identical**, the two that moved being `netlist_startup.cir` at 1.62 V and
  1.98 V.
  **Recognising the supply by the name `Vdd` is still there and is still the
  forbidden guess.** Deliberately untouched here: it is not a live defect (all
  eleven decks use `Vdd`), and widening it belongs to the corner-model
  generalisation work, not to a rewrite-counting fix.
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
  cost *more* than the full sweep it is meant to pre-empt. (**That sweep is no
  longer 286 s.** 286 s was the sequential double loop; with the parallel
  backend the same 45-corner × 5-testbench sweep measures **52.6 s — 225
  simulations at 0.234 s each**, 5.4×. Every cost argument in this file that
  multiplies 1.271 s/sim is quoting the pre-parallel number.) A `max_corners`
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
- **Re-entry is not reachable in the argmax regime on this benchmark — the
  original claim stands — but ε-coverage makes it reachable, and that
  asymmetry is the useful fact (measured 2026-07-30).** Growth requires a
  *failing* criterion whose argmax sits **outside** the set — and if a
  criterion's argmax is inside, the mid loop measured that same corner and
  would have failed there first. On the entry deck the only outside corners
  are `tt`, the typical corner, which is nobody's worst case.
  `scripts/reentry_feasibility.py` swept 11 perturbation shapes over every
  (entry deck, verdict deck) pair: **9 of 242** combinations fire on the
  9-corner grid, and they are **argmax 0, coverage 9** — all nine the
  `cc_trim_20` deck, which is exactly the one case §8 of the results doc
  measured end to end. A smaller seed leaves more outside, so the regime that
  fires is the one that drops corners.
  **Reachability is a property of (deck × grid), not of the deck** — the same
  axis ε turned out to live on. Projected onto the 45-corner grid the same
  sweep gives **43 of 242, argmax 21, coverage 22**: the seed tracks *criteria*
  (9 here) while the grid does not, so 36 corners sit outside and an argmax can
  drift out of the set. That row is a **projection, not a measurement** —
  neither 45-corner spec declares `corner_reduction:`, so nothing runs there
  today. Read it as: enable reduction on a 45-corner grid and re-entry stops
  being dead code even without ε-coverage.
  **Firing needs all three conditions, and "the mid loop exits PASS" is a
  property of the run, not of one criterion.** The mid loop reaches the
  verdict sweep only when `evaluate_criteria`'s `overall_pass` holds at the
  seed's worst — *every* criterion. A first pass checked only the third
  condition (29 firings), a second checked (1) **per criterion** (25), and
  only the global form gives 9. **The per-criterion form was reported here as
  "the claim has been measured false", with argmax 7 — that was wrong and the
  whole-branch review caught it.** Under the correct predicate the argmax
  regime, which is what every shipped spec runs, fires zero times.
  The same weakening applies to any "does this ever happen" sweep: check the
  condition the *system* branches on, not a per-item proxy for it.
  Still true and worth keeping: a smaller move (`TRIMAMP.Xt.W` 8 → 5.2488, the
  value the optimizer's bisection landed on) drifted exactly one argmax —
  `vbg1_residual`, `ff/1.98` → **`tt/1.62`, outside the set** — and it was not
  a failing criterion.
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
- **A topology swap moves the circuit far more than a parameter step, and the
  corner set is not re-seeded afterwards.** `seed_from_sweep` reads the *entry*
  sweep once; a swap mid-run replaces a whole block body. The locked invariant
  still holds — every corner in the set is a real corner of whatever deck is
  current, so a mid-loop FAIL is still genuine and a mid-loop PASS still merely
  optimistic, with the full sweep as the verdict — so this costs **relevance,
  never correctness**, the same shape as a wrong focus. Recorded so nobody
  "fixes" it by re-seeding mid-run, which would spend a full sweep to buy
  nothing the verdict sweep does not already guarantee.
- **Best-arm identification was considered and rejected.** Pure-exploration
  bandits (successive halving, LUCB, racing) exist to spend a sampling budget
  well when each evaluation is *noisy* — their entire gain structure comes from
  needing repeated samples to separate arms whose means are close. SPICE is
  deterministic: one evaluation of a corner is that corner's exact value, there
  is no confidence interval to shrink, and re-sampling is pure waste. What
  remains is a covering problem over a known finite grid, which is what
  `seed_from_sweep` (union of argmaxes) plus a rotating probe already is.
- **The union of argmaxes IS a covering problem — but a degenerate one, and the
  roadmap's adoption rule for improving it could not be satisfied.**
  `sweep["worst_case_corners"]` maps criterion → **one** corner, so the set each
  corner covers is that map's preimage and **no two corners cover the same
  criterion**. Disjoint sets make the coverage function *modular*, not merely
  submodular: greedy is exactly optimal, the (1−1/e) guarantee has no gap to
  close, and — decisively — **you cannot drop a corner without dropping every
  criterion it covered**. The pre-registered rule was "fewer simulations at the
  same coverage rate", which under disjointness has no satisfying case at all.
  Same shape as D1's `0.000`; caught before running rather than after.
- **ε-근접 피복 was REJECTED by its own pre-registered rule on 2026-07-30, and
  everything below describes a mechanism that is measured, correct, and not
  adopted.** The rule required zero missed violations. Broadening the
  perturbation from one shape to eleven produced a counter-example on the first
  try: on `cc_trim_20` (`TRIMAMP.Xcc.W` 40 → 20) the ε=0.03 seed misses
  `trim_phase_margin`, which the argmax seed catches. The shipped measurement
  spec — `spec_corner_coverage.yaml`, 9 corners, ε=0.03 — is therefore
  **불채택**. Two facts the rejection turns on:
  **ε is a property of (deck × grid), not of the deck.** Same deck, same
  perturbation: 45 corners tolerate ε=0.03, 9 corners do not. A sparse grid's
  ε-neighbourhood pulls in *far* corners rather than similar ones. The largest
  ε holding 0 missed on the 9-corner grid is **0.01**.
  **The value 0.03 was derived from a single perturbation axis**, and that
  derivation is what got falsified — not the mechanism. Reviving it needs a new
  pre-registration, ε derived per (deck × grid) from *several* perturbation
  axes, and the probe-promotion fix below. Nothing ships with `coverage:`
  declared except the measurement copy, so the rejection required no revert.
  Full numbers:
  `docs/superpowers/specs/2026-07-29-theory-combination-results.md` §9.
  The mechanism, for whoever revives it: declared per spec as
  `corner_reduction.coverage: {epsilon, tau}`. Corner `c` covers criterion `j`
  iff `|value_j(c) − worst_j| ≤ ε·|worst_j|`. Absent block ⇒ today's argmax
  union, and the selected `CornerSet` is **byte-identical** (verified over 603
  cases: three real coverage-less specs × 200 randomised sweeps × three
  missing-measurement densities, zero mismatches). **ε and τ have no code-side
  default** — they are derived per deck, so moving to another deck is a
  re-declaration rather than a code change. The budget `k` is derived from `τ`;
  there is deliberately no `max_corners` integer cap.
- **`scale = abs(worst)` with no fallback: a criterion whose worst is `0.0` is
  covered only by an exact tie.** An `or 1.0` fallback silently turns ε from a
  *relative* into an *absolute* tolerance for that criterion, in raw units, via
  a constant derived from nothing. Failing closed here only ever grows the seed,
  which is the safe direction. **A corner with no measurement is never
  approximated** — "the circuit does not work here" is not smoothed away by ε.
- **"Zero missed violations at every ε up to 0.1" was measured on ONE
  perturbation axis and is false in general — this is the finding that
  rejected the technique.** The original number (11 violation instances,
  `spec_pvt.yaml` entry deck plus three decks perturbed the *same* way — both
  amplifiers' tail widths together) is reproduced below because the reasoning
  it rests on is still right; what was wrong was concluding a *bound* from a
  single axis. Re-measured over 11 shapes (single/multi-block, FET width vs
  resistor length vs MOS-cap width, shrink *and* grow) across both grids, 36
  violation instances: **9-corner ε=0.03 misses one**. The lesson generalises
  past ε: **a tolerance derived from one perturbation axis is not a tolerance.**
  Same error class as the D1 metric that could only return `0.000` — the
  measurement lacked the condition under which it could have answered
  differently, and here that condition is *perturbation diversity*, not run
  count. `scripts/perturbations.py` owns the shape list so the two feasibility
  scripts cannot drift apart on it.
  The original single-axis measurement, kept for its reasoning:
  The reason is physical: **a violation is a band, not a knife edge** — a
  criterion failing at its worst corner generally fails at neighbouring corners
  too, so dropping the exact argmax rarely loses the violation. The first
  attempt to measure this was **void** and that is recorded: on the entry deck
  both corner-carrying specs pass everywhere, so "violations caught" was `0 of
  22` and "missed" could only ever return 0. The mid loop runs on decks the
  tuner *moved*; that is where the measurement belongs.
- **`seed_from_sweep` now returns `(CornerSet, record)`** and `cli.py` logs the
  record as `corner_seed` **unconditionally, including on the argmax path** —
  otherwise "chose the old way" and "the logging code is gone" are the same
  silence. `dropped` is the field that answers "what does this look like when
  the gate does nothing?": empty means ε created no overlap. `by_criterion` is
  **omitted** in coverage mode rather than emitted, because it is an argmax
  attribution and in coverage mode it can name a corner that is not in the set —
  the `OPAMP2STAGE drives vdd,vss` error shape. `points_per_tb` is the
  algorithm's metric and mirrors `corner_sim._probe_enabled`; parallel-wave and
  wall-clock numbers are **deployment facts tied to worker count** and are
  deliberately not logged as if they were algorithmic.
- **Where this actually pays is decided by criteria count, not corner count, and
  the repo's benchmarks are mostly outside that regime.** The mid loop
  parallelises corners but not testbenches, so cost is
  `#testbenches × ceil(points_per_tb / workers)` — reducing corners buys nothing
  once `points_per_tb` fits in one wave. Measured at 9 workers: `spec_pvt` 45
  corners × 22 criteria → argmax seed 9, 11 pts/tb, **10 waves → 5 at ε=0.03**;
  `spec_corner_reduction` 9 × 22 → seed 6, 8 pts/tb, **5 waves at every ε**
  (nothing to buy); `two_stage_opamp/spec_pvt` 45 × **7** → seed 5, 7 pts/tb,
  **4 waves at every ε** — the highest corner/criteria ratio in the repo and
  still no room, because the seed tracks *criteria*. Exactly one shipped spec is
  in the regime on this hardware, and **the regime boundary moves with
  `cpu_count`** — which is why the adoption metric is `points_per_tb`, not waves.
- **`scripts/search_ab.py` refuses a non-`argmax` corner regime, and that
  refusal is the eleventh entry in this file's silently-inert ledger — caught in
  review, not shipped.** The harness calls `run_optimization` directly; it never
  enters the corner-reduced tuning loop, `optimizer.py` has zero
  `corner_reduction` references, and `spec.corner_reduction.coverage` is read in
  exactly one place (`seed_from_sweep`) reachable from exactly one caller
  (`cli.py`). A `coverage:` regime therefore mutated a field nothing in that
  call graph read **while the record named the regime** — two sides running the
  same circuit and a comparison file asserting they differed. In the one file
  whose only job is producing trustworthy comparisons. It now refuses at the
  boundary and names what would have to change, the same shape as `5764abe`.
  **A consequence worth stating plainly: the stage-1 × stage-3 factorial cannot
  be run until that harness drives the reduced loop.**

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
- **The tuner is shown what it already tried in this run, as facts.**
  `attempt_log.py` keeps one `Attempt` per *component change* — not per
  proposal — because "which knob did what" is the only thing the tuner needs
  back, and a proposal-level record cannot be read apart again. Each entry
  carries `(outer_iter, retry, refdes, param, old_value, new_value, outcome)`,
  where `outcome` is `kept` / `rolled_back` / `rejected`, plus the measured
  per-criterion `deltas` and the `regressed` names for the first two, and a
  `reason`/`detail` pair for the third. The last `ATTEMPT_RENDER_LIMIT = 30`
  entries are rendered into the tuner prompt; an empty list renders the **empty
  string**, because an empty table reads as "something happened" rather than
  "nothing did". An `attempt_log` event is written **every retry, including
  when the history is empty**, so "recorded, zero entries" and "the record is
  gone" are distinguishable — the same rule as `optimize_guard_infeasible`.
  **Regressions are computed from the judge's own `pass` flips, never from
  `verify_post`'s `regressed_criteria`.** That field is schema-attached but it
  is still an LLM's claim, while `deltas_between`/`regressed_between` are
  arithmetic on the numbers `judge` returned before and after. Where the two
  disagree the fact wins — the same rule that made curation *measure*
  `addresses` instead of accepting a declared one. `deltas_between` also drops
  any criterion missing from either side rather than reading an absent
  measurement as 0.0, which is the shape `corner_allowances` already paid for.
- **A rejection's reason code is recorded where it is decided, not re-parsed
  out of `history.jsonl`.** The five codes are `area`, `refdes`, `param`,
  `stimulus` and `verify_pre` (`_record_rejected` in `orchestrator.py`). They
  cannot be recovered from the event stream: `area_check` and `refdes_check`
  both write their text under the same `feedback` key, so a reader after the
  fact can only tell them apart by which event name it happened to be reading,
  and any future gate that reuses the key silently joins the wrong bucket.
  A gate rejects the **whole** proposal, so every change in it gets an entry
  with the same code — which change tripped the gate is not something the gate
  reports, and guessing it would be an unsupported attribution. The gate's own
  feedback string goes into `detail` verbatim and usually names the refdes.
- **`deltas`/`regressed` are measured once per proposal and stamped onto every
  change in it, so the render is a joint fact wearing a per-knob shape.** A
  3-change proposal renders as three lines each ending with the *same* delta,
  shape-illustrated as `pm +18.4` (a format example from the schema test and
  design doc, not a measured value), and the only grouping signal is the
  shared `iter N.R` prefix.
  `after/two_stage-1` really did feed the tuner that: iterations 4, 5, 6r2 and
  8 were all 3-change proposals. This is the shape this repo has now paid for
  three times (F2's agent-declared `addresses`, the zero-tolerance Pareto
  turning solver noise into a measured claim, and this). The fix is one clause
  in `TUNER_SYSTEM_PROMPT` stating that lines sharing an `iter N.R` prefix were
  applied together and their numbers are the group's effect, pinned by
  `test_the_tuner_prompt_says_lines_sharing_an_iteration_prefix_were_applied_together`.
  Splitting the measurement per knob is not available — one simulation measures
  one deck.
- **D1's claim was measured and is not supported; the mechanism it rests on
  is.** The paired probe ran to completion 2026-07-29 — **75 pairs, 150 calls,
  0 failed** — and the pre-registered rule returns **no measured effect**:
  `R_exact` A 0.173 vs B 0.187, discordant pairs 12 vs 13, **p = 1.0**. Per the
  rule that was fixed before running, that makes D1 a feature spending prompt
  tokens for a benefit that did not appear — **explicitly not neutral**.
  **But the history does change behaviour, strongly, in a way the verdict
  metric does not see.** The context-only `R_knob` went A 0.933 → B **0.653**,
  discordance 25 vs 4, **p = 1.0e-4**: given the record, the tuner *leaves* a
  failed knob far more often. It was declared context-only **before** the run,
  so it does not become the verdict — promoting it now is the exact move this
  repo forbids. What the experiment establishes is two things: the record
  reaches the prompt and changes behaviour (**true**), and that change reduces
  byte-identical resubmission (**false**).
  A post-hoc observation worth the next experiment's attention, with no test
  attached: conditional on staying on the knob, B repeats the *value* more
  (`P(exact|knob)` A 13/70 = 0.186 vs B 14/49 = **0.286**).
  Three things this does **not** licence: opening D2 (a suppression gate needs
  repeats shown to be a *cost*, and the metric cannot tell a deliberate knob
  walk from a rediscovery); the permission-sentence ablation (that needs
  **B significantly higher**, and p = 1.0); and *removing* D1 (the measurement
  says "no benefit on this metric", not "no effect" — `R_knob` says otherwise,
  and whether leaving a knob helps or hurts is unmeasured in both directions).
  Full numbers, per-timepoint table and limits in
  `docs/superpowers/specs/2026-07-29-d1-remeasurement-results.md`.
  **The design is why the answer exists at all** (`scripts/paired_tuner_probe.py`,
  `…-d1-remeasurement-design.md`): a paired probe, not another pair of runs
  (`scripts/paired_tuner_probe.py`, design in
  `docs/superpowers/specs/2026-07-29-d1-remeasurement-design.md`): it replays a
  recorded run's `history.jsonl` to each proposal point and calls the tuner
  **twice from the identical state**, differing in `tuning_history` alone
  (arm A empty, arm B the real history), k=5 per point, McNemar exact on the
  paired outcomes. That design exists because the first measurement's two arms
  saw *different states* — and because the metric only fires where a
  `(refdes, param)` already failed, so timepoints are selected on
  `failed ≠ ∅`, which makes defect 1 structurally unrepeatable. The verdict
  rule was fixed before running: **effective** = B's `R_exact` rate lower than
  A with `p < 0.05`; `p ≥ 0.05` = no measured effect (and D1 is then a feature
  with a cost and no measured benefit, not a neutral one). Do not read a
  partial sweep's verdict — the script computes one at any n, and a run stopped
  by an outage rather than by the plan is the same error again; the shipped run
  went the full distance (150/150) and the verdict above is the complete one.
  The **superseded** first measurement was on
  `benchmarks/two_stage_opamp/spec.yaml` (a full 10-iteration run of this spec
  costs ~103 min — 6161 s; the 1348 s run only looks cheaper because it died
  at iteration 3 on an agent execution error, `iterations_used: 2`, not
  because it paid for a full budget): repeat-proposal rate 0.000 before D1 vs
  0.741 and 0.429 after, and
  0.000 vs 0.429–0.636 with iterations matched. **Those comparisons are
  confounded and the "it went up" reading is withdrawn.** The metric counts a
  change as a repeat only when that `(refdes, param)` already ended in a
  rollback or a rejection, and the baseline run had **0 rollbacks, 0 gate
  rejections and 0 `verify_pre` rejections across 4 proposal events** — so
  `0.000` was the only value it could return. Two further limits: the metric
  keys on `(refdes, param)` and never on the proposed **value**, so a search
  walk on one knob (`OPAMP2STAGE.Xcc` 13 → 14 → 15 → 16 → 16.4 → 16.3 → 16.5,
  not monotone, and revisiting a value that was rejected earlier) counts
  identically to rediscovering a known-bad change — and the design document
  *defends* that walk (`TRIMAMP.XRz.l` 15 → 60 → 120). The one genuine
  rediscovery in the data is a single event and it is the branch's most
  informative one: `OPAMP2STAGE.X6.W 8 -> 14` was `verify_pre`-rejected at
  iter1.r1 and re-proposed **byte-identically** at iter6.r1 with that rejection
  still inside the 30-entry window, and was rejected again — one LLM call plus
  a retry burned with the record sitting in the prompt. **This is exactly the
  failure D1 was built to prevent, occurring with D1 active**, and it is the one
  repeat the prompt's `verify_pre` carve-out ("an identical resubmission is not
  guaranteed to draw the same verdict") arguably licensed. Verdict: **D2 (a
  deterministic suppression gate) is not opened** — but on the ground that the
  measurement was uninformative, not that repeats are frequent. Do **not**
  remove the prompt's "You MAY propose the same component and parameter again"
  sentence on this evidence: the before run performed the same walk on a commit
  where that sentence did not exist, its effect is unmeasured in both
  directions, and the settled design rule is that history is presented as facts
  and never as a restriction — the same rule the focus view is under. Full
  numbers, the retracted claims and the redesigned next experiments are in
  `docs/superpowers/specs/2026-07-28-tuning-attempt-record-measurement.md`.
- **The original cross-run D was deferred (to D3) because it would have carried
  between runs the fields one run was already throwing away**: the within-run
  history already reached the tuner, but it held
  `{outer_iter, proposal, recommendation}` with **no measured values**, and a
  gate- or `verify_pre`-rejected attempt never survived its iteration at all
  (`rejection_feedback` is reset every outer iteration and overwritten on every
  assignment). Make one run able to read its own attempts, measure what that
  changes, and only then widen. Full argument in
  `docs/superpowers/specs/2026-07-28-tuning-attempt-record-design.md` and its
  predecessor `2026-07-28-cross-run-experience-design.md`.

### Measurement apparatus (Stage 0), and the corners a spec declares

- `simulators/cache.py` / `simulators/parallel.py` — a simulation is a **pure
  function of `(deck text, control block, corner, simulator identity)`**, which
  is the same determinism premise the whole corner argument rests on, so it is
  content-addressed and cached. All four determinants are in the key: drop one
  and the cache **manufactures a fact**, which is worse than any inert gate
  (an inert gate only ever passes something through). Hits and misses are
  logged, because "the cache never hit" and "there is no cache" must not look
  the same. Corner × testbench points are independent, so they run in a pool
  (`ANALOGCODER_SIM_WORKERS`, default `cpu_count-1`); the merge is
  order-independent and results are re-read in declaration order, so
  completion order touches no value. Measured: the 45-corner × 5-testbench
  bandgap sweep went **286 s → 52.6 s**.
- `checkpoint.py` / `history.py` — resume at **boundaries only** (outer
  iteration, corner-reduction attempt, entry to optimization). Mid-iteration
  resume would mean replaying LLM calls. The sharp edge is **not** the state
  snapshot but the event log: a crash leaves *partial* events for the
  iteration, the resumed run writes the same kinds again, and both
  `scripts/measure_repeat_rate.py` and `scripts/paired_tuner_probe.py` read
  `history.jsonl`. Do **not** truncate the log — destroying evidence is not
  the answer. The checkpoint records the line count, the `resume` event
  records the abandoned range, and `history.read_events` drops those ranges
  (several, for several resumes). `resumed_from` is **always** in
  `result.json` (`null` when not resumed) — a partial run entering a mean as
  if it were whole is half of why the first D1 measurement was void.
- `json_io.py` is the one place that writes this repo's JSON, and
  `checkpoint.py` reads back through `restore_non_finite` — see the JSON entry
  under "The optimization phase". Checkpoints differ from `result.json` in one
  way that matters: they are **read back into a running run**, so a marker
  string reaching `judge_result` would `TypeError` in `deltas_between`.
- `control_block_gate.py` — the simulator agent may return a control block, and
  it is executed. The gate allows a fixed command vocabulary and requires every
  non-`.option` line to be preserved in order. **An allow list narrows the
  command vocabulary, not the argument surface**, and that gap was a live
  arbitrary-command-execution hole: `option`/`options` (non-dot) lines are the
  one class inside the allow list *and* excluded from the line-preservation
  comparison, and ngspice's `cp` shell performs backtick substitution on them.
  Demonstrated end to end — gate `accepted=True`, file created. Two layers
  close it: the allow list is now dot-form only (what the prompt always said;
  cost zero, 42 shipped blocks contain no option line of any form), and shell
  substitution markers in the free surface are refused with their own reason
  code. Narrowing alone would rest on "today's ngspice does not substitute in
  dot form", and the three vectors tested are not an exhaustive survey.
- **A spec declares corners as an *enumeration*, and axes are sugar.**
  Sign-off in the production flow is a **human-picked set of N signature corners**,
  chosen by code outside this repo (confirmed 2026-07-29) — a partial grid,
  which **no axis declaration can express**. A list can express a product (by
  enumerating it); a product cannot express an arbitrary list. So
  `PVTCorners.corners` is the single truth, `__post_init__` expands an
  axis-only construction once, and `all_corners` is the **identity function**.
  Do not "optimize" it back into a product — that loses expressiveness and
  points at combinations that may have no corner file. When declared as an
  explicit list, `process`/`voltage`/`temperature` stay **empty** rather than
  being back-derived, so nobody reads a partial grid as a product.
  This is also what keeps the locked constraint alive at company scale: with
  `M := N`, "the full sweep is the verdict" survives verbatim. **That rule is
  this repo's own convention, not an external requirement** — `report.py` and
  `cli.py` declare it and CLAUDE.md calls it locked; it was written when a full
  sweep cost 286 s, and that price is what made it free.
  The label-only half of "sign-off corners are **opaque include files**, not
  coordinates" **shipped 2026-07-29** (the axis half stays deliberately undone
  until physical confirmation, because deriving axis identity from a filename
  would be the third instance of a mistake this repo has already shipped twice
  — matching the literal basename `pdk_corner.inc`; recognising the rail by
  `^Vdd`). `CornerPoint` is now `corner_id` (required, derived from the
  coordinates when there are any) + `payload` (absolute path to the file that
  realises the corner, **whose contents are never read**) + the three
  coordinates, optional and filled only by an axis declaration. `_as_point` and
  `raw_label` had to change in the **same commit**: fix one and every corner
  becomes `"(deck)"`, and `cli._argmax_drift` compares two label strings, so
  `moved_count` is **permanently 0** — a metric that runs, never crashes, and
  reports a conclusion nobody measured. Same defect class as D1's `0.000`.

### The composed deck model

- `compose.py` / `spec.py`'s `compose:` block — a testbench can be declared as
  **fragments** (`signal declaration + corner + netlist`) instead of one file,
  because that is how the production flow builds its final deck. On that path
  corner rendering is **slot filling, not rewriting**: `render_corner_report`'s
  three regexes do not exist there — the corner file sets models, temperature
  and supply itself, and we place one `.include` naming it into the slot and
  **count** that (`corner_slot_filled`). `_apply_corner_voltage`'s docstring
  already said recognising the rail by the name `vdd` is a guess this repo
  forbids and is genuinely wrong; the composed model removes that guess here.
  **Only fragments are versioned** — composition happens just before
  simulation, the `tunable: true` fragment is `Testbench.netlist_path`, so
  `RunState`, checkpoint and `resolve_includes` consumers are untouched. Each
  fragment absolutizes its includes **against its own directory**.
- **The regexes leave and a quieter failure family arrives.** Every check in
  `compose.py` came from a failure reproduced against real ngspice-46: a
  fragment whose first line is a statement is eaten as the deck **title** and
  vanishes (gain_db 19.999 → 100.0, zero warnings); directive-collision winner
  rules differ per directive (`.model`/`.option`/`.subckt` first wins,
  `.param`/`.temp` last wins, all silent) so **no safe fragment order exists**
  and the collision itself is refused; a relative `.include` resolves against
  cwd, not the deck's directory; a missing boundary newline after a comment
  absorbs the next fragment's first line; a `.ends` **name** mismatch is silent
  (a count mismatch is loud).
- **`records` counts what was checked; the counts are not the gate.**
  `corner_slot_filled == 1` says this path ran, not that 0 was reachable — a
  composed testbench with 0 or 2 slots is refused at the declaration. The
  counts exist so "composed, fine" and "the compose path is gone" differ.
- **Where `netlist.py` already owns a parsing rule, import it — do not
  re-derive it.** `compose.py` hand-copied the include rule and diverged **in
  both directions**, in a file where `netlist.py` had written the warning
  against exactly that (`simulators/cache.py` imports the regexes for this
  reason). Measured: `.inc` — an abbreviation `_INCLUDE_RE` has always known —
  bypassed the absolute-path gate entirely while `includes_checked` logged
  `0`; and `.lib <section>` … `.endl`, the **definition** form that names no
  file, was read as a path and falsely rejected. Separately, `.param rf = 10k`
  (spaced, which ngspice accepts) made the collision key an **empty string**,
  so a real collision was missed — confirmed in ngspice, silent, last wins —
  while two spaced `.param`s with *different* names collided falsely, and
  `directives_checked` read healthy throughout. All three were caught by
  whole-branch review, not by a run.
- **A boundary written in one entry point is not written.** The tuning loop
  refuses a composed spec (`state.current_netlist_paths()` points at a single
  fragment — no stimulus, no corner, not a circuit; measured on the fragment
  view: `check_stimulus_untouched` approves a `Vin` change with
  `approved=True`, i.e. the gate fails **open**; `supply_nets` empties and
  `roles_on('vdd')` revives the `AMP drives vdd` false structural claim; a
  `.option scale` on another fragment flips the same proposal's area verdict).
  That refusal lived only in `cli._run`, so `analogcoder-curate` still opened
  the fragment. One sentence now, in `spec.refuse_composed_testbenches`.
- **A corner-rendering log that skips NOMINAL is wrong on this path.**
  `_run_point` composes for NOMINAL too (there is no unrendered deck on disk),
  so a composed testbench with a NOMINAL-only set used to compose and log
  nothing.

### Theory adoption — what has been tried and rejected

- `docs/superpowers/specs/2026-07-29-theory-adoption-roadmap.md` is the plan;
  each stage is adopted only against a **pre-registered** rule, and a rejected
  stage's negative result is recorded rather than deleted.
- **Stage 2 (Plackett–Burman screening): rejected, 12/22.** PB stays a
  diagnostic and is never used to delete an axis.
- **Stage 3 (trust-region DFO / MADS, `mads.py`): rejected** by the
  pre-registered rule (corner-confirmed objective 212.2517 vs 212.4025), **but
  the negative result is far narrower than the rule's sentence** and that was
  measured, not argued. Positive-direction power was ~0: the best improvement
  any searcher could show was 0.059%, below this repo's own measured 0.1%
  noise tolerance. Two of the three targeted weaknesses (coupling, mixed
  integer) could not fire at all — the ranking held one knob. And corner
  blindness is undecidable in that harness by construction, since corner
  information only arrives after the search ends. So **stage 4's precondition
  is left open** — do not read this rejection as "search is not the
  bottleneck". Before the next verdict: pre-register a minimum effect size,
  use ≥2 knobs, and pick a configuration where recovery-chain density does not
  dominate the metric.

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

A second console script judges whether a candidate topology may join
`TOPOLOGY_LIBRARY`. It writes artifacts and never touches the library — a
human commits the emitted snippet. See "Topology curation" under Architecture.

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

`--max-knobs` and `--knobs` are opt-in speed caps; by default every knob of the
block is swept, because a count cap truncates alphabetically and once hid the
knob that decided the shipped proof case.

## Benchmarks

- `benchmarks/inverting_amp/` — ideal op-amp (VCVS), single criterion (gain),
  passes immediately with no tuning needed. The "golden path" smoke test.
- **`benchmarks/two_stage_opamp/`'s bias chain has TWO stable DC solutions, and
  which one the solver lands on flips chaotically with device size and with
  corner (measured 2026-07-30).** `Xn2`+`Rdeg` is a self-biased
  beta-multiplier, `Rstart` is its start-up element, and **both** solutions
  carry current — so this is not the zero-current degenerate case the bandgap
  fixed with a trickle. State A: `degn` 0.0119 V (~0.6 µA). State B: `degn`
  0.0626 V (~3.1 µA), **5.3×**, which raises `gm1` and takes `ugbw_hz` from
  2.08e6 to **2.70e7 — 13×**. `vout` is 0.55 V in both (feedback holds it), so
  **the DC output cannot tell them apart; only the AC response can.**
  Isolated single widths flip it — `X6.W` 5.999999 fine, **6.0 flips**,
  6.000001 fine — so it is neither a model bin (bins are intervals) nor
  non-determinism (five fresh processes agree). **The shipped 45-corner data is
  already affected at the shipped `X6.W=8`: 127 measurements change under a
  `.nodeset` that steers to state A**, `t_lo_last` by 2.7×, while
  `overall_pass` stays False on both. So every number this file and the two
  op-amp design docs quote from this deck — the `Cc` sweep table, the PSR
  baselines, the "`Cc`+`M6.W` in 2 iterations" run that **moved `M6.W`** — has
  an unverified operating state behind it. **The tuner changes device sizes,
  so it can flip the state mid-run and the judge sees a different circuit with
  no record of it.** A `.nodeset` fixes every anomalous width and leaves every
  correct one byte-identical (verified), but applying it changes 127 documented
  measurements and "which state was intended" is a design question — so it is
  **not applied**. Full diagnosis, the three excluded hypotheses, and the four
  decisions this needs:
  `docs/superpowers/specs/2026-07-30-two-stage-opamp-bistable-bias.md`.
  **The contamination does not spread to `bandgap`, and that was measured, not
  assumed.** `scripts/dc_solution_uniqueness.py` pushes the bias chain's initial
  guess five ways — including **explicitly to the off state** — across four
  device sizes and **four of bandgap's six testbench decks** (two are refused,
  below): all six probes (`nbias`/`ncas`/`pbias`/`pcas`/`vbg1`/`vbg0`)
  come back identical to displayed precision every time. `netlist_seed_topology.cir`
  is one of the four, which matters — that deck's `BUF_P` body is `BUF_N`'s, a
  starved NMOS fold with 10.1 mV of tail headroom, so uniqueness holding there is
  not just a restatement of the shared-body expectation. So `BGR_CORE.Xsu_b`'s
  trickle really does make the branch unique, exactly as the entry that added it
  claims. Not a proof of uniqueness — five directions, not a dense size sweep,
  and the op-amp's flip lived on *isolated* sizes. The control case is the
  useful half: shrink `Xsu_b` from `W=0.42` to `0.2` and **no DC solution comes
  out at all** under any of the five guesses. That row is recorded as **void**,
  not as agreement — reading absent data as "the values matched" is the same
  error shape as a gate that passes because it cannot fail. Writing that script
  cost **four** silent failures with **exit code 0** each time: a control block
  after `.end` is ignored; one bad name in `print v(a) v(b)` discards the whole
  line; re-reading the `.nodeset` line reports the value you injected as a
  measurement; and — the one that would have shipped a false claim — **`op` on a
  deck whose supply is a time-dependent source measures a state the circuit never
  uses.** `netlist_startup.cir`'s PWL ramp is 0 V at `t=0`, and the script
  reported "multiple solutions" across four rows there, a result that did not even
  reproduce when the deck was run alone. The first three produce *no* value; the
  fourth produces a *wrong* one, which is why it is the dangerous shape. The
  refusal is keyed on the parsed fact "a top-level source uses a time-dependent
  function" and deliberately does **not** try to tell supply from stimulus —
  that needs recognising the rail, the guess this repo forbids — so it fails
  closed and costs one valid measurement (`netlist_settling.cir`), recorded in a
  **refused** column distinct from *void*. Note the same deck's PWL supply has now
  fooled this repo twice: it is also defect #10. Same family throughout —
  `re.sub`, `print`, an ignored block, and `op` on a ramp are all silent by
  construction.
- `benchmarks/two_stage_opamp/` — real transistor-level 2-stage CMOS op-amp
  (**sky130**, `.option scale=1.0u` plus `pdk_corner.inc`; it was generic
  level-1 before the 2026-07-26 PDK migration and this line said so until
  2026-07-28), three criteria (DC gain,
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
  **Until 2026-07-29 the `startup` testbench had no voltage axis at all**, in
  this spec and in `spec_corner_reduction.yaml` both: `netlist_startup.cir`'s
  supply is a `PWL` ramp and the corner renderer only rewrote a literal `DC`
  value, so its 45 corners were 15 distinct conditions repeated three times
  (see "A rewrite nobody counted…" under "Corner reduction and re-entry").
  **Any `startup_time` corner number quoted from a run before that date is a
  1.8 V number wearing another voltage's label** — including the `74.8ns ..
  9.75us` range in the spec's own criterion comment, whose slow end happens to
  match the *real* `sf/1.62/-40` (9.751 µs) rather than what the sweep ran
  under that name (`sf/1.8/-40`, 5.140 µs). Every other testbench in both specs
  is unaffected: they all carry a `DC` supply and their renders are
  byte-identical across the fix.
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
  `spec_corner_coverage.yaml` is that file plus a `coverage:` block
  (`epsilon: 0.03`, `tau: 1.0`) and nothing else — the ε-근접 피복 counterpart,
  and the pair is meant to be run against each other. **It is deliberately the
  spec where ε-coverage buys no wall clock** (8 points/tb already fits one wave
  of 9 workers, so both copies cost 5 waves), because the question it answers is
  safety, not speed: does the locked asymmetry survive, and does re-entry behave
  the same. ε = 0.03 is derived — measured, the largest ε holding missed
  violations at zero is ≥ 0.1 — and on this grid it cuts the seed 6 → 2, so the
  coverage path demonstrably fires; if it did not, the run would be void rather
  than a verdict.
  `netlist_seed_topology.cir` / `spec_seed_topology.yaml` are the seed that
  **only a topology swap fixes**. The deck is `netlist_loops.cir` with `BUF_P`'s
  body replaced verbatim by `BUF_N`'s — an NMOS-input fold put where the
  complementary PMOS-input fold belongs — and one testbench (`amp_loops`) with
  `buf0_loop_gain` raised 60 → **90 dB**. The block it buffers sits at 0.4999 V,
  which is below an NMOS pair's reach: measured, the tail current source is left
  with **10.1 mV** of Vds and `buf0_gain_db` collapses 100.16 → **73.52 dB**
  with UGBW down 19×. Widening the input pair is the knob the physics points at
  and it *does* help — but only ~7 mV of tail headroom per doubling, reaching
  83.45 dB at W=80 before W=150 aborts the run on sky130's 100 µm bin ceiling.
  So 90 dB sits strictly between what sizing reaches and the 100.16 dB the swap
  reaches, and the failure is structural rather than a sizing problem. The seed
  is localised the same way the other `spec_seed_*` variants are: on the DC
  testbench all 8 criteria still hold (`vbg0` 0.4999 → 0.5003 V, TC unchanged,
  `iq` 212.99 → 178.95 µA — the starved fold draws less). Pinned without any LLM
  in `tests/unit/test_topology_seed_ngspice.py` (2.5 s, no `slow` marker).
  **Do not "unify" the seed body with the library entry
  `folded_cascode_nmos_in_cs`** — that one came from `TRIMAMP` and differs in
  `Xcl` and the `Xcc`/`XRz` sizes, so the 73.52 dB / 10.1 mV numbers above were
  measured with `BUF_N`'s body and only hold for it.
  **Cascode (Ahuja/indirect) compensation was tried here and rejected, with
  data.** Moving the compensation cap from the Miller path to the cascode source
  node peaked at 89.4° / 5.45 MHz on `TRIMAMP`, while Miller+`Rz` at the *same*
  cap area reaches **99.7° / 27.0 MHz** — better on both axes. The cause was not
  the technique but the shipped sizing: `TRIMAMP.XRz.l = 15` is badly under-set,
  and raising it to 60 lifts phase margin 81° → 125° and UGBW 4.8 → 24.8 MHz
  together (it collapses again by 120, so the optimum is not monotone). A
  library entry exists to reach where value tuning cannot; this one does not
  qualify. Same shape as `spec_topology_required.yaml`'s caveat, opposite
  direction — there the agent found a knob the sweep missed, here the designer
  underestimated a knob the sweep already had. Full table in
  `docs/superpowers/specs/2026-07-28-topology-applicability-design.md`.
  `spec_curate_slot.yaml` is the **curation validation slot**: one testbench
  (`amp_loops`), the 9-corner grid, 8 criteria across all four amps, 30 tunable
  knobs on `TRIMAMP`, no `optimize:` and no `corner_reduction:`. It is the only
  spec against which an authored (source C) candidate can reach
  `verified_at="corners"`, and it is where the curation gate was measured
  reproducing F1's hand judgement: the Ahuja body as a candidate for `TRIMAMP`
  gives **REJECT**, dominated by the swept point `TRIMAMP.XRz.l = 25.98` at
  **99.90° vs the candidate's 89.42°**, with `addresses` narrowed to
  `['trim_phase_margin']`. 120 simulations, 2 min 41 s. The same run ADMITs if
  `COMPARISON_REL_TOLERANCE` is set to 0 — that counter-run is what the
  tolerance's justification rests on and it is pinned.

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
  run log; it has happened six times now (the running count is under
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
- **`pytest -m "not slow"` is the normal TDD cycle (measured 2026-07-30:
  **1467 passed, 2 skipped, 98–121 s** — two runs on the same commit came out
  98.5 s and 120.6 s, so read the budget as ~2 min, not 100 s).** It was 1441
  as of 2026-07-29 (composed-deck, MADS, ε-coverage) and 1273 before that.
  Note the count grew 1273 → 1467 while the wall clock stayed the same order —
  these are unit tests, so read the *time* as the budget and the count as
  drift. **The spread between two identical runs is wider than a year of count
  growth**, so do not treat a single timing as a regression signal.
  It was ~69 s / 923 tests before the Stage-0 measurement work
  (cache, parallel sweep, checkpoint/resume, history, json_io, the
  control-block gate) and the audit fixes, and before that
  ~45 s until topology curation added a real-ngspice test of its own
  (`test_curation_ngspice.py`, ~18 s); that one stays unmarked because the
  marker here means minutes, not seconds. Both files below carry the `slow`
  marker, registered in `pyproject.toml` — per-test on the optimizer case (the
  rest of that file is fast) and file-wide via `pytestmark` on the
  corner-reduction one. A plain `pytest -q` is ~3 min (corner reduction
  included, optimizer case still deselected by node id) and ~33 min with
  everything. Before this marker existed `pytest -q` had silently gone 45 s →
  3 min with no documented way to opt out. **Re-measure this line when you add
  a real-simulator test** — it has drifted twice.
