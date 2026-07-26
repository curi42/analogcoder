# Subckt-Scoped Refdes Addressing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a component inside a specific `.subckt` individually addressable for tuning, so a proposal can target `BUF_N.Xcc` rather than silently editing whichever `Xcc` appears first.

**Architecture:** A refdes gains an optional `<subckt_name>.` prefix. `parse_netlist` records each component's subckt scope; `apply_changes` tracks scope while scanning lines and raises on an ambiguous unqualified refdes instead of first-match-wins; `index_baseline_components` keys by scoped refdes with unambiguous plain aliases so existing single-subckt benchmarks keep working. The scope is the subckt **definition**, not the instance — editing a subckt body changes every instance, which is what SPICE means.

**Tech Stack:** Python 3.14, pytest, jsonschema. No new dependencies.

## Global Constraints

- TDD throughout: write the failing test, run it and watch it fail, then implement. Every module has a paired test file in `tests/unit/`.
- Agent tests mock `run_agent`/`AgentBackend` and never hit a real LLM.
- Run the suite with `.venv/bin/python -m pytest`.
- Scope is the subckt **definition**, never the instance. Instance-scoped addressing (`Xb1.Xcc`) is explicitly out of scope.
- Back-compatibility is mandatory: an unqualified refdes that matches exactly one component anywhere must keep working, because `benchmarks/two_stage_opamp` and `benchmarks/inverting_amp` and all their tests rely on it.
- Do not change `apply_changes`'s behavior for a refdes matching **zero** components — it stays a silent no-op. Only ambiguity becomes an error. Widening this is a separate change.

## Scope Note

This plan covers **Part 1** of `docs/superpowers/specs/2026-07-26-bandgap-benchmark-and-scoped-refdes-design.md`. Part 2 (the bandgap chain benchmark) needs device sizing found empirically in ngspice, so it gets its own plan once this lands and the circuit can actually be run.

---

### Task 1: Record each component's subckt scope

**Files:**
- Modify: `src/analogcoder/netlist.py:34-41` (`Component`), `src/analogcoder/netlist.py:79-105` (`parse_netlist`)
- Test: `tests/unit/test_netlist.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Component.scope: str | None` — the name of the `.subckt` the component was declared inside, or `None` for a top-level component. Task 3 reads this.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_netlist.py`:

```python
def test_parse_netlist_records_each_component_subckt_scope():
    text = (
        ".subckt BUF_P vinp vinn vout vdd vss\n"
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
        ".ends BUF_P\n"
        ".subckt BUF_N vinp vinn vout vdd vss\n"
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
        ".ends BUF_N\n"
        "Cload vout 0 2p\n"
    )

    parsed = parse_netlist(text)

    assert parsed.subckts["BUF_P"].components[0].scope == "BUF_P"
    assert parsed.subckts["BUF_N"].components[0].scope == "BUF_N"
    assert parsed.top_components[0].scope is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py::test_parse_netlist_records_each_component_subckt_scope -v`
Expected: FAIL with `AttributeError: 'Component' object has no attribute 'scope'`

- [ ] **Step 3: Write minimal implementation**

In `src/analogcoder/netlist.py`, add the field to `Component`:

```python
@dataclass
class Component:
    refdes: str
    ctype: str
    nodes: list[str]
    value: str | None
    params: dict[str, str] = field(default_factory=dict)
    raw_line: str = ""
    scope: str | None = None
```

In `parse_netlist`, set it where the component is filed:

```python
        component = _parse_component_line(line)
        if current_subckt is not None:
            component.scope = current_subckt.name
            current_subckt.components.append(component)
        else:
            top_components.append(component)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -v`
Expected: PASS, including all pre-existing tests (the new field defaults to `None`, so nothing else moves).

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/netlist.py tests/unit/test_netlist.py
git commit -m "feat: record each component's subckt scope in parse_netlist"
```

---

### Task 2: Scope-aware `apply_changes` with an ambiguity error

**Files:**
- Modify: `src/analogcoder/netlist.py:108-140` (`apply_changes`)
- Test: `tests/unit/test_netlist.py`

**Interfaces:**
- Consumes: nothing from Task 1 (this task works on raw text lines, not parsed components).
- Produces: `split_scoped_refdes(scoped: str) -> tuple[str | None, str]` — Task 4's schema tests reference the same `<subckt>.<refdes>` form. `apply_changes(text, changes)` keeps its existing signature and raises `ValueError` on an ambiguous unqualified refdes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_netlist.py`:

