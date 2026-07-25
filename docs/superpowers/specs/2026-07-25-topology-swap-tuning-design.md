# Topology-Swap Tuning (Phase B) — Design

## Problem

The tuner can currently only change *values* of components already present in
the netlist (`TUNER_SCHEMA` / `apply_changes`). Some failures cannot be fixed
by value tuning alone — e.g. on `two_stage_opamp`, pushing the phase margin
past ~60° by increasing `Cc` alone eventually drops `unity_gain_bandwidth`
below its floor (empirically verified below), so no value of `Cc` or `M2.W`
satisfies all three criteria if the phase-margin threshold is raised even
slightly. A genuine circuit-design technique — adding a Miller nulling
resistor — solves this by cancelling the right-half-plane zero, but that
requires inserting a new component and a new node, which the current
value-only tuner cannot express.

This phase adds **topology swapping**: the tuner can, as a last resort,
replace the amplifier's internal structure with a different pre-verified
variant from a small curated library, instead of only tweaking values.

## Scope (v1)

- Structural changes apply **only** to a single amplifier `.subckt` block
  with the standard 5-port interface (`vinp vinn vout vdd vss`) — the same
  convention `two_stage_opamp` already uses. A netlist with zero or more than
  one such block is out of scope for this feature (parameter tuning still
  works normally; topology swap is simply unavailable).
- No inductors, ever (not used at the company this targets).
- The tuner picks from a **fixed, curated library** of known-good topologies
  — it never authors new SPICE structure itself. "Add/remove passive R/C"
  is not a separate freeform capability; it's expressed as different library
  entries (a topology *is* a specific set of components).
- v1 library has exactly two entries:
  - `miller_basic` — the existing two-stage Miller-compensated topology
    (`two_stage_opamp`'s current circuit, unchanged).
  - `miller_nulling_resistor` — the same circuit with a resistor `Rz` added
    in series with `Cc`, cancelling the RHP zero.
- Topology swap is a **last resort**: the orchestrator only offers it after
  parameter tuning has failed repeatedly at the current topology, and it
  shares the existing `MAX_OUTER_ITERATIONS` budget rather than getting a
  separate allowance per topology.

## Verified circuit data

Directly simulated in ngspice (not calculated by hand), starting from
`benchmarks/two_stage_opamp/netlist.cir`'s baseline (`Cc=2p`, no `Rz`):

**`Cc`-only sweep** (this is what parameter tuning alone can reach):

| `Cc` | gain (dB) | UGBW (Hz) | phase margin (°) |
|------|-----------|-----------|-------------------|
| 2p   | 87.03     | 44.33M    | 50.33 |
| 3p   | 87.03     | 30.96M    | 56.39 |
| 4p   | 87.03     | 23.66M    | 59.79 |
| 4.2p | 87.03     | 22.55M    | **60.33** |
| 5p   | 87.03     | 19.09M    | 61.94 (UGBW already fails) |
| 8p   | 87.03     | 12.04M    | 65.26 (UGBW fails badly) |

`Cc`-only tuning has a narrow window around 4.2p where phase margin barely
clears 60° while UGBW is still barely above 20M. Past that, phase margin
keeps improving but UGBW falls further below the 20M floor — there is no
`Cc` value that reaches, say, 65° phase margin without failing UGBW. This is
a genuine Pareto limit of the base topology, not a tuning-search failure.

**`miller_nulling_resistor` sweep** (`Cc=2p` unchanged, `Rz` in series with
`Cc` between the first-stage output and `vout`):

| `Rz` | gain (dB) | UGBW (Hz) | phase margin (°) |
|------|-----------|-----------|-------------------|
| 500  | 87.03     | 42.97M    | **66.13** |
| 1000 | 87.03     | 44.43M    | 80.78 |
| 2000 | 87.03     | 65.98M    | 105.25 |

`Rz=500` clears all three of the existing spec's thresholds (gain ≥70dB,
UGBW ≥20MHz, phase margin ≥60°) in a single swap, with UGBW still at 43MHz —
nowhere near the floor the `Cc`-only path was fighting. This confirms the
nulling-resistor topology is a real fix, not a coincidence of this benchmark.

