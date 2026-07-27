from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.tuner import TOPOLOGY_TUNER_SYSTEM_PROMPT, propose_topology_swap, propose_tuning
from analogcoder.schemas import TOPOLOGY_SCHEMA
from analogcoder.topologies import Topology
from analogcoder.topology_match import SwapCandidate


class FakeBackend:
    """Conforms to the positional AgentBackend.run(system_prompt, user_prompt,
    output_schema, tools) signature that agents.agent_runtime.run_agent actually
    calls with - see tests/unit/test_agent_runtime.py's FakeBackend."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def run(self, system_prompt, user_prompt, output_schema, tools=None):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "schema": output_schema})
        return self._result


NINE_PORT = Topology(
    id="nine_port",
    description="folded cascode with a 9-port bias interface",
    subckt_body="",
    addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss", "nbias", "ncas", "pbias", "pcas"],
    assumes_scale=1e-6,
)

FIVE_PORT = Topology(
    id="five_port",
    description="basic miller-compensated op-amp with no bias ports",
    subckt_body="",
    addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss"],
    assumes_scale=1e-6,
)


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
            structure_view="circuit: inverting amplifier\n\nblocks:\n",
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
    assert "circuit: inverting amplifier" in kwargs["user_prompt"]
    assert kwargs["backend"] is fake_backend


def test_the_tuner_prompt_explains_full_path_addressing():
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT

    assert "OUTER.INNER" in TUNER_SYSTEM_PROMPT


def test_the_tuner_prompt_does_not_turn_the_layered_view_back_into_a_filter():
    # 계층화된 상세도를 고른 이유가 "초점 판정이 틀려도 정답 노브가 사라지지
    # 않는다"인데, "tunable에 있는 것만 제안하라"는 문장 하나가 그 설계를
    # 통째로 무효화한다 - tunable 블록은 초점 블록에만 붙기 때문이다.
    # bandgap의 vbg0_min/max가 정확히 그 경우다: 초점은 {BUF_P}인데 정답
    # 노브(XRl1/XRl2)는 접힌 BANDGAP 안에 있다.
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT

    assert "Only propose changes to parameters listed" not in TUNER_SYSTEM_PROMPT
    assert "folded" in TUNER_SYSTEM_PROMPT
    assert "any component in the netlist" in TUNER_SYSTEM_PROMPT


def test_the_tuner_prompt_tells_the_model_not_to_edit_the_testbench_stimulus():
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT

    assert "stimulus (not tunable)" in TUNER_SYSTEM_PROMPT


def test_the_tuner_prompt_spells_the_two_schema_fields_apart():
    # 주소를 "BUF_P.X6.W"로 렌더링하면 점 하나가 스코프 구분자이자 param
    # 구분자가 되어, CLAUDE.md가 실제 실패로 기록한 "M1.W를 refdes 칸에
    # 쓴다"를 뷰 자신이 유도한다.
    from analogcoder.agents.tuner import TUNER_SYSTEM_PROMPT

    assert "refdes=" in TUNER_SYSTEM_PROMPT and "param=" in TUNER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_propose_topology_swap_calls_run_agent_with_candidates():
    fake_result = {
        "topology_id": "miller_nulling_resistor",
        "block_path": "AMP",
        "reasoning": "fixes phase margin",
        "confidence": 90,
    }
    fake_backend = object()
    library = {
        "miller_nulling_resistor": Topology(
            id="miller_nulling_resistor",
            description="adds Rz to cancel the RHP zero",
            subckt_body="Cc outA vnull 2p\nRz vnull vout 500\n",
            addresses=["phase_margin"],
            ports=["vinp", "vinn", "vout", "vdd", "vss"],
            assumes_scale=1e-6,
        ),
    }
    candidates = [SwapCandidate(block_path="AMP", topology_id="miller_nulling_resistor")]
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = await propose_topology_swap(
            structure_view="circuit: two-stage op-amp\n\nblocks:\n",
            judge_result={"overall_pass": False},
            candidates=candidates,
            library=library,
            rejection_feedback=None,
            backend=fake_backend,
        )
    assert result == fake_result
    _, kwargs = mock_run.call_args
    assert kwargs["output_schema"]["required"] == ["topology_id", "reasoning", "confidence"]
    assert kwargs["backend"] is fake_backend
    assert "miller_nulling_resistor" in kwargs["user_prompt"]
    assert "adds Rz to cancel the RHP zero" in kwargs["user_prompt"]
    assert "AMP" in kwargs["user_prompt"]


@pytest.mark.asyncio
async def test_propose_topology_swap_includes_rejection_feedback_in_prompt():
    fake_backend = object()
    library = {
        "miller_basic": Topology(
            id="miller_basic",
            description="baseline",
            subckt_body="",
            addresses=[],
            ports=["vinp", "vinn", "vout", "vdd", "vss"],
            assumes_scale=1e-6,
        ),
    }
    candidates = [SwapCandidate(block_path="AMP", topology_id="miller_basic")]
    with patch(
        "analogcoder.agents.tuner.run_agent",
        new=AsyncMock(return_value={"topology_id": "miller_basic", "reasoning": "x", "confidence": 50}),
    ) as mock_run:
        await propose_topology_swap(
            structure_view="",
            judge_result={},
            candidates=candidates,
            library=library,
            rejection_feedback="'bogus_id' is not an available untried topology.",
            backend=fake_backend,
        )
    _, kwargs = mock_run.call_args
    assert "is not an available untried topology" in kwargs["user_prompt"]


@pytest.mark.asyncio
async def test_the_prompt_lists_block_and_topology_pairs_not_the_whole_library():
    backend = FakeBackend({"topology_id": "nine_port", "block_path": "AMP", "reasoning": "r", "confidence": 80})
    await propose_topology_swap(
        "sv",
        {"criteria": []},
        [SwapCandidate(block_path="AMP", topology_id="nine_port")],
        {"nine_port": NINE_PORT, "five_port": FIVE_PORT},
        None,
        backend,
    )
    prompt = backend.calls[0]["user_prompt"]
    assert "AMP" in prompt and "nine_port" in prompt
    assert "five_port" not in prompt  # 후보가 아닌 항목은 새어 나가면 안 된다


@pytest.mark.asyncio
async def test_the_schema_does_not_require_block_path():
    assert "block_path" in TOPOLOGY_SCHEMA["properties"]
    assert "block_path" not in TOPOLOGY_SCHEMA["required"]


def test_the_system_prompt_does_not_assume_a_single_amplifier():
    assert "the amplifier's internal structure" not in TOPOLOGY_TUNER_SYSTEM_PROMPT
