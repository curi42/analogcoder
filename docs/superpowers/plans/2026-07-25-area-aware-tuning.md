# Area-Aware Parameter Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic gate that rejects (with retryable feedback) parameter-tuning proposals that grow a component's size beyond what its size tier allows, measured cumulatively against the run's starting netlist.

**Architecture:** A new pure-Python module (`area_limits.py`) computes per-component growth ratios and tier-based limits; the orchestrator calls it inside the existing parameter-tuning retry loop, before the LLM-based `verify_pre` call, and treats exhausting all retries on area rejections alone as a rollback (continue to the next outer iteration) rather than the existing hard-fail-the-run behavior — so repeated area rejection can escalate into a topology-swap offer instead of just ending the run.

**Tech Stack:** Same as the rest of the project — Python 3.14, `pytest`/`pytest-asyncio` for tests. No new external dependencies.

## Global Constraints

- Growth is measured **cumulatively against `netlist_v0`** (the run's starting netlist), not against the immediately-prior value.
- Each **component is checked independently** — no cross-type area budget (no PDK exists in this project to derive a real conversion factor between, e.g., transistor W×L and capacitor farads).
- Only **growth** (ratio > 1) is constrained. Shrinking a component always passes, regardless of tier.
- Covered types, by `refdes[0].upper()` (matches the existing `Component.ctype` convention): `M` (transistor, W×L), `C` (capacitor, value), `R` (resistor, value). Everything else (`I`, `V`, etc.) is unconstrained.
- Size tiers (baseline value → allowed growth multiplier), exact values:
  - Transistor `W`: `< 30e-6` → `3.0x`; `< 80e-6` → `2.0x`; else → `1.5x`
  - Capacitor `C`: `< 3e-12` → `3.0x`; `< 10e-12` → `2.0x`; else → `1.5x`
  - Resistor `R`: `< 1e3` → `3.0x`; `< 10e3` → `2.0x`; else → `1.5x`
- For a transistor, the baseline **`W`** value always selects the tier (not `L`, which is rarely tuned in this project) — regardless of which of `W`/`L` the current proposal actually changes. The pass/fail ratio itself is still the true combined `W×L` growth factor when both are proposed together in one change set.
- Topology-swap proposals are **out of scope** for this gate — a swap replaces the whole subckt with a pre-verified template, not an incremental value change.
- **Exhaustion behavior differs by rejection cause.** If all `MAX_TUNING_RETRIES` attempts in one `outer_iter` were rejected by `verify_pre` (at least one attempt reached and was rejected by it), the run hard-fails immediately with `"tuning proposal repeatedly rejected"` — unchanged from today. If every attempt was caught by the area gate and none ever reached `verify_pre`, treat it like a parameter-tuning rollback instead: `consecutive_rollbacks += 1`, no netlist rollback needed (nothing was ever applied), and `continue` to the next `outer_iter`.

---

### Task 1: `parse_spice_value` in `netlist.py`

**Files:**
- Modify: `src/analogcoder/netlist.py`
- Test: `tests/unit/test_netlist.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `parse_spice_value(s: str) -> float`. Task 2 imports and uses this.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_netlist.py`, change line 1 from:

```python
import pytest

from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
```

to:

```python
import pytest

from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist, parse_spice_value
```

Then append the following to the end of the file:

```python
def test_parse_spice_value_no_suffix():
    assert parse_spice_value("500") == 500.0


def test_parse_spice_value_pico():
    assert parse_spice_value("2p") == 2e-12


def test_parse_spice_value_nano():
    assert parse_spice_value("40n") == 40e-9


def test_parse_spice_value_micro():
    assert parse_spice_value("40u") == 40e-6


def test_parse_spice_value_milli():
    assert parse_spice_value("5m") == 5e-3


def test_parse_spice_value_kilo():
    assert parse_spice_value("10k") == 10e3


def test_parse_spice_value_mega_uses_full_meg_suffix():
    assert parse_spice_value("1.5meg") == 1.5e6


def test_parse_spice_value_bare_m_is_milli_not_mega():
    assert parse_spice_value("2MEG") == 2e6
    assert parse_spice_value("2m") == 2e-3


def test_parse_spice_value_giga_and_tera():
    assert parse_spice_value("3g") == 3e9
    assert parse_spice_value("1t") == 1e12


def test_parse_spice_value_femto():
    assert parse_spice_value("100f") == 100e-15


def test_parse_spice_value_negative_number():
    assert parse_spice_value("-5u") == -5e-6


def test_parse_spice_value_scientific_notation():
    assert parse_spice_value("2e-3") == 2e-3


def test_parse_spice_value_ignores_trailing_unit_letters():
    assert parse_spice_value("5pF") == 5e-12
    assert parse_spice_value("40uOHM") == 40e-6


def test_parse_spice_value_case_insensitive():
    assert parse_spice_value("2P") == 2e-12
    assert parse_spice_value("40U") == 40e-6


def test_parse_spice_value_raises_on_invalid_input():
    with pytest.raises(ValueError):
        parse_spice_value("not-a-number")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_spice_value'`

- [ ] **Step 3: Write the implementation**

In `src/analogcoder/netlist.py`, change the imports at the top of the file from:

```python
import re
from dataclasses import dataclass, field
```

to (unchanged — `re` is already imported; no new import needed).

Then append the following to the end of the file:

```python
_SPICE_VALUE_RE = re.compile(r"^(-?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$")

# Longest/most-specific suffix first: "meg" must be checked before "m", or
# "1.5meg" would incorrectly match "m" (milli) since "meg".startswith("m").
_SPICE_SUFFIXES = [
    ("meg", 1e6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
]


def parse_spice_value(s: str) -> float:
    match = _SPICE_VALUE_RE.match(s.strip())
    if not match:
        raise ValueError(f"not a valid SPICE numeric literal: {s!r}")
    number_str, suffix = match.groups()
    number = float(number_str)
    suffix_lower = suffix.lower()
    for name, multiplier in _SPICE_SUFFIXES:
        if suffix_lower.startswith(name):
            return number * multiplier
    return number
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_netlist.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/netlist.py tests/unit/test_netlist.py
git commit -m "feat: add parse_spice_value for SPICE numeric literal parsing"
```

---

### Task 2: `area_limits.py`

**Files:**
- Create: `src/analogcoder/area_limits.py`
- Test: `tests/unit/test_area_limits.py`

**Interfaces:**
- Consumes: `parse_spice_value` from `analogcoder.netlist` (Task 1); `Component`, `parse_netlist` from `analogcoder.netlist` (already exist).
- Produces: `SizeTier` dataclass, `TIERS_BY_CTYPE: dict[str, list[SizeTier]]`, `allowed_multiplier_for(ctype: str, baseline_value: float) -> float | None`, `index_baseline_components(netlist_text: str) -> dict[str, Component]`, `check_area_growth(baseline_components: dict[str, Component], proposed_changes: list[dict]) -> tuple[bool, str | None]`. Task 3 (orchestrator) imports and calls `index_baseline_components` and `check_area_growth`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_area_limits.py`:

```python
from analogcoder.area_limits import (
    allowed_multiplier_for,
    check_area_growth,
    index_baseline_components,
)

NETLIST_WITH_SUBCKT = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    "Cc outA vout 2p\n"
    ".ends AMP\n"
    "Iref nb1 vdd 100u\n"
    "Rz vnull vout 500\n"
    ".end\n"
)