**Testing implication:** because `two_stage_opamp`'s original spec (60°
threshold) is solvable by `Cc` alone, running it end-to-end won't force a
capable agent to ever attempt a topology swap. A new spec variant,
`benchmarks/two_stage_opamp/spec_topology_required.yaml`, raises the phase
margin threshold to 65.0° — unreachable via `Cc` alone (per the table above)
and reachable via `miller_nulling_resistor` (66.13° at `Rz=500`). This becomes
a real end-to-end proof that the feature *works*, but — see the note below —
it turned out not to be a proof that the feature is *necessary* for every
capable agent.

**Update after a real end-to-end run (2026-07-25):** a live Claude run against
`spec_topology_required.yaml` passed in 2 iterations *without ever attempting
a topology swap*. It found a two-parameter combination the `Cc`-only sweep
above never explored — `Cc: 2p→4p` together with `M6.W: 40u→100u` (widening
the output-stage NMOS) — reaching 91.02dB / 23.26MHz / 70.87°, comfortably
clearing all three thresholds. The `Cc`-only-tuning claim above is still
correct as stated (no value of `Cc` alone reaches 65° without failing UGBW),
but the broader claim that this spec is "unsolvable by parameter tuning
alone" was too strong — it's only unsolvable by `Cc` (or `Cc`+`M2`) tuning
alone. `M6`'s width is also in `tunable_params` and a capable agent can find
it. The topology-swap *mechanism* itself is unaffected by this — it's still
correct at the code level (unit tests, a real-ngspice test, and an
independent whole-branch review all verified it) — but
`spec_topology_required.yaml` doesn't reliably force it for a strong model.
It remains a useful differentiator between weaker and stronger tuning
strategies (see the Ollama results on the original `spec.yaml`, which never
found even the single-parameter `Cc` fix), just not a guaranteed trigger for
the topology-swap path specifically.
necessary and works, not just that it's wired up.

## Architecture

The orchestrator stays deterministic and in control of *when* topology
swapping is attempted — it is not left to the tuner's judgment. This follows
directly from what the Ollama runs this session already showed: a weak model
left to freely choose between "change a value" and "do something bigger"
tends to oscillate without ever recognizing exhaustion. A simple counter the
orchestrator owns is more reliable and testable than an LLM self-assessment.

- `consecutive_rollbacks`: incremented whenever a **parameter** proposal's
  `verify_post` recommends `"rollback"`; reset to 0 whenever a parameter
  proposal is kept (regardless of whether it fully passes yet).
- `TOPOLOGY_SWITCH_THRESHOLD = 3`: once `consecutive_rollbacks` reaches this,
  the *next* outer iteration is a topology-swap attempt instead of a
  parameter-tuning attempt — provided the library has an untried topology
  left. If the library is exhausted, the orchestrator just continues in
  parameter mode (existing behavior) until `MAX_OUTER_ITERATIONS`.
- After any topology-swap outer iteration (kept or rolled back),
  `consecutive_rollbacks` resets to 0 — parameter tuning gets a fresh
  runway under whichever topology is now current, rather than the
  orchestrator cascading straight through the whole library back-to-back.

## Components

### `src/analogcoder/topologies.py` (new)

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Topology:
    id: str
    description: str
    subckt_body: str  # lines between ".subckt NAME ports" and ".ends NAME"
    addresses: list[str]  # criterion names this is known to help; informational only, used in the tuner prompt


