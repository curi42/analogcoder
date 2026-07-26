# Netlist parse foundation (subproject E1) — design

**Status:** design agreed 2026-07-27, not yet implemented.

Subproject E ("netlist structure/representation for LLM analysis") was split in
two during brainstorming, because building structure derivation on the current
parse layer would build it on values that do not match the circuit:

- **E1 (this document)** — make the parse layer trustworthy: full nested scope
  tracking, dialect tolerance, and `.param` resolution.
- **E2 (its own spec, later)** — derive the structure, replace the analyzer
  agent. Its agreed shape is recorded at the end of this document so E2's
  brainstorming can start from it rather than re-deriving it.

## Why E1 exists: three silent defects, all reproduced

Each of these was found by probing the current code during brainstorming, not
by inspection. All three fail *silently* — that is the common thread, and it is
why they survived this long.

### 1. A nested `.subckt` definition reparents its enclosing subckt's components

```spice
.subckt OUTER a b
.subckt INNER c d
M1 c d 0 0 nch W=1 L=1
.ends
Xi a b INNER
M2 a b 0 0 nch W=2 L=1
.ends
```

```
parsed.subckts   == {'OUTER': [], 'INNER': ['M1']}
top_components   == [('Xi', scope=None), ('M2', scope=None)]
```

`INNER`'s `.ends` closes `OUTER`, so `OUTER` parses as empty and its own `M2`
and instance `Xi` are attributed to the **top level**. `check_refdes_resolution`
then reports `M2` as a valid, unambiguous top-level component.

`CLAUDE.md` describes this as nested subckts being "not scope-tracked" and
components "not distinguishable" from the outer level. That understates it: the
components are not merely indistinguishable, they are assigned to the wrong
scope, and a refdes that collides between `OUTER`'s body and the true top level
resolves to the wrong one.

### 2. An HSPICE `$` inline comment destroys the line

```
M1 d g 0 0 nch W=1 L=1 $ hspice inline comment
  -> nodes  = ['d', 'g', '0', '0', 'nch', '$', 'hspice', 'inline']
     value  = 'comment'
```

The model name is gone, so the device class is unknown, which makes the area
tier wrong, the terminal roles wrong, and every E2 derivation wrong. No
exception is raised.

### 3. A parameterised value disables the area gate

| proposal | growth | `check_area_growth` |
|---|---|---|
| `W=30` → `300` | 10× | **rejected** |
| `W='wn*2'` → `'wn*20'` | 10× | **approved** |

`check_area_growth` deliberately treats an unparseable baseline as "cannot
judge area impact, do not block" — a guard added earlier for a real crash, and
worth keeping. On a literal deck it fires rarely. On a parameterised deck,
which is the norm in HSPICE, it fires on *every device*, so the gate is
effectively absent.

This is the third bug of this shape in the project, after the inert area tiers
and the PVT two-sided windows. The pattern to watch for: a guard whose
fallback is "allow", reached far more often than its author expected.

## Constraints

- **All scopes must be tracked**, at any nesting depth. Stated as a hard
  requirement by the user.
- **The company will run HSPICE.** E1 must not add HSPICE-hostile assumptions,
  and must handle the HSPICE dialect features that are purely lexical. Actual
  HSPICE *simulation* stays out of scope — `simulators/base.py` already
  documents it as a future backend, and implementing an untestable simulator
  path would leave unverified code behind.
- The project's existing philosophy holds: deterministic checks run before LLM
  calls, and a rejection returns retryable feedback rather than ending the run.

## Design

### Scope becomes a path

`_line_scopes` changes from a single current-subckt value to a stack, and
`Component.scope` becomes a dotted path: `"OUTER.INNER"`, or `None` at the top
level. `ParsedNetlist.subckts` is keyed by that same path, so a nested
definition is `subckts["OUTER.INNER"]`.

Keying by path is not cosmetic: in SPICE a nested `.subckt` is local to its
enclosing subckt, so two different outer subckts may each define their own
`INNER` with different contents. A flat name key cannot represent that.

Every benchmark in this repo declares its subckts at the top level, so their
paths equal their bare names and existing keys are unchanged.

`split_scoped_refdes` splits on the **last** dot, so `"BUF_P.X6"` continues to
mean scope `BUF_P`, refdes `X6`, and `"OUTER.INNER.M1"` means scope
`OUTER.INNER`, refdes `M1`.

**Resolution rule:** a qualified refdes must match a scope path **exactly**. A
partially-qualified path (`INNER.M1` for a component in `OUTER.INNER`) is
rejected rather than guessed at — suffix matching would reintroduce ambiguity,
which is the thing `check_refdes_resolution` exists to remove. The rejection
feedback lists the valid full paths, so the tuner corrects in one retry, the
same way the area and refdes gates already work.

