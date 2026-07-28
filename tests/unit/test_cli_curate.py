import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from analogcoder.agents.backend import AgentBackend
from analogcoder.cli_curate import (
    _validate_source_flags,
    estimate_curation_cost,
    run_curation,
    write_curation_artifacts,
)
from analogcoder.simulators.base import RawSimResult
from analogcoder.spec import load_spec

TOPOLOGIES_PATH = Path(__file__).resolve().parents[2] / "src" / "analogcoder" / "topologies.py"

# --- shared fixtures --------------------------------------------------------

SLOT_DECK = """* slot
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
.ends BLOCK
.end
"""

# A source deck to extract from (source A): same ports, but a DIFFERENT
# component sequence (refdes AND value both differ from the slot's R1) so
# compatible_swaps' identical_body no-op check does not fire - otherwise
# extracting BLOCK from a byte-identical deck would reject as a no-op swap
# regardless of anything this test is trying to isolate.
SOURCE_DECK = """* source
.option scale=1.0u
.subckt BLOCK a b
R9 a b 2k
.ends BLOCK
.end
"""

SPEC_NO_CORNERS = """circuit_name: test
testbenches:
  - name: tb1
    netlist: slot.cir
    analyses: ["op"]
    control_block: |
      .control
      op
      .endc
    criteria:
      - name: r1_value
        measurement: r1v
        operator: ">="
        threshold: 100.0
"""

SPEC_WITH_ONE_CORNER = """circuit_name: test
pvt_corners:
  process: ["tt"]
  voltage: [1.8]
  temperature: [27]
testbenches:
  - name: tb1
    netlist: slot.cir
    analyses: ["op"]
    control_block: |
      .control
      op
      .endc
    criteria:
      - name: r1_value
        measurement: r1v
        operator: ">="
        threshold: 100.0
"""


def _write_slot(tmp_path: Path, spec_text: str) -> Path:
    (tmp_path / "slot.cir").write_text(SLOT_DECK)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_text)
    return spec_path


class _ConstantSimBackend:
    """Every simulated point (candidate, baseline, any corner, any sweep
    point) reports the same fixed measurements regardless of the deck it is
    handed - a fake SimulatorBackend, per the task's instruction to fake the
    simulator rather than mock curation.py's own stage functions."""

    def __init__(self, measurements: dict):
        self._measurements = dict(measurements)
        self.call_count = 0

    def run(self, netlist_path, testbench_config):
        self.call_count += 1
        return RawSimResult(status="success", measurements=dict(self._measurements), raw_log="", warnings=[])


class _FakeAgentBackend(AgentBackend):
    """A real AgentBackend implementation (not a mock of curation.py's own
    functions) so author_and_verify_variant/render_description run for real
    against it - per the task's instruction to mock the agent backend, not
    the gates. Dispatches on the schema's own properties since this one
    fake stands in for both LLM calls this pipeline makes (variant
    authoring and description rendering)."""

    def __init__(self, subckt_body: str = "R2 a b 2k\n", rationale: str = "test rationale", description: str = "A test description."):
        self.subckt_body = subckt_body
        self.rationale = rationale
        self.description = description

    async def run(self, system_prompt, user_prompt, output_schema, tools):
        props = output_schema.get("properties", {})
        if "subckt_body" in props:
            return {"subckt_body": self.subckt_body, "rationale": self.rationale}
        if "description" in props:
            return {"description": self.description}
        raise AssertionError(f"unexpected schema handed to the fake agent backend: {output_schema}")


