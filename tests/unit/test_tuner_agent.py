from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.tuner import propose_tuning


@pytest.mark.asyncio
async def test_propose_tuning_includes_history_and_rejection_feedback_in_prompt():
    fake_result = {
        "proposed_changes": [
            {"refdes": "Rf", "param": "value", "old_value": "10k", "new_value": "11k", "reasoning": "increase gain"}
        ],
        "overall_reasoning": "gain was slightly under target",
        "confidence": 0.8,
    }
    fake_backend = object()
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await propose_tuning(
            analysis={"circuit_type": "inverting amplifier"},
            judge_result={"overall_pass": False},
            history=[{"outer_iter": 1, "recommendation": "rollback"}],
            rejection_feedback="last proposal changed a fixed component",
            netlist_text="Rin in vminus 1k\nRf vminus vout 10k\n.end\n",
            backend=fake_backend,
        )

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "rollback" in kwargs["user_prompt"]
    assert "last proposal changed a fixed component" in kwargs["user_prompt"]
    assert "Rf vminus vout 10k" in kwargs["user_prompt"]
    assert kwargs["backend"] is fake_backend
