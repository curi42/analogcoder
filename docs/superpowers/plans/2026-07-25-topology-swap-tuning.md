# Topology-Swap Tuning (Phase B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the tuner fall back to swapping the amplifier's internal topology (from a small curated library) as a last resort when repeated parameter tuning fails, instead of only ever changing existing component values.

**Architecture:** A deterministic counter owned by `orchestrator.py` (`consecutive_rollbacks`) tracks how many parameter-tuning attempts have been rolled back in a row. Once it hits a threshold, the orchestrator asks the tuner to pick a `topology_id` from a curated library (`topologies.py`) instead of a value change; the orchestrator applies the swap mechanically (`netlist.py:apply_topology_swap`), re-runs the analyzer (structure changed, so cached `tunable_params` are stale), and evaluates the result with the existing `verify_post` — no new verification agent is added.

**Tech Stack:** Same as the rest of the project — Python 3.14, `jsonschema` for structured-output validation, `pytest`/`pytest-asyncio` for tests, ngspice for real circuit verification of the library entries (already done in the design spec, not repeated here).

## Global Constraints

- Structural changes apply only to a netlist with **exactly one** `.subckt` block using the standard 5-port amp interface (`vinp vinn vout vdd vss`). Zero or multiple such blocks means the feature is simply unavailable for that run — no error, parameter tuning proceeds as it does today.
- No inductors in any topology library entry.
- The tuner never authors raw SPICE for a topology change — it only picks a `topology_id` from the ids it's offered. Validity is a closed-set membership check the orchestrator performs deterministically (no extra LLM verification call, unlike parameter tuning's `verify_pre`).
- `TOPOLOGY_SWITCH_THRESHOLD = 3` consecutive parameter-tuning rollbacks (at the current topology) before a topology swap is offered.
- Topology swapping shares the existing `MAX_OUTER_ITERATIONS = 10` budget — it does not get a separate allowance.
- `consecutive_rollbacks` resets to 0 after every topology-swap outer iteration (kept or rolled back), so parameter tuning always gets a fresh run under whatever topology is current before another swap is considered.
- v1 library has exactly two entries: `miller_basic` (existing `two_stage_opamp` circuit, unchanged) and `miller_nulling_resistor` (adds `Rz=500` in series with `Cc`). Both subckt bodies are copied verbatim from the ngspice-verified data in `docs/superpowers/specs/2026-07-25-topology-swap-tuning-design.md` — do not retype them from memory.

---

### Task 1: Topology library

**Files:**
- Create: `src/analogcoder/topologies.py`
- Test: `tests/unit/test_topologies.py`

**Interfaces:**
- Produces: `Topology` dataclass (`id: str`, `description: str`, `subckt_body: str`, `addresses: list[str]`), `TOPOLOGY_LIBRARY: dict[str, Topology]`. Later tasks import both names from `analogcoder.topologies`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_topologies.py
from analogcoder.netlist import parse_netlist
from analogcoder.topologies import TOPOLOGY_LIBRARY, Topology


def test_library_has_exactly_the_v1_entries():
    assert set(TOPOLOGY_LIBRARY.keys()) == {"miller_basic", "miller_nulling_resistor"}
    for topology_id, topology in TOPOLOGY_LIBRARY.items():
        assert isinstance(topology, Topology)
        assert topology.id == topology_id


