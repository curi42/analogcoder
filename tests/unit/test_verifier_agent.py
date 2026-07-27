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
async def test_verify_pre_prompt_permits_the_param_the_peer_rule_exists_to_admit():
    # check_param_applicability deliberately admits a param that the component's
    # own line does not write but a same-model peer does - that is the only
    # thing keeping bandgap's Xq1.m (emitter-area ratio) reachable, since Xq1
    # writes no m= while Xq8 writes m=8. The prompt told verify_pre to reject
    # exactly that, and three such rejections set verify_pre_rejected_any and
    # hard-FAIL the run without ever reaching topology escalation.
    with patch(
        "analogcoder.agents.verifier.run_agent", new=AsyncMock(return_value={})
    ) as mock_run:
        await verify_pre({}, {}, {}, "* netlist\n", object())

    prompt = mock_run.call_args.kwargs["user_prompt"]
    assert "other instances of the same model" in prompt
    assert "a deterministic gate has already checked applicability" in prompt


def test_the_verifier_system_prompt_does_not_cite_an_analysis_that_no_longer_exists():
    # E2가 analyzer 에이전트를 없앴다. "circuit analysis"를 근거로 판정하라는
    # 지시는 이제 존재하지 않는 산출물을 가리킨다.
    from analogcoder.agents.verifier import VERIFIER_SYSTEM_PROMPT

    assert "circuit analysis" not in VERIFIER_SYSTEM_PROMPT
    assert "structure" in VERIFIER_SYSTEM_PROMPT


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