def test_index_baseline_components_finds_top_level_and_subckt_components():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    assert set(baseline.keys()) == {"M6", "Cc", "Iref", "Rz"}
    assert baseline["M6"].params["W"] == "40u"
    assert baseline["Cc"].value == "2p"
    assert baseline["Rz"].value == "500"


def test_allowed_multiplier_for_transistor_tiers():
    assert allowed_multiplier_for("M", 20e-6) == 3.0
    assert allowed_multiplier_for("M", 50e-6) == 2.0
    assert allowed_multiplier_for("M", 100e-6) == 1.5


def test_allowed_multiplier_for_capacitor_tiers():
    assert allowed_multiplier_for("C", 1e-12) == 3.0
    assert allowed_multiplier_for("C", 5e-12) == 2.0
    assert allowed_multiplier_for("C", 15e-12) == 1.5


def test_allowed_multiplier_for_resistor_tiers():
    assert allowed_multiplier_for("R", 500) == 3.0
    assert allowed_multiplier_for("R", 5000) == 2.0
    assert allowed_multiplier_for("R", 50000) == 1.5


def test_allowed_multiplier_for_unconstrained_ctype_returns_none():
    assert allowed_multiplier_for("I", 100e-6) is None


def test_check_area_growth_passes_when_within_tier_limit():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    # M6 W baseline 40u is in the medium tier (2.0x allowed); 40u->70u is 1.75x
    changes = [{"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "70u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_rejects_when_exceeding_tier_limit():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    # 40u->100u is 2.5x, exceeds the 2.0x medium-tier limit for a 40u baseline
    changes = [{"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False
    assert "M6" in feedback
    assert "2.0" in feedback


def test_check_area_growth_always_passes_shrinkage():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "10u"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_combines_w_and_l_for_same_refdes():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    # W 40u->60u (1.5x) * L 1u->2u (2x) = 3.0x combined; baseline W=40u -> medium
    # tier only allows 2.0x, so this is rejected even though neither single
    # dimension alone would be.
    changes = [
        {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "60u"},
        {"refdes": "M6", "param": "L", "old_value": "1u", "new_value": "2u"},
    ]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is False


def test_check_area_growth_ignores_unconstrained_ctype():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "Iref", "param": "value", "old_value": "100u", "new_value": "10m"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None


def test_check_area_growth_skips_unknown_refdes():
    baseline = index_baseline_components(NETLIST_WITH_SUBCKT)
    changes = [{"refdes": "NotInBaseline", "param": "value", "old_value": "1k", "new_value": "100k"}]
    approved, feedback = check_area_growth(baseline, changes)
    assert approved is True
    assert feedback is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analogcoder.area_limits'`

- [ ] **Step 3: Write the implementation**

Create `src/analogcoder/area_limits.py`:

```python
from dataclasses import dataclass

from analogcoder.netlist import Component, parse_netlist, parse_spice_value


@dataclass(frozen=True)
class SizeTier:
    max_value: float | None  # None = this is the top/unbounded tier
    allowed_multiplier: float


TRANSISTOR_TIERS: list[SizeTier] = [
    SizeTier(max_value=30e-6, allowed_multiplier=3.0),
    SizeTier(max_value=80e-6, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
CAPACITOR_TIERS: list[SizeTier] = [
    SizeTier(max_value=3e-12, allowed_multiplier=3.0),
    SizeTier(max_value=10e-12, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
RESISTOR_TIERS: list[SizeTier] = [
    SizeTier(max_value=1e3, allowed_multiplier=3.0),
    SizeTier(max_value=10e3, allowed_multiplier=2.0),
    SizeTier(max_value=None, allowed_multiplier=1.5),
]
TIERS_BY_CTYPE: dict[str, list[SizeTier]] = {
    "M": TRANSISTOR_TIERS,
    "C": CAPACITOR_TIERS,
    "R": RESISTOR_TIERS,
}


def allowed_multiplier_for(ctype: str, baseline_value: float) -> float | None:
    tiers = TIERS_BY_CTYPE.get(ctype)
    if tiers is None:
        return None
    for tier in tiers:
        if tier.max_value is None or baseline_value < tier.max_value:
            return tier.allowed_multiplier
    return tiers[-1].allowed_multiplier


def index_baseline_components(netlist_text: str) -> dict[str, Component]:
    parsed = parse_netlist(netlist_text)
    components = list(parsed.top_components)
    for subckt in parsed.subckts.values():
        components.extend(subckt.components)
    return {c.refdes: c for c in components}


def _baseline_value_for(component: Component, param: str) -> str | None:
    if param == "value":
        return component.value
    return component.params.get(param)


def _tier_baseline_value(component: Component) -> float | None:
    """The dimension used to pick a size tier: baseline W for transistors
    (L rarely varies in this project), the component's own value for C/R."""
    if component.ctype == "M":
        w = component.params.get("W")
        return parse_spice_value(w) if w is not None else None
    if component.value is not None:
        return parse_spice_value(component.value)
    return None


def check_area_growth(
    baseline_components: dict[str, Component], proposed_changes: list[dict]
) -> tuple[bool, str | None]:
    by_refdes: dict[str, list[dict]] = {}
    for change in proposed_changes:
        by_refdes.setdefault(change["refdes"], []).append(change)

    violations: list[str] = []
    for refdes, changes in by_refdes.items():
        component = baseline_components.get(refdes)
        if component is None:
            continue

        combined_ratio = 1.0
        for change in changes:
            baseline_str = _baseline_value_for(component, change["param"])
            if baseline_str is None:
                continue
            baseline_value = parse_spice_value(baseline_str)
            if baseline_value <= 0:
                continue
            new_value = parse_spice_value(change["new_value"])
            combined_ratio *= new_value / baseline_value

        if combined_ratio <= 1.0:
            continue

        tier_baseline = _tier_baseline_value(component)
        if tier_baseline is None:
            continue
        allowed = allowed_multiplier_for(component.ctype, tier_baseline)
        if allowed is not None and combined_ratio > allowed:
            violations.append(
                f"{refdes}: proposed change grows area by {combined_ratio:.2f}x, "
                f"exceeding the {allowed:.1f}x limit for its size tier"
            )

    if violations:
        return False, "; ".join(violations)
    return True, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_area_limits.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/analogcoder/area_limits.py tests/unit/test_area_limits.py
git commit -m "feat: add area_limits module with per-component size-tier growth checks"
```

---

### Task 3: Orchestrator integration

**Files:**
- Modify: `src/analogcoder/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `check_area_growth`, `index_baseline_components` from `analogcoder.area_limits` (Task 2).
- Produces: no new public interface — this is the terminal integration task. No `OrchestratorAgents` field changes, no `cli.py` changes needed (`check_area_growth` is pure Python, not an agent call).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_orchestrator.py`, add two new netlist fixtures right after `SUBCKT_NETLIST` (after line 23, before `FAKE_TOPOLOGY_PROPOSAL`):

```python
AREA_TEST_NETLIST = (
    "* test\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".end\n"
)
AREA_TEST_NETLIST_WITH_SUBCKT = (
    "* test\n"
    ".subckt AMP vinp vinn vout vdd vss\n"
    "M6 vout outA vss vss NMOSG W=40u L=1u\n"
    ".ends AMP\n"
    "Xamp1 vinp vinn vout vdd vss AMP\n"
    ".end\n"
)
```

`AREA_TEST_NETLIST` has **no** `.subckt` block, so `topology_swap_available`
is `False` for it — this deliberately isolates the area-gate's own
exhaustion behavior (Tests 1 and 2 below) from the topology-swap mechanism,
which would otherwise activate once `consecutive_rollbacks` reaches the
threshold and confound the result. `AREA_TEST_NETLIST_WITH_SUBCKT` has
exactly one `.subckt` block and is used only by Test 3, which specifically
verifies the two features compose.

Then append the following test functions to the end of the file:

```python
@pytest.mark.asyncio
async def test_area_check_rejects_without_calling_verify_pre(tmp_path):
    verify_pre_calls = {"count": 0}

    async def counting_verify_pre(analysis, judge_result, proposal, netlist_text):
        verify_pre_calls["count"] += 1
        return {"approved": True, "concerns": [], "feedback": "ok"}

    async def oversized_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        verify_pre=counting_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(AREA_TEST_NETLIST, FAKE_SPEC, state, agents)

    assert verify_pre_calls["count"] == 0
    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "max iterations reached"


@pytest.mark.asyncio
async def test_area_check_mixed_with_verify_pre_rejection_hard_fails(tmp_path):
    call_count = {"n": 0}

    async def mixed_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        call_count["n"] += 1
        if call_count["n"] % 2 == 1:
            new_value = "100u"  # oversized -> area-rejected, 2.5x
        else:
            new_value = "50u"  # right-sized, 1.25x -> passes area, reaches verify_pre
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": new_value, "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    async def always_reject_verify_pre(analysis, judge_result, proposal, netlist_text):
        return {"approved": False, "concerns": ["not justified"], "feedback": "try again"}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=mixed_tune,
        verify_pre=always_reject_verify_pre,
    )
    state = RunState(run_dir=str(tmp_path))

    result = await run_orchestration(AREA_TEST_NETLIST, FAKE_SPEC, state, agents)

    assert result["status"] == "FAIL"
    assert result["failure_reason"] == "tuning proposal repeatedly rejected"


@pytest.mark.asyncio
async def test_area_rejection_eventually_triggers_topology_swap(tmp_path):
    async def oversized_tune(analysis, judge_result, history, rejection_feedback, netlist_text):
        return {
            "proposed_changes": [
                {"refdes": "M6", "param": "W", "old_value": "40u", "new_value": "100u", "reasoning": "x"}
            ],
            "overall_reasoning": "x",
            "confidence": 90,
        }

    propose_topology_calls = {"count": 0}

    async def propose_topology_spy(analysis, judge_result, available_topologies, rejection_feedback):
        propose_topology_calls["count"] += 1
        return {"topology_id": available_topologies[0].id, "reasoning": "x", "confidence": 80}

    agents = make_agents(
        judge=lambda m, s: _async(FAIL_JUDGE),
        tune=oversized_tune,
        propose_topology=propose_topology_spy,
    )
    state = RunState(run_dir=str(tmp_path))

    await run_orchestration(AREA_TEST_NETLIST_WITH_SUBCKT, FAKE_SPEC, state, agents)

    assert propose_topology_calls["count"] >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: FAIL on all 3 new tests. Without the area check wired in, every proposal reaches `verify_pre` regardless of size — so `test_area_check_rejects_without_calling_verify_pre`'s `verify_pre_calls["count"] == 0` assertion fails (it'll be nonzero), and the other two tests' specific `failure_reason` assertions won't match today's undifferentiated exhaustion behavior either. The exact failure output isn't important to match precisely — what matters is that all 3 fail for the right underlying reason (the gate doesn't exist yet) and the 11 pre-existing tests still pass unchanged.

- [ ] **Step 3: Write the implementation**

In `src/analogcoder/orchestrator.py`, change the imports from:

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
from analogcoder.state import RunState
from analogcoder.topologies import TOPOLOGY_LIBRARY
```

to:

```python
from dataclasses import dataclass
from typing import Callable

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.area_limits import check_area_growth, index_baseline_components
from analogcoder.netlist import apply_changes, apply_topology_swap, parse_netlist
from analogcoder.state import RunState
from analogcoder.topologies import TOPOLOGY_LIBRARY
```

Then change this block (computing `topology_swap_available`) from:

```python
        topology_swap_available = len(parse_netlist(initial_netlist_text).subckts) == 1
        tried_topologies: set[str] = set()
        consecutive_rollbacks = 0
```

to:

```python
        topology_swap_available = len(parse_netlist(initial_netlist_text).subckts) == 1
        tried_topologies: set[str] = set()
        consecutive_rollbacks = 0
        baseline_components = index_baseline_components(initial_netlist_text)
```

Then change the parameter-tuning retry loop from:

```python
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
```

to:

```python
            approved_proposal = None
            rejection_feedback = None
            verify_pre_rejected_any = False
            for retry in range(1, MAX_TUNING_RETRIES + 1):
                proposal = await agents.tune(analysis, judge_result, tuning_history, rejection_feedback, netlist_text)
                state.log_event("tuning_proposal", {"outer_iter": outer_iter, "retry": retry, **proposal})

                area_ok, area_feedback = check_area_growth(baseline_components, proposal["proposed_changes"])
                state.log_event(
                    "area_check",
                    {"outer_iter": outer_iter, "retry": retry, "approved": area_ok, "feedback": area_feedback},
                )
                if not area_ok:
                    rejection_feedback = area_feedback
                    continue

                review = await agents.verify_pre(analysis, judge_result, proposal, netlist_text)
                state.log_event("verify_pre", {"outer_iter": outer_iter, "retry": retry, **review})

                if review["approved"]:
                    approved_proposal = proposal
                    break
                verify_pre_rejected_any = True
                rejection_feedback = review["feedback"]

            if approved_proposal is None:
                if verify_pre_rejected_any:
                    return _final_result(
                        "FAIL", state, outer_iter, judge_result,
                        failure_reason="tuning proposal repeatedly rejected",
                    )
                consecutive_rollbacks += 1
                continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: PASS (all 14 tests — 11 pre-existing + 3 new)

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass. Confirm the baseline count from your own terminal before Task 1 starts, and confirm it has grown by exactly 15 + 11 + 3 = 29 new tests (Task 1: 15, Task 2: 11, Task 3: 3) plus the unchanged count of skipped integration tests. Do not hardcode an expected absolute number — verify by comparison to your own recorded baseline.

- [ ] **Step 6: Commit**

```bash
git add src/analogcoder/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: gate parameter tuning on a deterministic area-growth check"
```

## Manual Validation (after all tasks are complete and reviewed)

Not a task with automated assertions. Run a real end-to-end validation after
the branch passes review, reusing the run that originally motivated this
feature:

```bash
.venv/bin/analogcoder \
  --netlist benchmarks/two_stage_opamp/netlist.cir \
  --spec benchmarks/two_stage_opamp/spec_topology_required.yaml \
  --run-dir runs/area_aware_validation_1
```

Confirm in `runs/area_aware_validation_1/history.jsonl`:
- If the tuner proposes something like `M6.W: 40u -> 100u` again (a 2.5x
  jump from a 40u baseline, which is in the medium tier at 2.0x allowed),
  look for an `area_check` event with `"approved": false` for that attempt,
  and confirm the run does not immediately end — it should either find a
  smaller fix on a later retry, or (if it keeps proposing oversized changes)
  eventually reach a `topology_proposal` event instead of failing outright.
- Compare against the original real run in `runs/topology_swap_claude_validation/`
  (from the topology-swap-tuning branch's validation) to see whether the
  presence of this gate changes which fix Claude converges on.

If the real run surfaces a bug or a tier boundary that's clearly wrong in
practice, follow the established pattern: root-cause it with
`systematic-debugging`, write a failing test, fix, verify, commit.
