from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.judge import judge_measurements
from analogcoder.spec import Criterion


@pytest.mark.asyncio
async def test_judge_measurements_calls_run_agent_with_measurements_and_criteria():
    fake_result = {
        "overall_pass": True,
        "criteria": [{"name": "gain", "target": ">=19.5", "actual": 20.0, "pass": True, "margin": 0.5}],
        "summary": "all criteria passed",
    }
    criteria = [Criterion(name="gain", measurement="gain_db", operator=">=", threshold=19.5, unit="dB")]

    with patch("analogcoder.agents.judge.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await judge_measurements({"gain_db": 20.0}, criteria)

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["allowed_tools"] == ["mcp__judge__evaluate_criteria"]
    assert "gain_db" in kwargs["user_prompt"]