TOPOLOGY_LIBRARY: dict[str, Topology] = {
    "miller_basic": Topology(
        id="miller_basic",
        description="Standard two-stage Miller-compensated CMOS op-amp, no nulling resistor.",
        addresses=[],
        subckt_body="""\
Iref nb1 vdd 100u
M9 nb1 nb1 vdd vdd PMOSG W=20u L=1u

M1 n1   vinn tail vdd PMOSG W=40u L=1u
M2 outA vinp tail vdd PMOSG W=40u L=1u

M3 n1   n1   vss vss NMOSG W=20u L=1u
M4 outA n1   vss vss NMOSG W=20u L=1u

M5 tail nb1 vdd vdd PMOSG W=40u L=1u

M6 vout outA vss vss NMOSG W=40u L=1u
M7 vout nb1  vdd vdd PMOSG W=60u L=1u

Cc outA vout 2p
Ca outA 0 0.3p
""",
    ),
    "miller_nulling_resistor": Topology(
        id="miller_nulling_resistor",
        description=(
            "Two-stage Miller-compensated CMOS op-amp with a nulling resistor Rz "
            "in series with Cc, cancelling the right-half-plane zero. Improves "
            "phase margin substantially without the unity-gain-bandwidth loss "
            "that increasing Cc alone causes."
        ),
        addresses=["phase_margin"],
        subckt_body="""\
Iref nb1 vdd 100u
M9 nb1 nb1 vdd vdd PMOSG W=20u L=1u

M1 n1   vinn tail vdd PMOSG W=40u L=1u
M2 outA vinp tail vdd PMOSG W=40u L=1u

M3 n1   n1   vss vss NMOSG W=20u L=1u
M4 outA n1   vss vss NMOSG W=20u L=1u

M5 tail nb1 vdd vdd PMOSG W=40u L=1u

M6 vout outA vss vss NMOSG W=40u L=1u
M7 vout nb1  vdd vdd PMOSG W=60u L=1u

Cc outA vnull 2p
Rz vnull vout 500
Ca outA 0 0.3p
""",
    ),
}
```

Both bodies were taken directly from the ngspice runs in "Verified circuit
data" above — not retyped from memory.

### `netlist.py`: `apply_topology_swap`

```python
def apply_topology_swap(text: str, subckt_name: str, new_body: str) -> str:
    lines = text.splitlines()
    start = end = None
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.lower().startswith(".subckt") and stripped.split()[1] == subckt_name:
            start = i
        elif start is not None and stripped.lower().startswith(".ends"):
            end = i
            break
    if start is None or end is None:
        raise ValueError(f"subckt {subckt_name!r} not found or not closed")
    new_lines = lines[: start + 1] + new_body.splitlines() + lines[end:]
    return "\n".join(new_lines) + "\n"
```

Keeps the `.subckt NAME ports...` header and `.ends NAME` footer lines
untouched — only the interior is replaced. This is a mechanical text
operation, not LLM-authored SPICE, so it can't introduce the syntax/param
mistakes weak models made with free-text edits earlier this session.

### `agents/tuner.py`: `propose_topology_swap` (new function, same file)

```python
async def propose_topology_swap(
    analysis: dict,
    judge_result: dict,
    available_topologies: list[Topology],
    backend: AgentBackend,
) -> dict:
    ...
    return await run_agent(
        system_prompt=TOPOLOGY_TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,  # includes each candidate's id/description/addresses
        output_schema=TOPOLOGY_SCHEMA,
        backend=backend,
    )
