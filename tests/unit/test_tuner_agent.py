from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.tuner import propose_topology_swap, propose_tuning
from analogcoder.topologies import Topology


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


def test_the_tuner_prompt_explains_full_path_addressing():
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT

    assert "OUTER.INNER" in TUNER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_propose_topology_swap_calls_run_agent_with_available_topologies():
    fake_result = {"topology_id": "miller_nulling_resistor", "reasoning": "fixes phase margin", "confidence": 90}
    fake_backend = object()
    topologies = [
        Topology(
            id="miller_nulling_resistor",
            description="adds Rz to cancel the RHP zero",
            subckt_body="Cc outA vnull 2p\nRz vnull vout 500\n",
            addresses=["phase_margin"],
        ),
    ]
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await propose_topology_swap(
            analysis={"circuit_type": "two-stage op-amp"},
            judge_result={"overall_pass": False},
            available_topologies=topologies,
            rejection_feedback=None,
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["topology_id", "reasoning", "confidence"]
    assert kwargs["backend"] is fake_backend
    assert "miller_nulling_resistor" in kwargs["user_prompt"]
    assert "adds Rz to cancel the RHP zero" in kwargs["user_prompt"]


@pytest.mark.asyncio
async def test_propose_topology_swap_includes_rejection_feedback_in_prompt():
    fake_backend = object()
    topologies = [
        Topology(id="miller_basic", description="baseline", subckt_body="", addresses=[]),
    ]
    with patch(
        "analogcoder.agents.tuner.run_agent",
        new=AsyncMock(return_value={"topology_id": "miller_basic", "reasoning": "x", "confidence": 50}),
    ) as mock_run:
        await propose_topology_swap(
            analysis={},
            judge_result={},
            available_topologies=topologies,
            rejection_feedback="'bogus_id' is not an available untried topology.",
            backend=fake_backend,
        )
    _, kwargs = mock_run.call_args
    assert "is not an available untried topology" in kwargs["user_prompt"]