def _args(tmp_path: Path, spec_path: Path, out_dir: Path, **overrides) -> argparse.Namespace:
    base = dict(
        from_deck=None,
        from_block=None,
        from_body=None,
        ports=None,
        assumes_scale=None,
        technique=None,
        slot_spec=str(spec_path),
        slot_block="BLOCK",
        topology_id="cand_test",
        out_dir=str(out_dir),
        max_knobs=8,
        points=3,
        simulator="ngspice",
        agent_backend="claude",
        llm_base_url=None,
        llm_model=None,
        claude_model="claude-test",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# --- test_two_source_flags_together_is_an_error ----------------------------


def test_two_source_flags_together_is_an_error():
    """Giving both --from-deck and --technique must be rejected before any
    candidate is ever built. Catches a mutation that accepts more than one
    source flag and silently picks one (e.g. always preferring --technique),
    which would let a confused invocation silently curate the wrong source."""
    args = argparse.Namespace(from_deck="deck.cir", from_block="BLOCK", from_body=None, ports=None, assumes_scale=None, technique="add a cascode")
    with pytest.raises(ValueError, match=r"exactly one of"):
        _validate_source_flags(args)


def test_no_source_flag_is_also_an_error():
    """The mirror case: zero source flags is just as invalid as two - there
    is no source to build a candidate from. Catches a mutation that treats
    an all-None args object as some default source instead of raising."""
    args = argparse.Namespace(from_deck=None, from_block=None, from_body=None, ports=None, assumes_scale=None, technique=None)
    with pytest.raises(ValueError, match=r"exactly one of"):
        _validate_source_flags(args)


def test_exactly_one_source_flag_is_accepted():
    """Sanity/positive control for the two tests above: a single, complete
    source (source A here) must NOT raise. Without this, a validator that
    always raises would still pass both negative tests."""
    args = argparse.Namespace(from_deck="deck.cir", from_block="BLOCK", from_body=None, ports=None, assumes_scale=None, technique=None)
    assert _validate_source_flags(args) == "deck"


# --- test_the_slot_spec_loads_and_declares_nine_corners ---------------------


def test_the_slot_spec_loads_and_declares_nine_corners():
    """benchmarks/bandgap/spec_curate_slot.yaml must load through the
    project's own spec.load_spec, declare exactly the amp_loops testbench,
    carry the 9-corner grid copied from spec_corner_reduction.yaml, and
    declare neither optimize nor corner_reduction (brief rule 7). Catches a
    mutation that reintroduces one of those blocks or drifts the corner
    count."""
    repo_root = Path(__file__).resolve().parents[2]
    spec_path = repo_root / "benchmarks" / "bandgap" / "spec_curate_slot.yaml"
    spec = load_spec(str(spec_path))

    assert [tb.name for tb in spec.testbenches] == ["amp_loops"]
    assert spec.optimize is None
    assert spec.corner_reduction is None
    assert spec.pvt_corners is not None
    corner_count = len(spec.pvt_corners.process) * len(spec.pvt_corners.voltage) * len(spec.pvt_corners.temperature)
    assert corner_count == 9


# --- test_a_rejection_still_writes_all_three_artifacts ----------------------


@pytest.mark.asyncio
async def test_a_rejection_still_writes_all_three_artifacts(tmp_path):
    """A source-B candidate that declares an extra port ("c") the slot's
    BLOCK does not have must be REJECTed by stage 1 (structure) - and all
    three artifacts must still be written. Catches a mutation that only
    calls write_curation_artifacts on the ADMIT path (brief's own stated
    mutation for this test)."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"

    body_path = tmp_path / "body.sp"
    body_path.write_text("R2 a b 1k\nR3 a c 1k\n")
    args = _args(
        tmp_path,
        spec_path,
        out_dir,
        from_body=str(body_path),
        ports="a b c",
        assumes_scale=1e-6,
    )

    sim_backend = _ConstantSimBackend({"r1v": 500.0})
    agent_backend = _FakeAgentBackend()

    result = await run_curation(args, sim_backend=sim_backend, agent_backend=agent_backend)
    assert result["verdict"] == "REJECT"

    write_curation_artifacts(str(out_dir), result)

    for name in ("curation_report.md", "topology_candidate.py", "curation.json"):
        path = out_dir / name
        assert path.exists(), f"{name} was not written on a REJECT verdict"
        assert path.stat().st_size > 0


@pytest.mark.asyncio
async def test_an_admitted_candidate_also_writes_all_three_artifacts(tmp_path):
    """Positive control for the test above: an ADMIT run must ALSO write all
    three artifacts. Without this, a validator that only checks REJECT would
    not catch a mutation that writes artifacts on every verdict EXCEPT
    ADMIT (the opposite-direction breakage of the same rule)."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"

    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    sim_backend = _ConstantSimBackend({"r1v": 500.0})
    agent_backend = _FakeAgentBackend()

    result = await run_curation(args, sim_backend=sim_backend, agent_backend=agent_backend)
    assert result["verdict"] == "ADMIT"

    write_curation_artifacts(str(out_dir), result)
    for name in ("curation_report.md", "topology_candidate.py", "curation.json"):
        assert (out_dir / name).stat().st_size > 0


# --- test_the_report_records_the_comparison_scope ---------------------------


@pytest.mark.asyncio
async def test_the_report_records_the_comparison_scope(tmp_path):
    """Reaching stage 3 (scoped comparison) must leave the swept knob name,
    the simulation count, and (since every sweep point ties the candidate
    here - the fake backend is constant - a dominating point) legible in
    curation_report.md. Catches a mutation that drops stage 3's detail from
    the report (e.g. writing only the verdict/reason)."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"

    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    # max_knobs left at the default (8) so BLOCK.R1's one real knob is
    # actually swept - a constant backend means every swept point ties the
    # candidate's own (identical) measurement, so stage 3 rejects. That is
    # deliberate here: it also proves the "dominating point" line renders.
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK")

    sim_backend = _ConstantSimBackend({"r1v": 500.0})
    agent_backend = _FakeAgentBackend()

    result = await run_curation(args, sim_backend=sim_backend, agent_backend=agent_backend)
    assert result["verdict"] == "REJECT"
    comparison_stages = [s for s in result["stages"] if s.name == "comparison"]
    assert len(comparison_stages) == 1
    assert comparison_stages[0].status == "fail"

    write_curation_artifacts(str(out_dir), result)
    report = (out_dir / "curation_report.md").read_text()

    assert "BLOCK.R1.value" in report
    assert "Comparison scope:" in report
    assert "Dominating point:" in report
    assert "none - candidate survives" not in report  # a point really did dominate here


# --- test_the_candidate_snippet_carries_the_provenance_actually_verified ----


@pytest.mark.asyncio
async def test_the_candidate_snippet_carries_the_provenance_actually_verified(tmp_path):
    """`verified_at` must reflect what THIS run actually verified, not a
    hardcoded constant: an extracted candidate (stage 2.5 skipped, since
    corner verification is authored-only) must snippet as "nominal", while
    an authored candidate whose corner stage genuinely passed must snippet
    as "corners". Catches a mutation that hardcodes verified_at to one
    constant regardless of which stages actually ran/passed."""
    sim_backend = _ConstantSimBackend({"r1v": 500.0})

    # --- extracted candidate: no pvt_corners in the slot -> corners is
    # unconditionally "skipped" for this provenance -> verified_at="nominal".
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir_a = tmp_path / "out_a"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args_a = _args(tmp_path, spec_path, out_dir_a, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    result_a = await run_curation(args_a, sim_backend=sim_backend, agent_backend=_FakeAgentBackend())
    assert result_a["candidate"].provenance == "extracted"
    assert result_a["verified_at"] == "nominal"
    write_curation_artifacts(str(out_dir_a), result_a)
    snippet_a = (out_dir_a / "topology_candidate.py").read_text()
    assert "provenance='extracted'" in snippet_a
    assert "verified_at='nominal'" in snippet_a

    # --- authored candidate: the slot DOES declare pvt_corners, and the
    # constant backend means every corner/testbench reports the same
    # measurement (no missing values) -> corners genuinely "pass".
    spec_path_corners = _write_slot(tmp_path, SPEC_WITH_ONE_CORNER)
    out_dir_c = tmp_path / "out_c"
    args_c = _args(
        tmp_path,
        spec_path_corners,
        out_dir_c,
        technique="add a nulling resistor",
        max_knobs=0,
    )

    result_c = await run_curation(args_c, sim_backend=sim_backend, agent_backend=_FakeAgentBackend(subckt_body="R2 a b 2k\n"))
    assert result_c["verdict"] == "ADMIT", result_c["reason"]
    assert result_c["candidate"].provenance == "authored"
    assert result_c["verified_at"] == "corners"
    write_curation_artifacts(str(out_dir_c), result_c)
    snippet_c = (out_dir_c / "topology_candidate.py").read_text()
    assert "provenance='authored'" in snippet_c
    assert "verified_at='corners'" in snippet_c


@pytest.mark.asyncio
async def test_the_report_does_not_assert_the_source_was_corner_verified(tmp_path):
    """For an extracted candidate, curation_report.md prints `Verified at:
    nominal` and then, a few lines later, the corners stage's own inherited
    `why` text - that text must NOT assert as fact that the source deck
    passed a corner sweep, which would flatly contradict `nominal` two
    paragraphs above it. Corner-verification is a property of (body x slot),
    not of the SPICE text alone: this pipeline cannot know whether an
    arbitrary --from-deck target was ever corner-swept, or whether that
    verification would transfer to this slot, so the report must not claim
    it either way. Catches a regression to the old wording ("an extracted
    body already comes from a deck that passed a full corner sweep")."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    result = await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0}), agent_backend=_FakeAgentBackend())
    assert result["verdict"] == "ADMIT"
    assert result["verified_at"] == "nominal"

    write_curation_artifacts(str(out_dir), result)
    report = (out_dir / "curation_report.md").read_text()

    assert "**Verified at:** nominal" in report
    # the resolving sentence next to "Verified at" must itself be present
    assert "does not imply that history transfers" in report
    # and the corners stage's own skip text must not contradict it
    assert "passed a full corner sweep" not in report
    assert "already comes from a deck" not in report