```python
TWO_BUFFERS = (
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Cload vout 0 2p\n"
)


def test_apply_changes_scoped_refdes_edits_only_the_named_subckt():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "BUF_N.Xcc", "param": "W", "new_value": "99"}])

    xcc_lines = [ln for ln in out.splitlines() if ln.startswith("Xcc")]
    assert xcc_lines == [
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10",
        "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=99",
    ]


def test_apply_changes_raises_on_an_ambiguous_unqualified_refdes():
    # Silently editing the first match is how a tuner's change to one block
    # lands in a different block with no error at all.
    with pytest.raises(ValueError, match="ambiguous"):
        apply_changes(TWO_BUFFERS, [{"refdes": "Xcc", "param": "W", "new_value": "99"}])


def test_apply_changes_still_accepts_an_unqualified_refdes_that_is_unique():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "Cload", "param": "value", "new_value": "5p"}])

    assert "Cload vout 0 5p" in out


def test_apply_changes_scoped_refdes_that_matches_nothing_is_a_no_op():
    out = apply_changes(TWO_BUFFERS, [{"refdes": "BUF_P.Xnope", "param": "W", "new_value": "99"}])

    assert out == TWO_BUFFERS
```

Ensure `import pytest` is present at the top of `tests/unit/test_netlist.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -k "scoped or ambiguous or unqualified" -v`
Expected: FAIL — the scoped test edits `BUF_P`'s line instead of `BUF_N`'s, and the ambiguity test does not raise.

- [ ] **Step 3: Write minimal implementation**

In `src/analogcoder/netlist.py`, add above `apply_changes`:

```python
def split_scoped_refdes(scoped: str) -> tuple[str | None, str]:
    """Splits "BUF_N.Xcc" into ("BUF_N", "Xcc") and a bare "Xcc" into
    (None, "Xcc"). One level only: the scope is a subckt DEFINITION name,
    which is unique within a netlist, so nesting never needs more."""
    scope, sep, refdes = scoped.rpartition(".")
    if not sep:
        return None, scoped
    return scope, refdes


def _line_scopes(lines: list[str]) -> list[str | None]:
    """For each line, the name of the .subckt it sits inside, or None at
    top level. Directive lines themselves are reported as None; they are
    skipped by every caller anyway."""
    scopes: list[str | None] = []
    current: str | None = None
    for raw_line in lines:
        stripped = raw_line.strip()
        lower = stripped.lower()
        if lower.startswith(".subckt"):
            scopes.append(None)
            current = stripped.split()[1]
            continue
        if lower.startswith(".ends"):
            scopes.append(None)
            current = None
            continue
        scopes.append(current)
    return scopes
```

Replace the body of `apply_changes` with:

```python
def apply_changes(text: str, changes: list[dict]) -> str:
    lines = text.splitlines()
    scopes = _line_scopes(lines)
    for change in changes:
        scope, refdes = split_scoped_refdes(change["refdes"])
        param = change["param"]
        new_value = change["new_value"]

        matches: list[tuple[int, list[str]]] = []
        for i, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("*") or stripped.startswith("."):
                continue
            tokens = stripped.split()
            if tokens[0] != refdes:
                continue
            if scope is not None and scopes[i] != scope:
                continue
            matches.append((i, tokens))

        if not matches:
            continue
        if len(matches) > 1:
            where = sorted({scopes[i] or "<top-level>" for i, _ in matches})
            raise ValueError(
                f"refdes {change['refdes']!r} is ambiguous - it matches components in {where}; "
                f"qualify it as <subckt>.{refdes}"
            )

        i, tokens = matches[0]
        if param == "value":
            positional_idx = [j for j, t in enumerate(tokens) if "=" not in t]
            tokens[positional_idx[-1]] = new_value
        else:
            replaced = False
            for j, tok in enumerate(tokens):
                if tok.startswith(f"{param}="):
                    tokens[j] = f"{param}={new_value}"
                    replaced = True
                    break
            if not replaced:
                tokens.append(f"{param}={new_value}")
        lines[i] = " ".join(tokens)
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the full suite to verify nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Every existing benchmark has a single subckt, so no existing unqualified refdes becomes ambiguous.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/netlist.py tests/unit/test_netlist.py
git commit -m "feat: target apply_changes by subckt-scoped refdes, raise on ambiguity"
```

