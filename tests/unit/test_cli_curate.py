import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from analogcoder.agents.backend import AgentBackend
from analogcoder.cli_curate import (
    _parse_knob_names,
    _validate_source_flags,
    build_arg_parser,
    estimate_curation_cost,
    run_curation,
    write_curation_artifacts,
)
from analogcoder.curation import StageResult
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


class _BodyAwareSimBackend:
    """Reports one value for any deck still carrying the slot's own BLOCK body
    (`R1 a b 1k`) and another for a deck whose body has been swapped out - so
    a test can make the candidate genuinely better than the incumbent.

    Needed because stage 3 now enters the incumbent's own measured point
    (`scoped_comparison`'s `incumbent_measurements`) as a dominance candidate:
    against a CONSTANT backend the incumbent ties the candidate on every
    criterion and therefore dominates it, so a constant backend can no longer
    produce an ADMIT at all. That is the fix working rather than a fixture
    accident - a candidate measuring exactly like the incumbent reaches
    nowhere that doing nothing does not already reach."""

    def __init__(self, incumbent: dict, candidate: dict):
        self._incumbent = dict(incumbent)
        self._candidate = dict(candidate)
        self.call_count = 0

    def run(self, netlist_path, testbench_config):
        with open(netlist_path) as f:
            text = f.read()
        self.call_count += 1
        measurements = self._incumbent if "R1 a b 1k" in text else self._candidate
        return RawSimResult(status="success", measurements=dict(measurements), raw_log="", warnings=[])


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

    sim_backend = _BodyAwareSimBackend(incumbent={"r1v": 500.0}, candidate={"r1v": 900.0})
    agent_backend = _FakeAgentBackend()

    result = await run_curation(args, sim_backend=sim_backend, agent_backend=agent_backend)
    assert result["verdict"] == "ADMIT", result["reason"]

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