# --- test_the_library_module_is_not_modified --------------------------------


@pytest.mark.asyncio
async def test_the_library_module_is_not_modified(tmp_path):
    """An ADMIT run must never touch topologies.py - the library only grows
    when a human commits a reviewed snippet (design doc "why a human
    commits"). Catches a mutation that auto-appends the admitted candidate
    into TOPOLOGY_LIBRARY or writes topology_candidate.py's content back
    into topologies.py."""
    before = TOPOLOGIES_PATH.read_bytes()

    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    result = await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0}), agent_backend=_FakeAgentBackend())
    assert result["verdict"] == "ADMIT"
    write_curation_artifacts(str(out_dir), result)

    after = TOPOLOGIES_PATH.read_bytes()
    assert before == after


# --- test_an_unexpected_exception_still_produces_a_report_and_a_verdict -----


class _RaisingSimBackend:
    """A fake SimulatorBackend whose .run() always raises - stands in for a
    real simulator crashing/timing out, distinct from a real deck genuinely
    missing a measurement."""

    def run(self, netlist_path, testbench_config):
        raise RuntimeError("simulator crashed")


@pytest.mark.asyncio
async def test_reject_and_inconclusive_do_not_collapse_at_the_same_stage(tmp_path):
    """Two different reasons stage 2 (reproduce) can fail to admit a
    candidate must produce two different verdicts: a genuinely missing
    measurement (the circuit was simulated fine, but doesn't produce the
    criterion's measurement) is REJECT, while the simulator itself raising
    is INCONCLUSIVE (we never got to measure anything). Catches a mutation
    that merges reproduce_characteristics' "fail" and "inconclusive"
    statuses into a single verdict branch in _curate, which would make a
    dead simulator look exactly like a bad circuit."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)

    # REJECT: the backend reports measurements, but never the criterion's
    # name ("r1v" is missing) - a genuine, measured failure.
    out_dir_reject = tmp_path / "out_reject"
    args_reject = _args(tmp_path, spec_path, out_dir_reject, from_deck=str(deck_path), from_block="BLOCK")
    result_reject = await run_curation(
        args_reject, sim_backend=_ConstantSimBackend({"unrelated_measurement": 1.0}), agent_backend=_FakeAgentBackend()
    )
    assert result_reject["verdict"] == "REJECT"

    # INCONCLUSIVE: the backend itself raises - nothing was measured at all.
    out_dir_inconclusive = tmp_path / "out_inconclusive"
    args_inconclusive = _args(tmp_path, spec_path, out_dir_inconclusive, from_deck=str(deck_path), from_block="BLOCK")
    result_inconclusive = await run_curation(args_inconclusive, sim_backend=_RaisingSimBackend(), agent_backend=_FakeAgentBackend())
    assert result_inconclusive["verdict"] == "INCONCLUSIVE"

    assert result_reject["verdict"] != result_inconclusive["verdict"]


@pytest.mark.asyncio
async def test_an_unexpected_exception_still_produces_a_report_and_a_verdict(tmp_path):
    """An arbitrary, unanticipated exception raised mid-pipeline (here,
    check_structure itself blowing up - something none of the gates'
    documented inconclusive/fail paths cover) must still end as a
    well-formed INCONCLUSIVE result with all three artifacts written, never
    an uncaught traceback. Mirrors run_optimization's guard. Catches a
    mutation that removes/narrows the try/except in run_curation."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK")

    with patch("analogcoder.cli_curate.check_structure", side_effect=RuntimeError("boom - unexpected")):
        result = await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0}), agent_backend=_FakeAgentBackend())

    assert result["verdict"] == "INCONCLUSIVE"
    assert "boom - unexpected" in result["reason"]

    write_curation_artifacts(str(out_dir), result)
    for name in ("curation_report.md", "topology_candidate.py", "curation.json"):
        path = out_dir / name
        assert path.exists()
        assert path.stat().st_size > 0

    # curation.json must be valid JSON even though the candidate itself was
    # never resolved past the exception point.
    payload = json.loads((out_dir / "curation.json").read_text())
    assert payload["verdict"] == "INCONCLUSIVE"


