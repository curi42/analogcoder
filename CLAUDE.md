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
  `patterns.py` matches differential pairs, current mirrors, **stacked
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

## Known limitations / gotchas for weaker (local) models

Found by actually running the pipeline against Ollama, not by inspection —
worth reading before assuming a weak-model failure is a code bug:

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

## Testing conventions

- TDD throughout; every module has a paired test file in `tests/unit/`.
- Agent tests mock `run_agent`/`AgentBackend`, never hit a real LLM.
- `tests/integration/` holds two skip-gated real-backend tests (`ANTHROPIC_API_KEY`
  for Claude, `LOCAL_LLM_BASE_URL` for local) — skipped by default, meant to be
  run manually when you have real credentials/a real server available.