def test_miller_basic_body_has_expected_components():
    body = TOPOLOGY_LIBRARY["miller_basic"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    refdes = {c.refdes for c in parsed.subckts["TEST"].components}
    assert refdes == {"Iref", "M9", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "Cc", "Ca"}


def test_miller_nulling_resistor_body_has_rz_in_series_with_cc():
    body = TOPOLOGY_LIBRARY["miller_nulling_resistor"].subckt_body
    wrapped = f".subckt TEST vinp vinn vout vdd vss\n{body}.ends TEST\n"
    parsed = parse_netlist(wrapped)
    subckt = parsed.subckts["TEST"]
    refdes = {c.refdes for c in subckt.components}
    assert refdes == {"Iref", "M9", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "Cc", "Rz", "Ca"}
    cc = next(c for c in subckt.components if c.refdes == "Cc")
    rz = next(c for c in subckt.components if c.refdes == "Rz")
    assert cc.nodes[1] == rz.nodes[0]  # Cc's second node feeds directly into Rz's first node
    assert rz.nodes[1] == "vout"


def test_miller_nulling_resistor_addresses_phase_margin():
    assert "phase_margin" in TOPOLOGY_LIBRARY["miller_nulling_resistor"].addresses
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_topologies.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.topologies'`

- [ ] **Step 3: Write the implementation**

```python
# src/analogcoder/topologies.py
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

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_topologies.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/topologies.py tests/unit/test_topologies.py
git commit -m "feat: add v1 topology library (miller_basic, miller_nulling_resistor)"
```

---

### Task 2: `apply_topology_swap` in `netlist.py`

**Files:**
- Modify: `src/analogcoder/netlist.py`
- Test: `tests/unit/test_netlist.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `apply_topology_swap(text: str, subckt_name: str, new_body: str) -> str`. Task 5 (orchestrator) calls this directly.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_netlist.py`, change line 1 from:

```python
from analogcoder.netlist import parse_netlist, apply_changes
```

to:

```python
import pytest

from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
```

Then append the following to the end of the file:

```python
def test_apply_topology_swap_replaces_interior_preserving_header_and_footer():
    netlist = (
        "* test netlist\n"
        ".subckt AMP vinp vinn vout vdd vss\n"
        "R1 vinp mid 1k\n"
        "R2 mid vout 2k\n"
        ".ends AMP\n"
        "Xamp1 a b c d e AMP\n"
        ".end\n"
    )
    new_body = "R3 vinp mid 5k\nR4 mid vout 6k\n"

    updated = apply_topology_swap(netlist, "AMP", new_body)

    assert ".subckt AMP vinp vinn vout vdd vss" in updated
    assert ".ends AMP" in updated
    assert "R1 vinp mid 1k" not in updated
    assert "R3 vinp mid 5k" in updated
    assert "R4 mid vout 6k" in updated
    assert "Xamp1 a b c d e AMP" in updated  # lines outside the block are untouched


def test_apply_topology_swap_raises_when_subckt_not_found():
    netlist = "* test\nR1 a b 1k\n.end\n"
    with pytest.raises(ValueError):
        apply_topology_swap(netlist, "AMP", "R1 a b 1k\n")


def test_apply_topology_swap_raises_when_subckt_not_closed():
    netlist = "* test\n.subckt AMP a b\nR1 a b 1k\n.end\n"
    with pytest.raises(ValueError):
        apply_topology_swap(netlist, "AMP", "R1 a b 1k\n")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -v`
Expected: FAIL with `ImportError: cannot import name 'apply_topology_swap'`

- [ ] **Step 3: Write the implementation**

Append to `src/analogcoder/netlist.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/netlist.py tests/unit/test_netlist.py
git commit -m "feat: add apply_topology_swap for mechanical subckt body replacement"
```

---

### Task 3: `TOPOLOGY_SCHEMA`

**Files:**
- Modify: `src/analogcoder/schemas.py`
- Test: `tests/unit/test_schemas.py`

**Interfaces:**
- Produces: `TOPOLOGY_SCHEMA` (module-level dict). Task 4 imports and uses it as `output_schema` for `propose_topology_swap`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_schemas.py`, change the import block from:

```python
from analogcoder.schemas import (
    ANALYZER_SCHEMA,
    JUDGE_SCHEMA,
    SIMULATION_SCHEMA,
    TUNER_SCHEMA,
    VERIFIER_POST_SCHEMA,
    VERIFIER_PRE_SCHEMA,
)
```

to:

```python
from analogcoder.schemas import (
    ANALYZER_SCHEMA,
    JUDGE_SCHEMA,
    SIMULATION_SCHEMA,
    TOPOLOGY_SCHEMA,
    TUNER_SCHEMA,
    VERIFIER_POST_SCHEMA,
    VERIFIER_PRE_SCHEMA,
)
```

Then append the following to the end of the file:

```python
def test_topology_schema_accepts_valid_payload():
    payload = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
    jsonschema.validate(payload, TOPOLOGY_SCHEMA)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("topology_id", "Miller.Nulling"),
        ("topology_id", "miller nulling resistor"),
        ("topology_id", "123_starts_with_digit"),
        ("confidence", -1),
        ("confidence", 101),
    ],
)
def test_topology_schema_rejects_invalid_values(field, bad_value):
    payload = {"topology_id": "miller_nulling_resistor", "reasoning": "x", "confidence": 90}
    payload[field] = bad_value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, TOPOLOGY_SCHEMA)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_schemas.py -v`
Expected: FAIL with `ImportError: cannot import name 'TOPOLOGY_SCHEMA'`

- [ ] **Step 3: Write the implementation**

Append to `src/analogcoder/schemas.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_schemas.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/schemas.py tests/unit/test_schemas.py
git commit -m "feat: add TOPOLOGY_SCHEMA for topology-swap proposals"
```

---

### Task 4: `propose_topology_swap` agent function

**Files:**
- Modify: `src/analogcoder/agents/tuner.py`
- Test: `tests/unit/test_tuner_agent.py`

**Interfaces:**
- Consumes: `Topology` from `analogcoder.topologies` (Task 1), `TOPOLOGY_SCHEMA` from `analogcoder.schemas` (Task 3).
- Produces: `propose_topology_swap(analysis: dict, judge_result: dict, available_topologies: list[Topology], rejection_feedback: str | None, backend: AgentBackend) -> dict`. Task 5 (orchestrator) and Task 6 (cli) call this signature.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_tuner_agent.py`, change line 5 from:

```python
from analogcoder.agents.tuner import propose_tuning
```

to:

```python
from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
from analogcoder.topologies import Topology
```

Then append the following to the end of the file:

```python
@pytest.mark.asyncio
async def test_propose_topology_swap_calls_run_agent_with_available_topologies():
    fake_result = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
    fake_backend = object()
    topologies = [
        Topology(
            id="miller_nulling_resistor",
            description="adds Rz to cancel the RHP zero",
            subckt_body="Cc outA vnull 2p\nRz vnull vout 500\n",
            addresses=["phase_margin"],
        ),
    ]
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await propose_topology_swap(
            analysis={"circuit_type": "two-stage op-amp"},
            judge_result={"overall_pass": False},
            available_topologies=topologies,
            rejection_feedback=None,
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["topology_id", "reasoning", "confidence"]
    assert kwargs["backend"] is fake_backend
    assert "miller_nulling_resistor" in kwargs["user_prompt"]
    assert "adds Rz to cancel the RHP zero" in kwargs["user_prompt"]


@pytest.mark.asyncio
async def test_propose_topology_swap_includes_rejection_feedback_in_prompt():
    fake_backend = object()
    topologies = [
        Topology(id="miller_basic", description="baseline", subckt_body="", addresses=[]),
    ]
    with patch(
        "analogcoder.agents.tuner.run_agent",
        new=AsyncMock(return_value={"topology_id": "miller_basic", "reasoning": "x", "confidence": 50}),
    ) as mock_run:
        await propose_topology_swap(
            analysis={},
            judge_result={},
            available_topologies=topologies,
            rejection_feedback="'bogus_id' is not an available untried topology.",
            backend=fake_backend,
        )
    _, kwargs = mock_run.call_args
    assert "is not an available untried topology" in kwargs["user_prompt"]
```

Existing tests in this file already check `propose_tuning`; leave those as-is.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_tuner_agent.py -v`
Expected: FAIL with `ImportError: cannot import name 'propose_topology_swap'`

- [ ] **Step 3: Write the implementation**

In `src/analogcoder/agents/tuner.py`, change the import block from:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import TUNER_SCHEMA
```

to:

```python
from analogcoder.agents.agent_runtime import run_agent
from analogcoder.agents.backend import AgentBackend
from analogcoder.schemas import TOPOLOGY_SCHEMA, TUNER_SCHEMA
from analogcoder.topologies import Topology
```

Then append the following to the end of the file:

```python
TOPOLOGY_TUNER_SYSTEM_PROMPT = """You are an analog circuit tuning specialist. Parameter
tuning has been tried repeatedly and failed to meet the target criteria. You must now
choose ONE topology from the list of available, pre-verified topologies below to replace
the amplifier's internal structure.

topology_id MUST be exactly one of the ids listed as available - never invent a new id,
never reuse a topology_id that is not in the available list (it has likely already been
tried and rejected). Base your choice on which listed topology's description most
directly addresses the currently failing criteria.

Respond via the structured output schema."""


async def propose_topology_swap(
    analysis: dict,
    judge_result: dict,
    available_topologies: list[Topology],
    rejection_feedback: str | None,
    backend: AgentBackend,
) -> dict:
    topology_descriptions = "\n".join(
        f"- {t.id}: {t.description} (addresses: {t.addresses})" for t in available_topologies
    )
    user_prompt = (
        f"Circuit analysis: {analysis}\n"
        f"Judge result: {judge_result}\n"
        f"Available topologies:\n{topology_descriptions}\n"
        f"Rejection feedback (if retrying): {rejection_feedback}"
    )
    return await run_agent(
        system_prompt=TOPOLOGY_TUNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=TOPOLOGY_SCHEMA,
        backend=backend,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_tuner_agent.py -v`
Expected: PASS (all tests in the file, including the pre-existing `propose_tuning` ones)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/agents/tuner.py tests/unit/test_tuner_agent.py
git commit -m "feat: add propose_topology_swap agent function"
```

---

### Task 5: Orchestrator integration

**Files:**
- Modify: `src/analogcoder/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `apply_topology_swap`, `parse_netlist` from `analogcoder.netlist` (Tasks 2 and existing); `TOPOLOGY_LIBRARY` from `analogcoder.topologies` (Task 1). Calls `agents.propose_topology(analysis, judge_result, available_topologies, rejection_feedback)` matching Task 4's signature (minus `backend`, which the wiring closure supplies — see Task 6).
- Produces: `OrchestratorAgents.propose_topology: Callable` (new required field). Task 6 (cli) must supply it.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_orchestrator.py`, change line 14 (right after `FAKE_PROPOSAL`) from:

```python
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}
```

to:

```python
FAKE_PROPOSAL = {"proposed_changes": [{"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k"}]}
SUBCKT_NETLIST = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "R1 vinp mid 1k\n"
    "R2 mid vout 2k\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)
FAKE_TOPOLOGY_PROPOSAL = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
```

Then change `make_agents()` from:

```python
def make_agents(**overrides):
    async def default_analyze(netlist_text):
        return {"circuit_type": "inverting amplifier"}

    async def default_simulate(netlist_text, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return FAKE_PROPOSAL

    async def default_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    defaults = dict(
        analyze=default_analyze,
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
    )
    defaults.update(overrides)
    return OrchestratorAgents(**defaults)
```

to:

```python
def make_agents(**overrides):
    async def default_analyze(netlist_text):
        return {"circuit_type": "inverting amplifier"}

    async def default_simulate(netlist_text, spec):
        return {"measurements": {"gain_db": 20.0}, "status": "success", "warnings": []}

    async def default_judge(measurements, spec):
        return PASS_JUDGE

    async def default_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return FAKE_PROPOSAL

    async def default_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def default_verify_post(prev_judge, new_judge, applied_changes):
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "ok"}

    async def default_propose_topology(analysis, judge_result, available_topologies, rejection_feedback):
        return FAKE_TOPOLOGY_PROPOSAL

    defaults = dict(
        analyze=default_analyze,
        simulate=default_simulate,
        judge=default_judge,
        tune=default_tune,
        verify_pre=default_verify_pre,
        verify_post=default_verify_post,
        propose_topology=default_propose_topology,
    )
    defaults.update(overrides)
    return OrchestratorAgents(**defaults)