```

`TOPOLOGY_SCHEMA` (new, in `schemas.py`) — deliberately **not** an enum,
because the set of *available* (untried) topologies changes per call and
`schemas.py` schemas are static module-level constants in this codebase:

```python
TOPOLOGY_SCHEMA = {
    "type": "object",
    "properties": {
        "topology_id": {"type": "string", "pattern": "^[a-z_][a-z0-9_]*$"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["topology_id", "reasoning", "confidence"],
}
```

The orchestrator, not another LLM call, checks that `topology_id` is both a
real library key and not already in `tried_topologies`. This mirrors the
existing `verify_pre` reject-and-retry loop (same `MAX_TUNING_RETRIES` cap,
same feedback-injection shape) but without spending an extra agent call —
justified because an invalid pick here is a closed-set membership check, not
the open-ended "is this a reasonable circuit change" judgment `verify_pre`
exists for.

### Orchestrator changes (`orchestrator.py`)

New fields on `OrchestratorAgents`: `analyze` (already exists, reused) and
`propose_topology: Callable`. No new `verify_pre`-equivalent — `verify_post`
is reused unchanged for topology swaps (it already just compares
before/after judge results, agnostic to what changed).

Whether topology swapping is available at all is decided **once**, before
the outer loop starts, from the scope constraint above:

```python
topology_swap_available = len(parse_netlist(initial_netlist_text).subckts) == 1
```

If `False` (zero or multiple in-scope subckts), the feature simply never
activates for this run — `consecutive_rollbacks` is still tracked but never
consulted, and behavior is identical to today's parameter-only pipeline. No
error, no special-casing elsewhere.

Sketch of the added branch, inserted where the parameter-tuning retry loop
currently starts:

```python
if topology_swap_available and consecutive_rollbacks >= TOPOLOGY_SWITCH_THRESHOLD:
    untried = [t for t in TOPOLOGY_LIBRARY.values() if t.id not in tried_topologies]
else:
    untried = []

if untried:
    topology_id = None
    feedback = None
    for retry in range(1, MAX_TUNING_RETRIES + 1):
        proposal = await agents.propose_topology(analysis, judge_result, untried, feedback)
        state.log_event("topology_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})
        candidate = proposal["topology_id"]
        if candidate in TOPOLOGY_LIBRARY and candidate not in tried_topologies:
            topology_id = candidate
            break
        feedback = f"'{candidate}' is not an available untried topology. Choose one of: {[t.id for t in untried]}"

    if topology_id is None:
        return _final_result("FAIL", state, outer_iter, judge_result,
                              failure_reason="topology proposal repeatedly rejected")

    tried_topologies.add(topology_id)
    topology = TOPOLOGY_LIBRARY[topology_id]
    subckt_name = next(iter(parse_netlist(netlist_text).subckts))  # the one in-scope subckt
    new_netlist_text = apply_topology_swap(netlist_text, subckt_name, topology.subckt_body)
    state.push_netlist_version(new_netlist_text)

    pre_swap_analysis = analysis
    analysis = await agents.analyze(new_netlist_text)   # re-analyze: structure changed, tunable_params are stale
    state.log_event("analysis", {"outer_iter": outer_iter, "topology_id": topology_id, **analysis})

    new_sim_result = await agents.simulate(new_netlist_text, spec)
    new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
    post_review = await agents.verify_post(judge_result, new_judge_result, [{"topology_id": topology_id}])
    state.log_event("verify_post", {"outer_iter": outer_iter, "topology_swap": True, **post_review})

    consecutive_rollbacks = 0  # reset regardless of outcome — see Architecture

    if post_review["recommendation"] == "rollback":
        state.rollback()
        analysis = pre_swap_analysis  # netlist is byte-identical to before the swap; no need to re-analyze
        continue

    if new_judge_result["overall_pass"]:
        return _final_result("PASS", state, outer_iter, new_judge_result)
    judge_result = new_judge_result
    continue

# existing parameter-tuning retry loop, unchanged, except:
#   - on rollback: consecutive_rollbacks += 1
#   - on kept: consecutive_rollbacks = 0
```

`tried_topologies: set[str]` is a local variable scoped to one
`run_orchestration` call (like `tuning_history` already is) — it doesn't
need to persist beyond the run.

## Error handling

No new error-handling paths. `propose_topology`, like every other agent
call, goes through `run_agent`/`AgentBackend`, so a malformed or
unvalidatable response already surfaces as `AgentExecutionError` and is
caught by `run_orchestration`'s existing top-level `try/except`, producing a
clean `FAIL` — exactly like every other agent in this pipeline.

## Testing

1. `tests/unit/test_topologies.py` — library has exactly the two documented
   entries; every entry's `subckt_body` parses as valid component lines via
   `parse_netlist` wrapped in a throwaway `.subckt`/`.ends`.
2. `tests/unit/test_netlist.py` (extend) — `apply_topology_swap`: replaces
   only the interior of the named block, preserves the header/footer lines
   verbatim, raises `ValueError` if the subckt isn't found or isn't closed.
3. `tests/unit/test_tuner_agent.py` (extend) — `propose_topology_swap`
   builds a prompt listing only the passed-in `available_topologies` (not
   the whole library), asserts `output_schema` is `TOPOLOGY_SCHEMA`.
4. `tests/unit/test_orchestrator.py` (extend), all with mocked agents:
   - stays in parameter mode below `TOPOLOGY_SWITCH_THRESHOLD` consecutive
     rollbacks.
   - switches to topology mode at the threshold, applies the swap, and
     calls `analyze` again with the new netlist text.
   - invalid/already-tried `topology_id` triggers retry-with-feedback, then
     `FAIL` with `"topology proposal repeatedly rejected"` after
     `MAX_TUNING_RETRIES`.
   - rolled-back topology swap restores the pre-swap `analysis` object
     (identity check) and resumes parameter mode with
     `consecutive_rollbacks == 0`.
   - library exhaustion (both topologies already in `tried_topologies`)
     falls back to parameter-only mode for the remaining iterations.
5. `benchmarks/two_stage_opamp/spec_topology_required.yaml` (new) — same
   circuit, `phase_margin` threshold raised to `65.0`. Real end-to-end run
   (both Claude and, time permitting, Ollama) against this spec is the
   feature's actual proof: parameter tuning alone must fail on it, and a
   correct implementation must reach `miller_nulling_resistor` and pass.