**`TUNER_SCHEMA` must be relaxed.** Its refdes pattern currently permits at
most one dot:

```python
r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"
```

`OUTER.INNER.M1` fails schema validation before any gate sees it. The pattern
becomes one or more dot-separated identifiers:

```python
r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
```

### Dialect tolerance

Three purely lexical additions, all testable on text with no simulator:

- `$` and `;` begin an inline comment; strip to end of line. Neither character
  can appear in a SPICE identifier, so this is unambiguous. Checked against the
  ten benchmark netlists: no `$` at all, and every `;` occurrence sits inside a
  line that is already a `*` comment, so this change moves nothing today.
- `.macro` / `.eom` are accepted as synonyms for `.subckt` / `.ends`.
- `.inc` is accepted as a synonym for `.include`, in both `_INCLUDE_RE` and
  `resolve_includes`.

### `.param` resolution

Parameters resolve from three sources, in increasing precedence:

1. Global `.param name=value` declarations at the top level.
2. Subckt definition defaults — the trailing `name=value` tokens on the
   `.subckt` line (`.subckt SUB a b W=10`).
3. Instance overrides — `name=value` tokens on the instance line
   (`X1 a b SUB W=20`).

Expression evaluation is deliberately bounded to a subset that can be
implemented correctly and tested:

- arithmetic `+ - * / **`, unary minus, parentheses
- numeric literals including SPICE suffixes, via the existing
  `parse_spice_value`
- references to other parameters
- surrounding `'...'` (HSPICE) or `{...}` (ngspice) quoting is stripped

Evaluation uses Python's `ast` module with an explicit node whitelist, never
`eval` on the raw string.

Anything outside that subset resolves to `None` — **flagged as unresolvable,
never guessed**. That includes:

- function calls (`sqrt(...)`, `max(...)`) and conditionals
- an undefined parameter name
- a circular reference
- **a subckt definition whose instances resolve the same parameter to
  different values** — the value is genuinely instance-dependent, and this
  project addresses components by subckt *definition*, so there is no single
  correct answer. This is the same underlying constraint as the existing
  "two differently-tuned instances require two subckts" gotcha.

An unresolvable value falls back to today's behaviour, so E1 never makes a
currently-working deck worse. It converts a silent wrong answer into an
explicit "unknown".

### `Component` carries both forms

```python
@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None                          # raw token, unchanged
    params: dict[str, str]                     # raw tokens, unchanged
    resolved_params: dict[str, float]          # new: numeric where resolvable
    resolved_value: float | None               # new: for positional values
    raw_line: str = ""
    scope: str | None = None                   # now a path
    geometry_scale: float = 1.0
```

The split is the point: `apply_changes` edits the **raw** text and stays a
textual operation, while `check_area_growth` and E2's derivations read the
**resolved** numbers. Neither has to know about the other's representation.

### The area gate consumes resolved values

With resolution in place, `W='wn*2'` → `'wn*20'` is tiered exactly like the
literal 10× case and rejected. Where a value is unresolvable, the existing
"cannot judge, do not block" fallback stays — now reached only when it is
genuinely true.

### What a tuning change edits

When a device's parameter is an expression, `apply_changes` **replaces the
device token with a literal**: `W='wn*2'` → `W=55`. This keeps the change local
and keeps `apply_changes` a textual operation.

The cost is real and worth recording: that device drops out of the deck's
parameterisation, losing the traceability its designer intended. Editing the
`.param` declaration instead was considered and rejected for now — one `.param`
feeds many devices, so the area gate would have to compute the growth of every
device the parameter reaches before it could rule on a single change. That is a
larger design, and it belongs to subproject C (area/current sizing) if it is
wanted at all.

## Testing

Three layers, in this order.

**1. Regression tests for the three reproduced defects.** Each of the probes in
"Why E1 exists" becomes a test asserting the correct behaviour. They fail
against today's code, which is what makes them meaningful.

**2. A synthetic HSPICE-flavoured deck.** A small fixture exercising nesting,
`$` comments, `.macro`/`.eom`, global and subckt-level `.param`, instance
overrides, and at least one deliberately unresolvable value. The company's real
decks are not available here, so this fixture is the stand-in and its
limitations should be stated where it lives.

**3. Golden-snapshot invariance on the ten existing benchmark netlists** — five
bandgap, four two_stage_opamp, one inverting_amp.

