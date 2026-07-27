from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.verifier import verify_post, verify_pre


@pytest.mark.asyncio
async def test_verify_pre_calls_run_agent_with_proposal():
    fake_result = {"approved": True, "concerns": [], "feedback": "reasonable"}
    fake_backend = object()
    with patch("analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await verify_pre(
            structure_view="circuit: inverting amplifier\n\nblocks:\n",
            judge_result={"overall_pass": False},
            proposal={"proposed_changes": [{"refdes": "Rf", "param": "value", "new_value": "11k"}]},
            netlist_text="Rin in vminus 1k\nRf vminus vout 10k\n.end\n",
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["approved", "concerns", "feedback"]
    assert kwargs["backend"] is fake_backend
    assert "Rf vminus vout 10k" in kwargs["user_prompt"]
    assert 'param is not exactly "value"' in kwargs["user_prompt"]
    assert "A refdes is either the exact first token" in kwargs["user_prompt"]


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


@pytest.mark.asyncio
async def test_verify_post_calls_run_agent_with_before_after_judge_results():
    fake_result = {"improved": True, "regressed_criteria": [], "recommendation": "keep", "feedback": "gain fixed"}
    fake_backend = object()
    with patch("analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await verify_post(
            prev_judge_result={"overall_pass": False},
            new_judge_result={"overall_pass": True},
            applied_changes=[{"refdes": "Rf", "param": "value", "new_value": "11k"}],
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["improved", "regressed_criteria", "recommendation", "feedback"]
    assert kwargs["backend"] is fake_backend
