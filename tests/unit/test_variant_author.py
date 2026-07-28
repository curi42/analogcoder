from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.backend import AgentExecutionError
from analogcoder.agents.variant_author import (
    MAX_VARIANT_AUTHOR_RETRIES,
    author_and_verify_variant,
    author_variant,
)
from analogcoder.curation import Candidate, Slot, check_structure
from analogcoder.simulators.base import RawSimResult

# A minimal single-block deck, reused across the retry-loop tests. Its block
# (BLOCK, ports a/b) is what check_structure judges an authored candidate
# against - no device content matters beyond that, same convention as
# test_curation.py's DECK_TWO_PORT.
DECK = """* t
.option scale=1.0u
.subckt BLOCK a b
R1 a b 1k
.ends BLOCK
.end
"""


def _slot() -> Slot:
    """A Slot whose spec carries no criteria - reproduce_characteristics then
    has nothing to check (an empty criteria list trivially passes), so these
    tests can exercise the retry loop's structure-check path without needing
    real measurements."""
    tb = SimpleNamespace(name="tb1", netlist_path="/dev/null", analyses=["op"], control_block=".control\nop\n.endc", criteria=[])
    spec = SimpleNamespace(testbenches=[tb], all_criteria=[], canonical=tb)
    return Slot(spec=spec, spec_dir=Path("."), block_path="BLOCK")


class _TrivialSimBackend:
    """Always reports a successful run with no measurements - fine here since
    _slot()'s criteria list is empty, so reproduce_characteristics has
    nothing to require a value for."""

    def run(self, netlist_path, testbench_config):
        return RawSimResult(status="success", measurements={}, raw_log="", warnings=[])


# --- author_variant (single LLM call) ---------------------------------------


@pytest.mark.asyncio
async def test_the_prompt_carries_the_models_the_deck_actually_provides():
    """Without the deck's actual model set in the prompt, the agent has no
    way to know which devices it may use and will invent one the deck cannot
    provide - exactly the miller_basic/cap_mim shape from the design doc,
    which dies in ngspice with 'unknown subckt'. This pins that the set is
    literally present in the prompt, and that an unrelated model name is not
    - i.e. the prompt reflects the given set, not some hardcoded example."""
    with patch(
        "analogcoder.agents.variant_author.run_agent",
        new=AsyncMock(return_value={"subckt_body": "...", "rationale": "..."}),
    ) as mock_run:
        await author_variant(
            base_body="M1 d g s b NMOS_MODEL_X W=1u L=1u\n",
            technique="add a cascode device",
            ports=["in", "out", "vdd", "vss"],
            available_models={"NMOS_MODEL_X", "PMOS_MODEL_Y"},
            scale=1e-6,
            rejection_feedback=None,
            backend=object(),
        )

    prompt = mock_run.call_args.kwargs["user_prompt"]
    assert "NMOS_MODEL_X" in prompt
    assert "PMOS_MODEL_Y" in prompt
    assert "sky130_fd_pr__cap_mim_m3_1" not in prompt


@pytest.mark.asyncio
async def test_the_prompt_carries_the_base_body_and_scale():
    """The agent inherits sizing from the existing body and must be told the
    deck's .option scale (a bare W/L number is meaningless without it - the
    same fact that made the area gate's size tiers silently inert on every
    PDK deck until scale was read from the deck itself)."""
    with patch(
        "analogcoder.agents.variant_author.run_agent",
        new=AsyncMock(return_value={"subckt_body": "...", "rationale": "..."}),
    ) as mock_run:
        await author_variant(
            base_body="Xt outA ns nulla UNIQUE_BASE_BODY_MARKER\n",
            technique="move the compensation cap's connection point",
            ports=["outA", "ns"],
            available_models=set(),
            scale=2.5e-6,
            rejection_feedback=None,
            backend=object(),
        )

    prompt = mock_run.call_args.kwargs["user_prompt"]
    assert "UNIQUE_BASE_BODY_MARKER" in prompt
    assert repr(2.5e-6) in prompt


# --- author_and_verify_variant (reject-and-retry loop) ----------------------