The ordering matters: **the snapshots must be generated from the current code
and committed before any parser change**, then kept green through the change. A
golden file generated from the new code proves nothing. This is the guarantee
that a parser rewrite this broad does not silently move a value in a benchmark
that took real ngspice measurement to characterise.

## Out of scope

- HSPICE simulation, and the HSPICE `SimulatorBackend`.
- HSPICE functions, conditionals, `.if`/`.elseif`, wildcards, and encrypted
  or binary library formats.
- Evaluating `.param` expressions for tuning *purposes* (proposing a change to
  a parameter rather than a device) — see the note under "What a tuning change
  edits".
- Everything in E2 below.

## Recorded for E2 (agreed, not yet specified)

E2's shape was settled during the same brainstorming session and is recorded
here so its spec starts from these decisions instead of re-deriving them.

**Motivation, measured.** In the `bg_buf0c` run — which passed in 4 iterations
— the analyzer agent returned a schema-valid but empty analysis:

```json
{"circuit_type": "test",
 "stages": [{"name": "a", "role": "b", "components": ["c"]}],
 "component_roles": {"a": "b"},
 "tunable_params": [{"refdes": "a", "param": "b", "role_in_circuit": "c"}]}
```

The run passed anyway, because the tuner receives `netlist_text` directly. And
across runs on the same bandgap netlist the analyzer produced 93, 26, and 1
component roles — the circuit's "structural ground truth" is not reproducible.
`tunable_params` appears only in the tuner's prompt string; nothing in the code
enforces it.

**Decisions:**

- The analyzer agent is **replaced** by deterministic derivation, not augmented.
- `circuit_type` comes from `spec.yaml`'s existing `circuit_name` field. What
  the circuit *is* gets declared by a human; what it *contains* gets derived.
- Three modules, dependency flowing one way, `structure` → {`signal_path`,
  `patterns`}:
  - `structure.py` — flat per-scope facts: component inventory, device classes,
    the complete tunable `(refdes, param)` index, per-net terminal roles.
  - `signal_path.py` — the instance tree, port↔net mapping across hierarchy
    levels, and `net → {block: drives | senses}`.
  - `patterns.py` — four local subgraph matches: differential pair, current
    mirror, cascode, Miller compensation (`Cc` ± `Rz`).
- `patterns.py` stays a separate module despite its size because it is the only
  one of the three that can be *wrong*; isolating the fallible part from the
  exact parts is the boundary. It is also where subproject F would grow.
- **Patterns never guess.** A match produces a fact; a non-match produces
  silence. This is the direct opposite of an LLM filling a schema with `{"a":
  "b"}` to satisfy it.
- Drive/sense direction comes from terminal roles, not connectivity: a MOSFET
  gate carries no DC current and is a pure sense terminal, everything else
  conducts. Verified on `BUF_P`: net `vout` is touched by `X6`'s and `X7`'s
  drains (the CS output stage), net `vinn` only by `X1`'s gate.
- Signal-path output names **subckt definitions** (`BUF_P`), not instances
  (`BANDGAP.Xb0`), because the definition is what the tuner can address. A
  subckt with more than one instance reports that fact, since tuning it changes
  every instance.
- A new deterministic gate, `check_param_applicability`, sits beside
  `check_refdes_resolution` and checks **existence only** — does this parameter
  actually exist on this component. It does not enforce a tuning-eligibility
  whitelist, which risks blocking a legitimate knob the derivation missed.
- The tuner keeps `netlist_text` **and** gains the derived structure. Removing
  the raw text is an unverified bet: the evidence says the tuner succeeds by
  reading it.

**The defect the param gate exists to catch**, reproduced during brainstorming:

```
proposal: A.X6, param="width", 20 -> 55     (the real token is W=20)
refdes gate: passes
result:  X6 d g s s ...pfet_01v8 L=1 W=20 width=55
```

`Rf a b 10k` with `param="R"` becomes `Rf a b 10k R=15k`. In both cases the
netlist changed, the device did not, the simulation shows no improvement, and
`verify_post` rolls back — burning an iteration for a reason nobody can see.
`CLAUDE.md` documents this as a weak-model hazard; nothing currently guards it.
It matters more as the production target moves to GLM-5.2.

**E2 success criteria** (agreed): determinism and coverage; the param gate
demonstrably catching the silent failure above; and improvement on a weak model
via the Ollama backend.

## Related

- `docs/superpowers/specs/2026-07-26-bandgap-benchmark-and-scoped-refdes-design.md`
  — subckt-scoped refdes (subproject A part 1), which this extends to full depth.
- `docs/superpowers/specs/2026-07-25-area-aware-tuning-design.md` — the area
  gate whose fallback defect is fixed here.
