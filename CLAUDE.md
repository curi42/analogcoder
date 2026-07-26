# analogcoder

CLI that automates iterative analog circuit verification and repair: run a SPICE
simulation, judge the result against pass/fail criteria from a spec, and if it
fails, propose and apply netlist parameter changes, then re-verify — repeating
until it passes or hits iteration/retry limits.

## Architecture

Five independent LLM agents (netlist analyzer, simulator, judge, tuner, verifier)
coordinated by a deterministic (non-LLM) Python orchestrator in `orchestrator.py`.
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
- `agents/*.py` (analyzer, judge, simulator_agent, tuner, verifier) — one file
  per agent: system prompt + schema + tool declarations (`ToolSpec`, not
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
including real tool calls, not just the no-tuning-needed happy path.

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
- **`netlist.py` tracks subckt scope.** A component inside a `.subckt` is
  addressable as `<SUBCKT>.<refdes>`; an unqualified refdes still works when
  it matches exactly one component netlist-wide, and raises `ValueError` when
  it is ambiguous rather than silently editing the first match. The scope is
  the subckt *definition*, so a change applies to every instance of it —
  two differently-tuned instances require two subckts.
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
  (analyzer, tuner, verifier) than at tool-calling agents (simulator, judge).
  If a weak-model run fails, check which agent failed before assuming the
  whole pipeline is unreliable.
- If an agent's structured output still doesn't validate after retries,
  `orchestrator.py` catches `AgentExecutionError` and returns a clean
  `{"status": "FAIL", ...}` result instead of crashing — this is intentional
  (see `run_orchestration`'s try/except). Don't remove it.

## Testing conventions

- TDD throughout; every module has a paired test file in `tests/unit/`.
- Agent tests mock `run_agent`/`AgentBackend`, never hit a real LLM.
- `tests/integration/` holds two skip-gated real-backend tests (`ANTHROPIC_API_KEY`
  for Claude, `LOCAL_LLM_BASE_URL` for local) — skipped by default, meant to be
  run manually when you have real credentials/a real server available.
