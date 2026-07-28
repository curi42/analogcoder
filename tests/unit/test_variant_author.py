from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.variant_author import VARIANT_AUTHOR_SYSTEM_PROMPT, author_variant

# `author_and_verify_variant` (the reject-and-retry loop that actually calls
# check_structure/reproduce_characteristics) lives in curation.py, not here -
# CLAUDE.md's convention keeps agents/*.py to system prompt + schema + tool
# declarations, with retry/orchestration living beside the gates it calls
# (orchestrator.py for tuning; optimizer.py vs agents/optimizer.py for the
# ranking split). Its tests are in tests/unit/test_curation.py accordingly.
# This file covers only author_variant - the single LLM call.


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


def test_the_prompt_requires_a_local_modification_that_inherits_sizing():
    """Minor finding from the task-6 review: rule 2 ("the prompt asks for a
    LOCAL modification, not a from-scratch design, and says the existing
    sizing is inherited") had no test pinning the actual wording, so a future
    edit could silently drop it. This checks the static system prompt
    directly - no backend call needed, since the wording lives in the
    module-level constant, not something built per-call."""
    assert "LOCAL MODIFICATION" in VARIANT_AUTHOR_SYSTEM_PROMPT
    assert "NOT designing from scratch" in VARIANT_AUTHOR_SYSTEM_PROMPT
    assert "inherit" in VARIANT_AUTHOR_SYSTEM_PROMPT.lower()