@pytest.mark.asyncio
async def test_a_stage_3_inconclusive_never_becomes_an_admit(tmp_path):
    """I6's rule at the pipeline level: "I could not judge this" and "nothing
    dominated the candidate" are different facts, so a stage 3 that comes back
    `inconclusive` must end the run as INCONCLUSIVE - not fall through to
    stage 5 and ADMIT the candidate on a comparison that never happened.

    Driven by monkeypatching scoped_comparison to return the inconclusive
    StageResult its own unjudgeable-operator path produces, because every
    operator the shipped spec language allows is judgeable today (that is the
    point of the '==' fix) - the branch has to be reachable in the CLI even
    when curation.py currently only produces it for an operator no spec
    declares yet.

    Mutation this catches: deleting _curate's `status == "inconclusive"`
    branch after stage 3 - the run then ADMITs."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(tmp_path, spec_path, out_dir, from_deck=str(deck_path), from_block="BLOCK", max_knobs=0)

    inconclusive = StageResult(
        name="comparison",
        status="inconclusive",
        detail={"why": "cannot judge operator '!='", "unjudgeable_operators": [{"criterion": "x", "operator": "!="}]},
    )
    with patch("analogcoder.cli_curate.scoped_comparison", return_value=inconclusive):
        result = await run_curation(
            args,
            sim_backend=_BodyAwareSimBackend(incumbent={"r1v": 500.0}, candidate={"r1v": 900.0}),
            agent_backend=_FakeAgentBackend(),
        )

    assert result["verdict"] == "INCONCLUSIVE"
    assert result["verdict"] != "ADMIT"
    assert "cannot judge operator" in result["reason"]


@pytest.mark.asyncio
async def test_a_candidate_worse_than_the_incumbent_is_rejected_by_the_incumbent_itself(tmp_path):
    """C1 + I7 end to end, on the exact shape the reviewer measured: an
    authored candidate WORSE than the incumbent on every criterion. Stage 2
    therefore measures no improvement (`addresses: []`), stage 2.5's
    requirement 2 iterates that empty list and compares zero criteria, and the
    old stage 3 - with the incumbent's own point absent from the dominance
    scan and `max_knobs=0` leaving no swept point either - had literally
    nothing to reject with. Measured verdict then: ADMIT, verified_at=corners.

    Now the incumbent's own point is the dominance candidate, so this must
    REJECT, naming the incumbent (not a knob). `max_knobs=0` is load-bearing:
    it removes every swept point, so ONLY the incumbent point can produce
    this rejection.

    The corners stage must still record that requirement 2 compared nothing,
    and that fact must reach curation_report.md - it is the difference
    between "verified at corners" and "requirement 1 held at corners".

    Mutation this catches: dropping the incumbent from the dominance scan
    (verdict returns to ADMIT), or dropping addresses_compared /
    _verified_at_caveat (the report stops disclosing what was not compared)."""
    spec_path = _write_slot(tmp_path, SPEC_WITH_ONE_CORNER)
    out_dir = tmp_path / "out"
    args = _args(tmp_path, spec_path, out_dir, technique="add a nulling resistor", max_knobs=0)

    # The incumbent measures 500, the swapped-in authored body measures 200 -
    # worse on the slot's only criterion (r1_value, '>=').
    sim_backend = _BodyAwareSimBackend(incumbent={"r1v": 500.0}, candidate={"r1v": 200.0})

    result = await run_curation(args, sim_backend=sim_backend, agent_backend=_FakeAgentBackend())

    assert result["addresses"] == []
    corners = next(s for s in result["stages"] if s.name == "corners")
    assert corners.status == "pass"
    assert corners.detail["addresses_compared"] == 0
    assert "NOTHING" in corners.detail["requirement_2_note"]

    assert result["verdict"] == "REJECT", result["reason"]
    comparison = next(s for s in result["stages"] if s.name == "comparison")
    assert comparison.detail["knobs_swept"] == []  # no swept point could have done it
    assert comparison.detail["dominating_point"]["point"] == "incumbent"

    write_curation_artifacts(str(out_dir), result)
    report = (out_dir / "curation_report.md").read_text()
    assert "Requirement 2 (worst-corner comparison):" in report
    assert "compared **zero** criteria" in report


# --- test_the_candidate_snippet_carries_the_provenance_actually_verified ----


@pytest.mark.asyncio
async def test_the_candidate_snippet_carries_the_provenance_actually_verified(tmp_path):
    """`verified_at` must reflect what THIS run actually verified, not a
    hardcoded constant: an extracted candidate (stage 2.5 skipped, since
    corner verification is authored-only) must snippet as "nominal", while
    an authored candidate whose corner stage genuinely passed must snippet
    as "corners". Catches a mutation that hardcodes verified_at to one
    constant regardless of which stages actually ran/passed."""
    sim_backend = _BodyAwareSimBackend(incumbent={"r1v": 500.0}, candidate={"r1v": 900.0})

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

    result = await run_curation(
        args,
        sim_backend=_BodyAwareSimBackend(incumbent={"r1v": 500.0}, candidate={"r1v": 900.0}),
        agent_backend=_FakeAgentBackend(),
    )
    assert result["verdict"] == "ADMIT", result["reason"]
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

    result = await run_curation(
        args,
        sim_backend=_BodyAwareSimBackend(incumbent={"r1v": 500.0}, candidate={"r1v": 900.0}),
        agent_backend=_FakeAgentBackend(),
    )
    assert result["verdict"] == "ADMIT", result["reason"]
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


def test_estimate_curation_cost_narrows_swept_count_by_knob_names_but_keeps_the_total():
    """Minor follow-up from Task 8 review: knob_names must shrink
    swept_knob_count (and therefore stage3_simulations) to the intersection
    with the block's real knob index - otherwise a narrowed real run's
    startup log overestimates by the full knob-count ratio. knob_count
    itself (the block's TOTAL knob count) must stay unchanged - it answers
    "how many knobs does this block have", a different question from
    "how many will actually be swept", and overwriting it would hide the
    very fact that a narrowing happened. Catches a mutation that ignores
    knob_names entirely (swept_knob_count stays at the full count) as well
    as one that overwrites knob_count with the narrowed value too."""

    class _FakeCanonical:
        name = "tb1"

    class _FakeSpec:
        circuit_name = "test"
        canonical = _FakeCanonical()
        testbenches = [None]

    full = estimate_curation_cost(_FakeSpec(), {"tb1": SLOT_DECK_TWO_KNOBS}, "BLOCK", max_knobs=None, points=3)
    assert full["knob_count"] == 2
    assert full["swept_knob_count"] == 2

    narrowed = estimate_curation_cost(
        _FakeSpec(),
        {"tb1": SLOT_DECK_TWO_KNOBS},
        "BLOCK",
        max_knobs=None,
        points=3,
        knob_names=[("BLOCK.R1", "value")],
    )
    assert narrowed["knob_count"] == 2  # unchanged - the block still HAS 2 knobs
    assert narrowed["swept_knob_count"] == 1  # only 1 is actually going to be swept
    assert narrowed["stage3_simulations"] == 1 * 3 * 1  # swept_knob_count * points * testbench_count


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


# --- --knobs CLI surface (post-review addition - Task 8 follow-up) ---------
#
# Review finding: the underlying curation.scoped_comparison(knob_names=...)
# API was mutation-tested (tests/unit/test_curation.py), but the CLI wiring
# around it (--knobs -> _parse_knob_names -> _curate -> scoped_comparison ->
# _comparison_scope_text -> curation_report.md) had zero coverage. Gutting
# _parse_knob_names to always return [] and gutting _comparison_scope_text's
# prefix logic both passed all 15 pre-existing tests unchanged - exactly the
# "untested wiring silently doing nothing" failure shape this repo keeps
# hitting. The tests below close that gap: parsing in isolation, the error
# path, and end-to-end wiring through to the report a human actually reads.


def test_parse_knob_names_splits_a_scope_qualified_refdes_on_the_last_dot():
    """The realistic case (this is exactly what Task 8's own real-ngspice
    test passes as a Python value, and what --knobs TRIMAMP.XRz.l would
    parse from the command line): a refdes that is ITSELF a dotted scope
    path (TRIMAMP.XRz), followed by the param (l). A mutation that splits
    on the FIRST dot instead of the last would instead produce
    ("TRIMAMP", "XRz.l") - wrong on every scoped refdes, which is the
    normal case for any block nested under a circuit_name, not an edge
    case."""
    assert _parse_knob_names("TRIMAMP.XRz.l") == [("TRIMAMP.XRz", "l")]


def test_parse_knob_names_splits_multiple_comma_separated_entries():
    assert _parse_knob_names("TRIMAMP.XRz.l,BLOCK.R1.value") == [
        ("TRIMAMP.XRz", "l"),
        ("BLOCK.R1", "value"),
    ]


def test_parse_knob_names_returns_none_when_the_flag_is_omitted():
    """None (flag omitted) must stay None, not become an empty list - the
    two mean different things downstream (scoped_comparison's own
    docstring: None = no named narrowing was requested at all; [] = a
    narrowing to zero knobs was explicitly requested). This is exactly the
    mutation the review reported: gutting _parse_knob_names to always
    return [] passed every pre-existing test because none of them checked
    this return value directly."""
    assert _parse_knob_names(None) is None


def test_parse_knob_names_rejects_an_entry_with_no_dot():
    """The error path (brief follow-up, bullet 2): a bare refdes with no
    '.param' suffix (a typo, or a user who forgot the parameter) must raise
    a ValueError naming the bad token - not silently produce a garbage
    tuple or swallow the entry."""
    with pytest.raises(ValueError, match=r"refdes\.param"):
        _parse_knob_names("XRz")


def test_build_arg_parser_recognizes_the_knobs_flag():
    """argparse-level check that --knobs is actually wired into the parser
    (not just into _parse_knob_names, which every test above calls
    directly) and defaults to None when omitted - the real CLI entry
    point's own contract, distinct from the function-level tests above."""
    parser = build_arg_parser()
    common = [
        "--slot-spec", "spec.yaml",
        "--slot-block", "BLOCK",
        "--id", "cand",
        "--out-dir", "out",
        "--from-deck", "deck.cir",
        "--from-block", "BLOCK",
    ]

    args = parser.parse_args(common + ["--knobs", "TRIMAMP.XRz.l"])
    assert args.knobs == "TRIMAMP.XRz.l"

    args_default = parser.parse_args(common)
    assert args_default.knobs is None


SLOT_DECK_TWO_KNOBS = """* slot
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
R2 a b 2k
.ends BLOCK
.end
"""


def _write_slot_two_knobs(tmp_path: Path, spec_text: str) -> Path:
    (tmp_path / "slot.cir").write_text(SLOT_DECK_TWO_KNOBS)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_text)
    return spec_path


