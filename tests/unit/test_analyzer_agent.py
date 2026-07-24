from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.analyzer import analyze_netlist


@pytest.mark.asyncio
async def test_analyze_netlist_calls_run_agent_with_netlist_text():
    fake_result = {
        "circuit_type": "inverting amplifier",
        "stages": [],
        "component_roles": {},
        "tunable_params": [],
    }
    with patch("analogcoder.agents.analyzer.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await analyze_netlist("Rin in vminus 1k\n.end\n")

    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert "Rin in vminus 1k" in kwargs["user_prompt"]
    assert kwargs["output_schema"]["required"] == ["circuit_type", "stages", "component_roles", "tunable_params"]
