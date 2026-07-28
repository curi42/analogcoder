from unittest.mock import AsyncMock, patch

import jsonschema
import pytest

from analogcoder.agents.variant_author import VARIANT_AUTHOR_SYSTEM_PROMPT, author_variant
from analogcoder.schemas import VARIANT_AUTHOR_SCHEMA

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


def test_rationale_is_optional_so_omitting_it_does_not_end_the_run():
    """I5. `rationale` was `required`, read by nothing (`grep rationale src/`
    found only the dataclass field - never `_finalize`, `curation.json`,
    `curation_report.md` or `topology_candidate.py`), and its absence raised
    a schema-validation AgentExecutionError, which `author_and_verify_variant`
    treats as terminal INCONCLUSIVE. Measured: a model returning a valid
    `subckt_body` without `rationale` ended the run on attempt 1 with the
    retry budget of 3 untouched. CLAUDE.md's weak-model section calls
    malformed structured output the EXPECTED local-model failure.

    `subckt_body` stays required - it is the entire output.

    Mutation this catches: putting "rationale" back into `required`
    (observed: `jsonschema.validate` raises
    `ValidationError: 'rationale' is a required property` and the first
    assertion below fails)."""
    assert VARIANT_AUTHOR_SCHEMA["required"] == ["subckt_body"]
    jsonschema.validate({"subckt_body": "R1 a b 1k\n"}, VARIANT_AUTHOR_SCHEMA)
    jsonschema.validate({"subckt_body": "R1 a b 1k\n", "rationale": "why"}, VARIANT_AUTHOR_SCHEMA)

    # ... and the body itself is still non-negotiable.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"rationale": "why"}, VARIANT_AUTHOR_SCHEMA)


def test_the_prompt_does_not_claim_the_incumbent_body_passed_a_corner_sweep():
    """M4. The prompt told the model "the block you are given already works
    and passed a full corner sweep in this PDK". The pipeline has no way to
    know that: `verify_corners`' skip message and the report's disclaimer
    both go out of their way to refuse exactly this claim (corner
    verification is a property of body x slot, and this pipeline never
    checked the source deck's history).

    Mutation this catches: restoring "passed a full corner sweep" to
    VARIANT_AUTHOR_SYSTEM_PROMPT (observed: the first assertion fails)."""
    assert "passed a full corner sweep" not in VARIANT_AUTHOR_SYSTEM_PROMPT
    # It may still say what IS known - the body is the slot's sized incumbent.
    assert "already sized in this PDK" in VARIANT_AUTHOR_SYSTEM_PROMPT
    # ... and it must not silently drop the honesty, so state the unknown.
    assert "does NOT know" in VARIANT_AUTHOR_SYSTEM_PROMPT