@pytest.mark.asyncio
async def test_the_knobs_flag_narrows_stage_3_to_the_named_knob_and_the_report_records_it(tmp_path):
    """End-to-end proof that --knobs actually reaches
    curation.scoped_comparison's knob_names parameter, not just that
    _parse_knob_names parses correctly in isolation (the two tests above).
    BLOCK has two knobs here (R1, R2); --knobs "BLOCK.R1.value" must leave
    R2 unswept (named in knobs_omitted, not silently dropped) and the
    narrowing itself must be legible in curation_report.md's
    comparison-scope line - the report a human actually reads, not just an
    internal StageResult.detail. This is precisely the wiring the review
    found untested: gutting _parse_knob_names to return [] regardless of
    input, or gutting _comparison_scope_text's prefix logic, both passed
    every pre-existing test unchanged."""
    spec_path = _write_slot_two_knobs(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(
        tmp_path,
        spec_path,
        out_dir,
        from_deck=str(deck_path),
        from_block="BLOCK",
        knobs="BLOCK.R1.value",
    )

    result = await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0}), agent_backend=_FakeAgentBackend())

    comparison_stages = [s for s in result["stages"] if s.name == "comparison"]
    assert len(comparison_stages) == 1
    detail = comparison_stages[0].detail
    assert detail["knob_names_requested"] == ["BLOCK.R1.value"]
    assert [k["knob"] for k in detail["knobs_swept"]] == ["BLOCK.R1.value"]
    assert "BLOCK.R2.value" in detail["knobs_omitted"]

    write_curation_artifacts(str(out_dir), result)
    report = (out_dir / "curation_report.md").read_text()
    assert "scope narrowed to explicitly requested knob(s)" in report
    assert "BLOCK.R1.value" in report