@pytest.mark.asyncio
async def test_a_structure_rejection_is_fed_back_verbatim_and_retried():
    """First authored body instantiates a model (MODELX) the deck never
    provides - a 'models' rejection from check_structure/compatible_swaps.
    The loop must feed that exact rejection (reason AND detail, not a
    paraphrase) back as the next call's rejection_feedback, and must
    actually make a second attempt rather than giving up after one
    rejection. The expected reason is computed via the real check_structure
    (same convention as test_curation.py's verbatim-rejection test) so this
    pins "carried over exactly", not a hardcoded string."""
    netlist_texts = {"tb1": DECK}
    slot = _slot()

    bad_candidate = Candidate(
        topology_id="cand_variant",
        subckt_body="X1 a b MODELX\n",
        ports=["a", "b"],
        assumes_scale=1e-6,
        provenance="authored",
    )
    expected = check_structure(bad_candidate, slot, netlist_texts)
    assert expected.status == "fail"

    responses = [
        {"subckt_body": "X1 a b MODELX\n", "rationale": "first try"},
        {"subckt_body": "R2 a b 2k\n", "rationale": "fixed, no foreign model"},
    ]
    with patch(
        "analogcoder.agents.variant_author.run_agent",
        new=AsyncMock(side_effect=responses),
    ) as mock_run:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_TrivialSimBackend(),
            backend=object(),
        )

    assert mock_run.call_count == 2
    second_prompt = mock_run.call_args_list[1].kwargs["user_prompt"]
    assert expected.detail["reason"] in second_prompt
    assert expected.detail["detail"] in second_prompt
    assert result["verdict"] == "PASS"
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_the_retry_limit_is_honoured():
    """Every attempt is rejected at the structure stage (same MODELX shape as
    above, every time). The loop must stop calling the backend at exactly
    MAX_VARIANT_AUTHOR_RETRIES attempts - not fewer (giving up early would
    silently shrink the retry budget) and not more (an unbounded loop)."""
    netlist_texts = {"tb1": DECK}
    slot = _slot()

    with patch(
        "analogcoder.agents.variant_author.run_agent",
        new=AsyncMock(return_value={"subckt_body": "X1 a b MODELX\n", "rationale": "always bad"}),
    ) as mock_run:
        await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_TrivialSimBackend(),
            backend=object(),
        )

    assert mock_run.call_count == MAX_VARIANT_AUTHOR_RETRIES == 3


@pytest.mark.asyncio
async def test_exhausting_retries_is_a_rejection_not_inconclusive():
    """Exhausting the retry budget without ever producing a body that passes
    is a measured fact about the circuit (it tried and failed), not "never
    tried" - so the verdict must be REJECT, never INCONCLUSIVE, and `reason`
    must carry the LAST rejection (not None, not silently dropped)."""
    netlist_texts = {"tb1": DECK}
    slot = _slot()

    with patch(
        "analogcoder.agents.variant_author.run_agent",
        new=AsyncMock(return_value={"subckt_body": "X1 a b MODELX\n", "rationale": "always bad"}),
    ):
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_TrivialSimBackend(),
            backend=object(),
        )

    assert result["verdict"] == "REJECT"
    assert result["verdict"] != "INCONCLUSIVE"
    assert result["candidate"] is None
    assert result["reason"] is not None
    assert "models" in result["reason"]


@pytest.mark.asyncio
async def test_an_agent_execution_error_is_inconclusive():
    """The backend dying (or failing schema validation) says nothing about
    whether the authored circuit is good - it never got a body to judge.
    This must be INCONCLUSIVE, never REJECT, and must not be silently
    retried (a dead backend will typically fail again, so retrying spends
    budget without learning anything new)."""
    netlist_texts = {"tb1": DECK}
    slot = _slot()

    with patch(
        "analogcoder.agents.variant_author.run_agent",
        new=AsyncMock(side_effect=AgentExecutionError("rate limited")),
    ) as mock_run:
        result = await author_and_verify_variant(
            base_body="R1 a b 1k\n",
            technique="add series resistor",
            ports=["a", "b"],
            available_models=set(),
            scale=1e-6,
            topology_id="cand_variant",
            slot=slot,
            netlist_texts=netlist_texts,
            sim_backend=_TrivialSimBackend(),
            backend=object(),
        )

    assert result["verdict"] == "INCONCLUSIVE"
    assert result["verdict"] != "REJECT"
    assert result["candidate"] is None
    assert mock_run.call_count == 1