# --- cost estimate logging (brief rule 5) -----------------------------------


def test_estimate_curation_cost_scales_with_testbench_count():
    """estimate_curation_cost's stage-3 estimate must multiply by the
    number of testbenches in the slot (the same multiplication corner
    reduction already measured as its dominant cost) - not just by knob
    count x points. Catches a mutation that drops the testbench_count
    factor from the stage3_simulations computation."""
    spec_path_1tb = None  # built below via a real spec object

    class _FakeCanonical:
        name = "tb1"

    class _FakeSpec:
        circuit_name = "test"
        canonical = _FakeCanonical()

        def __init__(self, n):
            self.testbenches = [None] * n

    cost_1 = estimate_curation_cost(_FakeSpec(1), {"tb1": SLOT_DECK}, "BLOCK", max_knobs=None, points=3)
    cost_2 = estimate_curation_cost(_FakeSpec(2), {"tb1": SLOT_DECK}, "BLOCK", max_knobs=None, points=3)

    assert cost_1["knob_count"] == cost_2["knob_count"] == 1  # BLOCK.R1.value
    assert cost_2["stage3_simulations"] == 2 * cost_1["stage3_simulations"]
    assert cost_2["stage2_simulations"] == 2 * cost_1["stage2_simulations"]


@pytest.mark.asyncio
async def test_expected_cost_is_logged_only_for_multi_testbench_slots(tmp_path, caplog):
    """Brief rule 5: a multi-testbench slot must log its expected simulation
    count/time at the start of the run; a single-testbench slot (every
    other test in this file) must not. Catches a mutation that logs
    unconditionally (noise on the common case) or never logs at all
    (silently dropping the rule)."""
    (tmp_path / "slot.cir").write_text(SLOT_DECK)
    spec_text = """circuit_name: test
testbenches:
  - name: tb1
    netlist: slot.cir
    analyses: ["op"]
    control_block: |
      .control
      op
      .endc
    criteria:
      - name: r1_value
        measurement: r1v
        operator: ">="
        threshold: 100.0
  - name: tb2
    netlist: slot.cir
    analyses: ["op"]
    control_block: |
      .control
      op
      .endc
    criteria:
      - name: r1_value_2
        measurement: r1v
        operator: ">="
        threshold: 100.0
"""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_text)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    import logging

    with caplog.at_level(logging.INFO, logger="analogcoder.cli_curate"):
        await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0, "r1v_2": 500.0}), agent_backend=_FakeAgentBackend())

    assert any("multi-testbench slot" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_expected_cost_is_not_logged_for_a_single_testbench_slot(tmp_path, caplog):
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    import logging

    with caplog.at_level(logging.INFO, logger="analogcoder.cli_curate"):
        await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0}), agent_backend=_FakeAgentBackend())

    assert not any("multi-testbench slot" in rec.message for rec in caplog.records)