@pytest.mark.asyncio
async def test_a_malformed_knobs_value_ends_as_inconclusive_not_a_crash(tmp_path):
    """A --knobs entry missing the 'refdes.param' dot must not crash the
    pipeline. _parse_knob_names raises ValueError for it; run_curation's
    own guard (the same try/except that already catches apply_changes'
    ambiguous-refdes ValueError, per CLAUDE.md) must turn that into a
    well-formed INCONCLUSIVE result carrying the parse error, with all
    three artifacts still written - not an uncaught traceback."""
    spec_path = _write_slot(tmp_path, SPEC_NO_CORNERS)
    out_dir = tmp_path / "out"
    deck_path = tmp_path / "source_deck.cir"
    deck_path.write_text(SOURCE_DECK)
    args = _args(
        tmp_path,
        spec_path,
        out_dir,
        from_deck=str(deck_path),
        from_block="BLOCK",
        knobs="not_a_valid_entry",
    )

    result = await run_curation(args, sim_backend=_ConstantSimBackend({"r1v": 500.0}), agent_backend=_FakeAgentBackend())

    assert result["verdict"] == "INCONCLUSIVE"
    assert "not_a_valid_entry" in result["reason"]

    write_curation_artifacts(str(out_dir), result)
    for name in ("curation_report.md", "topology_candidate.py", "curation.json"):
        path = out_dir / name
        assert path.exists()
        assert path.stat().st_size > 0