---

### Task 3: Index baseline components by scoped refdes

**Files:**
- Modify: `src/analogcoder/area_limits.py:68-73` (`index_baseline_components`)
- Test: `tests/unit/test_area_limits.py`

**Interfaces:**
- Consumes: `Component.scope` from Task 1.
- Produces: `index_baseline_components(netlist_text) -> dict[str, Component]` keyed by `"<subckt>.<refdes>"` for subckt components and by plain refdes for top-level ones, plus a plain alias for any refdes that occurs exactly once netlist-wide. `check_area_growth` is unchanged and looks up whatever refdes the proposal used.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_area_limits.py`:

```python
TWO_BUFFERS_NETLIST = (
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    "Xonly n2 vss sky130_fd_pr__nfet_01v8 L=1 W=4\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Cload vout 0 2p\n"
)


def test_index_baseline_components_keys_colliding_refdes_by_subckt():
    indexed = index_baseline_components(TWO_BUFFERS_NETLIST)

    assert indexed["BUF_P.Xcc"].params["W"] == "10"
    assert indexed["BUF_N.Xcc"].params["W"] == "20"
    # Ambiguous plain name gets no alias - it must not silently resolve to one of them.
    assert "Xcc" not in indexed


def test_index_baseline_components_aliases_a_unique_refdes_unqualified():
    # Back-compat: existing single-subckt benchmarks propose unqualified
    # refdes, and without this alias check_area_growth would find no
    # baseline and silently wave the change through.
    indexed = index_baseline_components(TWO_BUFFERS_NETLIST)

    assert indexed["Xonly"] is indexed["BUF_P.Xonly"]
    assert indexed["Cload"].value == "2p"


def test_area_gate_uses_the_scoped_baseline_not_a_colliding_one():
    baseline = index_baseline_components(TWO_BUFFERS_NETLIST)

    # 20 -> 30 is 1.5x against BUF_N's own baseline, at the tier limit.
    # Against BUF_P's 10 it would look like 3.0x and be rejected.
    approved, feedback = check_area_growth(
        baseline, [{"refdes": "BUF_N.Xcc", "param": "W", "new_value": "30"}]
    )

    assert approved, feedback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -k "scoped or colliding or aliases" -v`
Expected: FAIL — `KeyError: 'BUF_P.Xcc'`, because the current index is keyed by bare refdes.

- [ ] **Step 3: Write minimal implementation**

Replace `index_baseline_components` in `src/analogcoder/area_limits.py`:

```python
def index_baseline_components(netlist_text: str) -> dict[str, Component]:
    """Keyed by "<subckt>.<refdes>" for components declared inside a subckt
    and by plain refdes for top-level ones. A plain alias is added for any
    refdes occurring exactly once netlist-wide, so an unqualified proposal
    against an existing single-subckt benchmark still finds its baseline
    instead of silently bypassing the area gate (check_area_growth treats a
    missing baseline as unconstrained)."""
    parsed = parse_netlist(netlist_text)

    plain_counts: dict[str, int] = {}
    for component in parsed.top_components:
        plain_counts[component.refdes] = plain_counts.get(component.refdes, 0) + 1
    for subckt in parsed.subckts.values():
        for component in subckt.components:
            plain_counts[component.refdes] = plain_counts.get(component.refdes, 0) + 1

    indexed: dict[str, Component] = {}
    for component in parsed.top_components:
        indexed[component.refdes] = component
    for subckt in parsed.subckts.values():
        for component in subckt.components:
            indexed[f"{subckt.name}.{component.refdes}"] = component
            if plain_counts[component.refdes] == 1:
                indexed[component.refdes] = component
    return indexed
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. `two_stage_opamp`'s components all live in one subckt with unique refdes, so each keeps its plain alias.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/area_limits.py tests/unit/test_area_limits.py
git commit -m "feat: index baseline components by scoped refdes with unique-name aliases"
```

---

### Task 4: Let the schema and the agents express a scoped refdes

**Files:**
- Modify: `src/analogcoder/schemas.py:75` (`TUNER_SCHEMA` refdes pattern)
- Modify: `src/analogcoder/agents/tuner.py:5-29` (`TUNER_SYSTEM_PROMPT`)
- Modify: `src/analogcoder/agents/verifier.py:16-27` (`verify_pre` user prompt)
- Modify: `src/analogcoder/agents/analyzer.py:5-8` (`ANALYZER_SYSTEM_PROMPT`)
- Test: `tests/unit/test_schemas.py`, `tests/unit/test_verifier_agent.py`

**Interfaces:**
- Consumes: the `<subckt>.<refdes>` form from Task 2.
- Produces: nothing consumed by later tasks; Task 5 exercises the result end to end.

This task matters because `verify_pre`'s prompt currently instructs the verifier to reject anything other than a bare first token — it would reject every correct scoped proposal.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_schemas.py`:

```python
def test_tuner_schema_accepts_a_subckt_scoped_refdes():
    proposal = {
        "changes": [
            {
                "refdes": "BUF_N.Xcc",
                "param": "W",
                "old_value": "20",
                "new_value": "30",
                "reasoning": "widen the vbg1 buffer's compensation cap",
            }
        ],
        "overall_reasoning": "improve vbg1 settling",
    }

    jsonschema.validate(proposal, TUNER_SCHEMA)


def test_tuner_schema_rejects_a_malformed_scoped_refdes():
    proposal = {
        "changes": [
            {
                "refdes": "BUF_N.",
                "param": "W",
                "old_value": "20",
                "new_value": "30",
                "reasoning": "trailing dot is not a refdes",
            }
        ],
        "overall_reasoning": "x",
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(proposal, TUNER_SCHEMA)
```

Add to `tests/unit/test_verifier_agent.py`:

```python
@pytest.mark.asyncio
async def test_verify_pre_prompt_explains_subckt_scoped_refdes():
    # The prompt used to instruct the verifier that a refdes is only ever a
    # bare first token, which would make it reject every correct scoped
    # proposal.
    with patch(
        "analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value={})
    ) as mock_run:
        await verify_pre({}, {}, {}, "* netlist\n", object())

    prompt = mock_run.call_args.kwargs["user_prompt"]
    assert "<SUBCKT>.<refdes>" in prompt
    assert "ambiguous" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_schemas.py tests/unit/test_verifier_agent.py -k "scoped" -v`
Expected: FAIL — the schema pattern rejects the dot, and the prompt lacks the new text.

- [ ] **Step 3: Write minimal implementation**

In `src/analogcoder/schemas.py`, change the `TUNER_SCHEMA` refdes pattern:

```python
                    "refdes": {
                        "type": "string",
                        "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$",
                    },
```

In `src/analogcoder/agents/tuner.py`, insert this paragraph into `TUNER_SYSTEM_PROMPT` immediately before the final `Respond via the structured output schema.` line:

```
refdes MUST identify exactly one component. When the component sits inside a
.subckt and its refdes also appears in another .subckt, qualify it with the
subckt's name as "<SUBCKT>.<refdes>" (e.g. "BUF_N.Xcc" for the Xcc inside
".subckt BUF_N ..."). An unqualified refdes that appears in more than one
subckt is ambiguous and will be rejected. Note the scope is the subckt
definition: changing it changes every instance of that subckt.
```

In `src/analogcoder/agents/verifier.py`, replace the refdes sentences of the `verify_pre` user prompt (keep the `param` sentences that follow unchanged):

```python
        "Decide whether to approve this proposal before it is applied. A refdes "
        "is either the exact first token of a component line in the netlist "
        "above, or - when that component is declared inside a .subckt - that "
        'token qualified by the subckt name as "<SUBCKT>.<refdes>" (e.g. for '
        '"Xcc n1 vout ..." declared inside ".subckt BUF_N ...", a valid refdes '
        'is "Xcc" or "BUF_N.Xcc"). Reject any change whose refdes is neither - '
        "applying a change to a refdes that does not exist in the netlist "
        "silently does nothing at all, with no error. Also reject an "
        "unqualified refdes whose bare token appears inside more than one "
        "subckt: it is ambiguous and will be rejected when applied. "
```

In `src/analogcoder/agents/analyzer.py`, append to `ANALYZER_SYSTEM_PROMPT` before the closing quotes:

```
When a component is declared inside a .subckt, report its refdes qualified by
the subckt name as "<SUBCKT>.<refdes>" so it can be addressed unambiguously.
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Check existing verifier tests that assert on prompt content still hold; if one asserts the removed `"Rf.value"` wording, update it to match the new prompt rather than reinstating the old text.

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/schemas.py src/analogcoder/agents/tuner.py src/analogcoder/agents/verifier.py src/analogcoder/agents/analyzer.py tests/unit/test_schemas.py tests/unit/test_verifier_agent.py
git commit -m "feat: allow subckt-scoped refdes in the tuner schema and agent prompts"
```

---

### Task 5: End-to-end guard on a multi-subckt netlist

**Files:**
- Create: `tests/unit/test_scoped_refdes_end_to_end.py`

**Interfaces:**
- Consumes: `index_baseline_components` and `check_area_growth` from Task 3, `apply_changes` from Task 2.
- Produces: nothing.

This is the regression test for the defect the whole plan exists to fix: it must fail if any single task is reverted.

- [ ] **Step 1: Write the test**

```python
import pytest

from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import apply_changes

TWO_BUFFERS = (
    "* two buffers whose compensation caps share a refdes\n"
    ".subckt BUF_P vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10\n"
    ".ends BUF_P\n"
    ".subckt BUF_N vinp vinn vout vdd vss\n"
    "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=20\n"
    ".ends BUF_N\n"
    "Xb0 a b c vdd vss BUF_P\n"
    "Xb1 d e f vdd vss BUF_N\n"
    ".end\n"
)


def test_a_scoped_proposal_is_gated_and_applied_against_the_right_block():
    baseline = index_baseline_components(TWO_BUFFERS)
    change = {"refdes": "BUF_N.Xcc", "param": "W", "new_value": "30"}

    approved, feedback = check_area_growth(baseline, [change])
    assert approved, feedback

    out = apply_changes(TWO_BUFFERS, [change])

    assert "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=10" in out
    assert "Xcc n1 vout sky130_fd_pr__nfet_01v8 L=2 W=30" in out
    assert "W=20" not in out


def test_an_unqualified_colliding_proposal_is_refused_rather_than_misapplied():
    with pytest.raises(ValueError, match="ambiguous"):
        apply_changes(TWO_BUFFERS, [{"refdes": "Xcc", "param": "W", "new_value": "30"}])
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/unit/test_scoped_refdes_end_to_end.py -v`
Expected: PASS (all the machinery landed in Tasks 1-4).

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no skips beyond the two pre-existing integration skips.

- [ ] **Step 4: Update CLAUDE.md**

The "Known limitations / gotchas" section currently states that `netlist.py`'s `apply_changes`/`parse_netlist` "don't track subckt scope (a refdes collision between a subckt-local and top-level component could misfire) — known, deliberately deferred limitation, not fixed." Replace that bullet with:

```markdown
- **`netlist.py` tracks subckt scope.** A component inside a `.subckt` is
  addressable as `<SUBCKT>.<refdes>`; an unqualified refdes still works when
  it matches exactly one component netlist-wide, and raises `ValueError` when
  it is ambiguous rather than silently editing the first match. The scope is
  the subckt *definition*, so a change applies to every instance of it —
  two differently-tuned instances require two subckts.
```

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_scoped_refdes_end_to_end.py CLAUDE.md
git commit -m "test: guard scoped refdes gating and application end to end"
```

---

## Self-Review

**Spec coverage (Part 1).** Every module the spec's table names has a task: `netlist.py` (Tasks 1-2), `area_limits.py` (Task 3), `schemas.py` + `analyzer.py` + `verifier.py` (Task 4). The spec's back-compatibility requirement is covered by Task 2's unique-unqualified test and Task 3's alias test. The spec's "ambiguous is an error, not silent first-match" requirement is Task 2. Part 2 is deferred to its own plan, stated above.

**Placeholder scan.** No TBD/TODO. Every code step carries the literal code. Task 4's prompt edits quote the exact replacement text.

**Type consistency.** `split_scoped_refdes` returns `tuple[str | None, str]` and is used that way in `apply_changes`. `Component.scope` is `str | None`, set only in `parse_netlist`. `index_baseline_components` keeps its `dict[str, Component]` return type, so `check_area_growth`'s signature is untouched.

**One gap accepted deliberately.** If a top-level refdes collides with a subckt-local one, the plain key resolves to the top-level component and the subckt one is reachable only when qualified. Top-level components in these benchmarks are testbench harness elements that are never tuned, so this is documented rather than solved.
