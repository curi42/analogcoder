from unittest.mock import AsyncMock, patch

import pytest

from analogcoder.agents.tuner import (
    TOPOLOGY_TUNER_SYSTEM_PROMPT,
    TUNER_SYSTEM_PROMPT,
    propose_topology_swap,
    propose_tuning,
)
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
    provenance="authored",
    verified_at="nominal",
)

FIVE_PORT = Topology(
    id="five_port",
    description="basic miller-compensated op-amp with no bias ports",
    subckt_body="",
    addresses=[],
    ports=["vinp", "vinn", "vout", "vdd", "vss"],
    assumes_scale=1e-6,
    provenance="authored",
    verified_at="nominal",
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
            attempts_view="Past attempts this run:\n  iter 1.1  Rf value  10k -> 11k  rollback",
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
async def test_the_rendered_attempt_log_reaches_the_user_prompt():
    """어느 변형을 잡는가: attempts_view를 인자로 받아 놓고 프롬프트에 안 넣는
    구현. 시그니처만 바뀌고 아무것도 전달되지 않으면 이 브랜치 전체가 무의미해진다."""
    backend = FakeBackend({"proposed_changes": [], "overall_reasoning": "x", "confidence": 50})
    rendered = "Past attempts this run:\n  iter 1.1  TRIMAMP.XRz l  15 -> 45  kept  pm +18.4"

    await propose_tuning("structure", {"overall_pass": False}, rendered, None, "* deck", backend)

    assert rendered in backend.calls[0]["user_prompt"]


def test_the_tuner_prompt_presents_past_attempts_as_facts_not_as_a_restriction():
    """어느 변형을 잡는가: "이미 시도한 노브를 다시 제안하지 마라"류의 문장을
    프롬프트에 넣는 구현. 히스토리를 필터로 바꾸는 것은 초점을 필터로 바꿨다가
    값을 치른 것과 같은 오류이고, 이 저장소에는 과거의 롤백이 지금은 옳은
    실측이 있다(TRIMAMP.XRz.l 15->60은 위상 여유 81°->125°, 120에서 다시 무너짐)."""
    prompt = TUNER_SYSTEM_PROMPT.lower()

    # 긍정 단언은 **문단이 존재한다**까지만 건다. 정확한 문장을 고정하면
    # 측정 문서가 후보로 지목한 절제 실험이 "다른 불변식을 지키려고 쓴
    # 테스트를 고쳐야만" 가능해진다. 실제 불변식은 아래 banned 목록이다.
    assert "past attempts this run" in prompt
    banned = (
        "do not propose",
        "never propose",
        "must not repeat",
        "avoid proposing",
        "should not propose",
        "refrain from",
        "avoid re-propos",
        "discouraged",
        "must not propose",
    )
    for phrase in banned:
        assert phrase not in prompt, f"히스토리가 제한으로 서술되었다: {phrase!r}"


_MODEL_NAME_POSITIONAL_DECK = """* peer rule deck
.subckt CORE vp vn
Xq1 vp 0 0 pnp_05v5
Xq8 vn 0 0 pnp_05v5 m=8
.ends
Xcore a b CORE
.end
"""


def test_the_tuner_prompt_does_not_point_param_value_at_a_model_name():
    """어느 변형을 잡는가: "위치 토큰이면 param=value"라고만 적은 프롬프트.

    check_param_applicability는 위치 토큰이 **숫자가 아닐 때** param="value"를
    거부한다 - 그 토큰은 모델명/서브서킷명이고, 덮어쓰면 소자의 정체가 바뀐다.
    프롬프트가 그 단서 없이 "plain positional token"이라고만 말하면 게이트가
    금지하는 주소를 생성자에게 가리키는 것이고(프롬프트가 게이트보다 **느슨**),
    그 제안은 게이트에 막혀 재시도 하나를 태운다. 아래 두 단언 중 앞은 게이트의
    실제 규칙이고 뒤는 프롬프트가 그것을 거울처럼 반복하는지다."""
    from analogcoder.netlist import check_param_applicability

    ok_value, _ = check_param_applicability(
        _MODEL_NAME_POSITIONAL_DECK,
        [{"refdes": "CORE.Xq1", "param": "value", "old_value": "pnp_05v5", "new_value": "2"}],
    )
    assert not ok_value, "게이트가 모델명 위치 토큰에 param=value를 허용하면 이 테스트의 전제가 무너진다"
    ok_m, _ = check_param_applicability(
        _MODEL_NAME_POSITIONAL_DECK,
        [{"refdes": "CORE.Xq1", "param": "m", "old_value": "1", "new_value": "8"}],
    )
    assert ok_m, "동료 규칙(Xq8이 m=8을 쓴다)이 Xq1.m을 허용해야 한다"

    # 프롬프트는 줄바꿈으로 접혀 있으므로 공백을 정규화하고 본다 - 그러지
    # 않으면 문장을 다시 감싸는 것만으로 단언이 깨진다.
    prompt = " ".join(TUNER_SYSTEM_PROMPT.lower().split())
    assert "numeric" in prompt, "위치 토큰이 숫자여야 한다는 단서가 프롬프트에 없다"
    assert "model or subckt name" in prompt


def test_the_tuner_prompt_says_a_same_model_peer_can_supply_the_parameter_name():
    """어느 변형을 잡는가: "그 소자 자신의 줄에 name=로 적혀 있을 때만"으로
    좁힌 프롬프트. 게이트의 동료 규칙(위 테스트가 실측으로 고정한다)이 admit
    하는 Xq1.m을 프롬프트가 금지하면, 이 저장소가 verify_pre에서 이미 값을
    치른 "게이트보다 엄격한 거울"이 생성자 쪽에 생긴다."""
    prompt = " ".join(TUNER_SYSTEM_PROMPT.lower().split())

    assert "another instance of the same model" in prompt
    assert "does not have to appear on that component's own line" in prompt


def test_the_tuner_prompt_says_lines_sharing_an_iteration_prefix_were_applied_together():
    """어느 변형을 잡는가: 한 제안의 deltas/regressed를 그 제안의 **모든** 변경에
    복사해 놓고(orchestrator.py의 루프), 렌더러가 변경당 한 줄을 찍는 구현 -
    3-변경 제안이 "pm +18.4"를 세 줄에 똑같이 남긴다. 공동으로 측정된 사실이
    노브별로 귀속된 주장으로 읽히는 모양이고, 이 저장소는 그 모양에 이미
    두 번 값을 치렀다(F2의 agent-declared addresses, 무관용 Pareto).
    유일한 묶음 신호가 "iter N.R" 접두사이므로, 프롬프트가 그것을 말해야 한다."""
    prompt = TUNER_SYSTEM_PROMPT.lower()

    assert "iter n.r" in prompt
    assert "together" in prompt
    assert "not of the individual line" in prompt


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
            provenance="authored",
            verified_at="nominal",
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
            provenance="authored",
            verified_at="nominal",
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


@pytest.mark.asyncio
async def test_the_swap_prompt_renders_how_each_candidate_entry_was_verified():
    """M6. F2 added `provenance`/`verified_at` to `Topology` precisely so that
    a reader can tell what an entry actually passed - but nothing consumed
    them: this prompt rendered only `description` and `addresses`, so a
    `verified_at="nominal"` entry looked identical to a corner-verified one to
    the agent choosing the swap. A field that nothing reads is not a record.

    Mutation this catches: dropping the two f-string fields from
    `candidate_descriptions` (observed: 'verified_at: nominal' is absent from
    the prompt and the assertion fails)."""
    library = {
        "nominal_only": Topology(
            id="nominal_only",
            description="an LLM-authored variant",
            subckt_body="Rz vnull vout 500\n",
            addresses=["phase_margin"],
            ports=["vinp", "vinn", "vout", "vdd", "vss"],
            assumes_scale=1e-6,
            provenance="authored",
            verified_at="nominal",
        ),
    }
    candidates = [SwapCandidate(block_path="AMP", topology_id="nominal_only")]
    with patch("analogcoder.agents.tuner.run_agent", new=AsyncMock(return_value={})) as mock_run:
        await propose_topology_swap(
            structure_view="x",
            judge_result={"overall_pass": False},
            candidates=candidates,
            library=library,
            rejection_feedback=None,
            backend=object(),
        )

    prompt = mock_run.call_args.kwargs["user_prompt"]
    assert "provenance: authored" in prompt
    assert "verified_at: nominal" in prompt
    # ... and the system prompt must say what those words mean, or rendering
    # them is decoration the agent cannot act on.
    assert "verified_at: corners" in TOPOLOGY_TUNER_SYSTEM_PROMPT
    assert "prefer the one verified at corners" in TOPOLOGY_TUNER_SYSTEM_PROMPT
