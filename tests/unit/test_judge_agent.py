from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.judge import _build_judge_tool, judge_measurements
from analogcoder.spec import Criterion


@pytest.mark.asyncio
async def test_judge_measurements_calls_run_agent_with_measurements_and_criteria():
    fake_result = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
    }
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    fake_backend = object()

    with patch("analogcoder.agents.judge.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await judge_measurements({"gain_db": 20.0}, criteria, backend=fake_backend)

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["tools"][0].name == "evaluate_criteria"
    assert "gain_db" in kwargs["user_prompt"]
    assert kwargs["backend"] is fake_backend


@pytest.mark.asyncio
async def test_judge_tool_handler_calls_evaluate_criteria():
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]
    tool_spec = _build_judge_tool(criteria)

    result = await tool_spec.handler({"measurements": {"gain_db": 20.0}})

    assert result["overall_pass"] is True
    assert result["criteria"][0]["name"] == "gain"