```

Then append these test functions to the end of the file:

```python
@pytest.mark.asyncio
async def test_topology_swap_never_offered_without_exactly_one_subckt(tmp_path):
    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return FAKE_TOPOLOGY_PROPOSAL

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration("* netlist\n.end\n", FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    assert propose_topology_calls["count"] == 0


@pytest.mark.asyncio
async def test_topology_swap_triggers_after_threshold_consecutive_rollbacks(tmp_path):
    judge_calls = {"count": 0}

    async def judge_sequence(measurements, spec):
        judge_calls["count"] += 1
        return PASS_JUDGE if judge_calls["count"] == 8 else FAIL_JUDGE

    verify_post_calls = {"count": 0}

    async def verify_post_sequence(prev_judge, new_judge, applied_changes):
        verify_post_calls["count"] += 1
        if verify_post_calls["count"] <= 3:
            return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}
        return {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "fixed"}

    propose_topology_calls = []

    async def propose_topology(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls.append([t.id for t in available_topologies])
        return {"topology_id": available_topologies[0].id, "reasoning": "matches phase margin gap", "confidence": 90}

    agents = make_agents(judge=judge_sequence, verify_post=verify_post_sequence, propose_topology=propose_topology)
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(SUBCKT_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "PASS"
    assert result["iterations_used"] == 4
    assert len(propose_topology_calls) == 1
    assert set(propose_topology_calls[0]) == {"miller_basic", "miller_nulling_resistor"}
    assert len(state.netlist_versions) == 2  # v0 initial + v1 after the kept topology swap


@pytest.mark.asyncio
async def test_topology_swap_repeatedly_invalid_id_fails_run(tmp_path):
    async def always_bad_topology(analysis, judge_result, available_topologies, rejection_feedback):
        return {"topology_id": "not_a_real_topology", "reasoning": "x", "confidence": 50}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=always_bad_topology,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(SUBCKT_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "topology proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_topology_swap_rollback_restores_analysis_without_reanalyzing(tmp_path):
    analyze_calls = {"count": 0}

    async def counting_analyze(netlist_text):
        analyze_calls["count"] += 1
        return {"circuit_type": "amp", "call": analyze_calls["count"]}

    async def verify_post_always_rollback(prev_judge, new_judge, applied_changes):
        return {"improved": False, "regressed_criteria": ["gain"], "recommendation": "rollback", "feedback": "no"}

    propose_topology_calls = {"count": 0}

    async def propose_topology_once(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        analyze=counting_analyze,
        judge=lambda m, s: _async(FAIL_JUDGE),
        verify_post=verify_post_always_rollback,
        propose_topology=propose_topology_once,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(SUBCKT_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"
    # initial analyze + one re-analyze per topology attempt (library has 2 entries,
    # each gets exactly one attempt across the 10-iteration budget); rollback restores
    # the saved pre-swap analysis instead of triggering a third analyze call
    assert analyze_calls["count"] == 3
    assert propose_topology_calls["count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: FAIL — `TypeError: OrchestratorAgents.__init__() missing 1 required positional argument: 'propose_topology'` on every test (existing ones too, since `make_agents()` didn't have the field yet before Step 1's edit takes effect — after Step 1 this failure mode shifts to the new tests failing differently once orchestrator.py is unmodified, e.g. `AttributeError`/`TypeError` from the orchestrator not calling `agents.propose_topology` at all). Confirm all 4 new tests fail; the 7 pre-existing tests should still pass once `make_agents()` has the default (they don't exercise the new branch).

- [ ] **Step 3: Write the implementation**

Replace the full contents of `src/analogcoder/orchestrator.py`:

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
from analogcoder.state import RunState
from analogcoder.topologies import TOPOLOGY_LIBRARY

MAX_OUTER_ITERATIONS = 10
MAX_TUNING_RETRIES = 3
TOPOLOGY_SWITCH_THRESHOLD = 3


@dataclass
class OrchestratorAgents:
    analyze: Callable
    simulate: Callable
    judge: Callable
    tune: Callable
    verify_pre: Callable
    verify_post: Callable
    propose_topology: Callable


def _final_result(
    status: str, state: RunState, iterations_used: int, judge_result: dict | None, failure_reason: str | None = None
) -> dict:
    result = {
        "status": status,
        "final_netlist_path": state.current_netlist_path(),
        "iterations_used": iterations_used,
        "final_criteria": judge_result["criteria"] if judge_result else [],
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


async def run_orchestration(initial_netlist_text: str, spec, state: RunState, agents: OrchestratorAgents) -> dict:
    state.push_netlist_version(initial_netlist_text)
    outer_iter = 0
    judge_result: dict = {}

    try:
        analysis = await agents.analyze(initial_netlist_text)
        state.log_event("analysis", analysis)

        topology_swap_available = len(parse_netlist(initial_netlist_text).subckts) == 1
        tried_topologies: set[str] = set()
        consecutive_rollbacks = 0

        tuning_history: list[dict] = []

        for outer_iter in range(1, MAX_OUTER_ITERATIONS + 1):
            with open(state.current_netlist_path()) as f:
                netlist_text = f.read()

            sim_result = await agents.simulate(netlist_text, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, **sim_result})

            judge_result = await agents.judge(sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, **judge_result})

            if judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, judge_result)

            untried_topologies = (
                [t for t in TOPOLOGY_LIBRARY.values() if t.id not in tried_topologies]
                if topology_swap_available and consecutive_rollbacks >= TOPOLOGY_SWITCH_THRESHOLD
                else []
            )

            if untried_topologies:
                topology_id = None
                rejection_feedback = None
                for retry in range(1, MAX_TUNING_RETRIES + 1):
                    proposal = await agents.propose_topology(
                        analysis, judge_result, untried_topologies, rejection_feedback
                    )
                    state.log_event("topology_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                    candidate = proposal["topology_id"]
                    if candidate in TOPOLOGY_LIBRARY and candidate not in tried_topologies:
                        topology_id = candidate
                        break
                    rejection_feedback = (
                        f"'{candidate}' is not an available untried topology. "
                        f"Choose one of: {[t.id for t in untried_topologies]}"
                    )

                if topology_id is None:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="topology proposal repeatedly rejected",
                    )

                tried_topologies.add(topology_id)
                topology = TOPOLOGY_LIBRARY[topology_id]
                subckt_name = next(iter(parse_netlist(netlist_text).subckts))
                new_netlist_text = apply_topology_swap(netlist_text, subckt_name, topology.subckt_body)
                state.push_netlist_version(new_netlist_text)

                pre_swap_analysis = analysis
                analysis = await agents.analyze(new_netlist_text)
                state.log_event("analysis", {"outer_iter": outer_iter, "topology_id": topology_id, **analysis})

                new_sim_result = await agents.simulate(new_netlist_text, spec)
                state.log_event(
                    "simulation", {"outer_iter": outer_iter, "post_topology_swap": True, **new_sim_result}
                )

                new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
                state.log_event(
                    "judge", {"outer_iter": outer_iter, "post_topology_swap": True, **new_judge_result}
                )

                post_review = await agents.verify_post(
                    judge_result, new_judge_result, [{"topology_id": topology_id}]
                )
                state.log_event("verify_post", {"outer_iter": outer_iter, "topology_swap": True, **post_review})

                consecutive_rollbacks = 0

                if post_review["recommendation"] == "rollback":
                    state.rollback()
                    analysis = pre_swap_analysis
                    judge_result = new_judge_result
                    continue

                if new_judge_result["overall_pass"]:
                    return _final_result("PASS", state, outer_iter, new_judge_result)

                judge_result = new_judge_result
                continue

            approved_proposal = None
            rejection_feedback = None
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                proposal = await agents.tune(analysis, judge_result, tuning_history, rejection_feedback, netlist_text)
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                review = await agents.verify_pre(analysis, judge_result, proposal, netlist_text)
                state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

                if review["approved"]:
                    approved_proposal = proposal
                    break
                rejection_feedback = review["feedback"]

            if approved_proposal is None:
                return _final_result(
                    "FAIL", state, outer_iter, judge_result, failure_reason="tuning proposal repeatedly rejected"
                )

            new_netlist_text = apply_changes(netlist_text, approved_proposal["proposed_changes"])
            state.push_netlist_version(new_netlist_text)

            new_sim_result = await agents.simulate(new_netlist_text, spec)
            state.log_event("simulation", {"outer_iter": outer_iter, "post_tuning": True, **new_sim_result})

            new_judge_result = await agents.judge(new_sim_result["measurements"], spec)
            state.log_event("judge", {"outer_iter": outer_iter, "post_tuning": True, **new_judge_result})

            post_review = await agents.verify_post(
                judge_result, new_judge_result, approved_proposal["proposed_changes"]
            )
            state.log_event("verify_post", {"outer_iter": outer_iter, **post_review})

            tuning_history.append({
                "outer_iter": outer_iter,
                "proposal": approved_proposal,
                "recommendation": post_review["recommendation"],
            })

            if post_review["recommendation"] == "rollback":
                state.rollback()
                consecutive_rollbacks += 1
                judge_result = new_judge_result
                continue

            consecutive_rollbacks = 0

            if new_judge_result["overall_pass"]:
                return _final_result("PASS", state, outer_iter, new_judge_result)

            judge_result = new_judge_result

        return _final_result("FAIL", state, MAX_OUTER_ITERATIONS, judge_result, failure_reason="max iterations reached")
    except AgentExecutionError as exc:
        return _final_result(
            "FAIL", state, max(outer_iter - 1, 0), judge_result, failure_reason=f"agent execution error: {exc}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: PASS (all 11 tests — 7 pre-existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: add deterministic topology-swap fallback to orchestrator"
```

---

### Task 6: CLI wiring

**Files:**
- Modify: `src/analogcoder/cli.py`
- Test: `tests/unit/test_cli.py` (no new test needed — see Step 4)

**Interfaces:**
- Consumes: `propose_topology_swap` from `analogcoder.agents.tuner` (Task 4), `OrchestratorAgents.propose_topology` field (Task 5).

- [ ] **Step 1: Modify the import line**

In `src/analogcoder/cli.py`, change:

```python
from analogcoder.agents.tuner import propose_tuning
```

to:

```python
from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
```

- [ ] **Step 2: Add the wiring closure**

In `src/analogcoder/cli.py`, immediately after the existing `verify_post_fn` definition (before the `agents = OrchestratorAgents(...)` call), add:

```python
    async def propose_topology_fn(analysis, judge_result, available_topologies, rejection_feedback):
        return await propose_topology_swap(
            analysis, judge_result, available_topologies, rejection_feedback, agent_backend
        )
```

- [ ] **Step 3: Pass it into `OrchestratorAgents`**

Change:

```python
    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
    )
```

to:

```python
    agents = OrchestratorAgents(
        analyze=analyze_fn,
        simulate=simulate_fn,
        judge=judge_fn,
        tune=tune_fn,
        verify_pre=verify_pre_fn,
        verify_post=verify_post_fn,
        propose_topology=propose_topology_fn,
    )
```

- [ ] **Step 4: Run the existing CLI test suite to confirm the wiring is complete**

Run: `.venv/bin/python -m pytest tests/unit/test_cli.py -v`
Expected: PASS (all tests, including `test_run_wires_orchestration_and_returns_its_result`, which constructs a real `OrchestratorAgents(...)` inside `_run()` before `run_orchestration` is invoked — if `propose_topology_fn` were missing this test would fail with `TypeError: missing required argument`, so its passing is the regression check for this task). No new test file changes are needed because no existing test inspects the `OrchestratorAgents` object's fields directly.

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass. Confirm the baseline count from your own terminal (run this same command before Task 1 starts, if you haven't already, to know the starting number) and confirm it has grown by the number of new tests added in Tasks 1-5 (4 + 3 + 6 + 2 + 4 = 19; Task 6 itself adds none) plus 2 skipped integration tests unchanged. Do not hardcode an expected absolute number — verify by comparison to your own recorded baseline.

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/cli.py
git commit -m "feat: wire propose_topology_swap into the CLI's agent backend"
```

---

### Task 7: `spec_topology_required.yaml` benchmark variant

**Files:**
- Create: `benchmarks/two_stage_opamp/spec_topology_required.yaml`
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Consumes: nothing from other tasks (this is a standalone YAML fixture + a `load_spec` regression check).
- Produces: a benchmark spec file used for manual end-to-end validation after all tasks are complete (see "Manual Validation" below) — not consumed by any other task's code.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_spec.py`:

```python
def test_topology_required_spec_has_stricter_phase_margin_threshold():
    spec = load_spec("benchmarks/two_stage_opamp/spec_topology_required.yaml")
    baseline = load_spec("benchmarks/two_stage_opamp/spec.yaml")

    phase_margin = next(c for c in spec.criteria if c.name == "phase_margin")
    baseline_phase_margin = next(c for c in baseline.criteria if c.name == "phase_margin")

    assert phase_margin.threshold == 65.0
    assert phase_margin.threshold > baseline_phase_margin.threshold
    # gain and UGBW thresholds are unchanged from the baseline spec
    assert {c.name: c.threshold for c in spec.criteria if c.name != "phase_margin"} == {
        c.name: c.threshold for c in baseline.criteria if c.name != "phase_margin"
    }
```

This test assumes the working directory is the repository root, matching how `.venv/bin/python -m pytest -q` is documented to be run in `CLAUDE.md`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: FAIL with `FileNotFoundError: [Errno 2] No such file or directory: 'benchmarks/two_stage_opamp/spec_topology_required.yaml'`

- [ ] **Step 3: Create the spec file**

Create `benchmarks/two_stage_opamp/spec_topology_required.yaml`:

```yaml
circuit_name: two_stage_opamp
analyses: ["ac"]
control_block: |
  .control
  set units=degrees
  ac dec 20 1 100meg
  meas ac gain_db find vdb(vout) at=1
  meas ac ugbw_hz when vdb(vout)=0
  meas ac phase_margin_deg find vp(vout) when vdb(vout)=0
  .endc
criteria:
  - name: dc_gain
    measurement: gain_db
    operator: ">="
    threshold: 70.0
    unit: dB
  - name: unity_gain_bandwidth
    measurement: ugbw_hz
    operator: ">="
    threshold: 20000000.0
    unit: Hz
  - name: phase_margin
    measurement: phase_margin_deg
    operator: ">="
    threshold: 65.0
    unit: deg
```

Identical to `benchmarks/two_stage_opamp/spec.yaml` except `phase_margin`'s threshold (60.0 → 65.0). Per the design spec's verified `Cc`-sweep data, no value of `Cc` alone reaches 65° phase margin without `unity_gain_bandwidth` dropping below 20MHz, and `miller_nulling_resistor` does reach it (verified at 66.13° phase margin, 42.97MHz UGBW) — but a later real Claude run found this spec is *also* solvable by combining `Cc` with `M6.W`, a parameter this design's `Cc`-only sweep didn't explore. See the design spec's "Update after a real end-to-end run" note: this file still proves the topology-swap mechanism works when exercised, but doesn't reliably force it for a strong model.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_spec.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add benchmarks/two_stage_opamp/spec_topology_required.yaml tests/unit/test_spec.py
git commit -m "feat: add spec_topology_required benchmark variant, unsolvable by parameter tuning alone"
```

## Manual Validation (after all tasks are complete and reviewed)

Not a task with automated assertions — this is the same kind of real-run
verification already used throughout this project (`CLAUDE.md`'s "Known
limitations" section was built this way). Run after the whole branch passes
review:

```bash
.venv/bin/analogcoder \
  --netlist benchmarks/two_stage_opamp/netlist.cir \
  --spec benchmarks/two_stage_opamp/spec_topology_required.yaml \
  --run-dir runs/topology_swap_claude_1
```

Confirm in `runs/topology_swap_claude_1/history.jsonl`:
- Several `tuning_proposal`/`verify_post` (`rollback`) cycles at `miller_basic` before any `topology_proposal` event appears (proves the threshold gate works, not that topology swap fires immediately).
- A `topology_proposal` event with `topology_id: "miller_nulling_resistor"`, followed by an `analysis` event carrying `"topology_id": "miller_nulling_resistor"` (proves re-analysis happened).
- Final `result.json` has `"status": "PASS"`.

If the real run surfaces a bug (malformed topology_id, wrong node wiring,
etc.), follow this session's established pattern: root-cause it with
`systematic-debugging`, write a failing test, fix, verify, commit — don't
just patch and move on.
