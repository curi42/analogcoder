import inspect
import json
import math
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

from analogcoder import cli
from analogcoder.agents.backends.claude_sdk import ClaudeSDKBackend
from analogcoder.agents.backends.openai_compatible import OpenAICompatibleBackend
from analogcoder.checkpoint import BOUNDARY_ATTEMPT, read_payload
from analogcoder.cli import (
    AGENT_NAMES,
    _argmax_drift,
    _build_agent_backend,
    _build_agent_backends,
    _run,
    build_arg_parser,
)
from analogcoder.corner_selection import CornerSet
from analogcoder.pvt import CornerPoint

# 최적화 단계가 시뮬레이션 결과를 어떻게 읽는지를 그대로 상대로 삼는다.
# cli.py가 만드는 status를 optimizer가 실제로 거절하는지까지 확인하지 않으면,
# "합쳤다"는 사실만 남고 그것이 무엇을 막는지는 아무도 지키지 않는다.
from analogcoder.optimizer import _run_simulation
from analogcoder.simulators.base import RawSimResult
from analogcoder.topologies import TOPOLOGY_LIBRARY
from analogcoder.topology_match import SwapCandidate

SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    "    analyses: [\"ac\"]\n"
    "    control_block: |\n"
    "      .control\n"
    "      .endc\n"
    "    criteria: []\n"
)

TWO_TESTBENCH_SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
    "  - name: psr_plus\n"
    "    netlist: netlist_psr_plus.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)

OPTIMIZE_SPEC_YAML = (
    "circuit_name: test\n"
    "optimize:\n"
    "  objective: iq_ua\n"
    "  area_budget: 1.1\n"
    "  guard_band: 0.2\n"
    "pvt_corners:\n"
    "  process: [tt]\n"
    "  voltage: [1.8]\n"
    "  temperature: [27]\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)

OPTIMIZE_NO_CORNERS_SPEC_YAML = (
    "circuit_name: test\n"
    "optimize:\n"
    "  objective: iq_ua\n"
    "  area_budget: 1.1\n"
    "  guard_band: 0.2\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria: []\n"
)


# 메인 루프가 고쳐 놓은 덱. 원본과 **내용이 달라야** 한다 - 최적화가 어느
# 쪽을 받는지는 키 집합으로는 구별되지 않기 때문이다(둘 다 테스트벤치 이름이
# 키다). 이 상수가 없으면 원본을 넘기는 배선 실수가 전 테스트를 통과한다.
TUNED_TEXT = "* tuned by the main loop\nM1 d g 0 0 nch W=44 L=1\n.end\n"


def _orchestration(result, captured: dict | None = None):
    """run_orchestration 대역. **v0을 push하고 그 위에 튜닝된 v1을 push한다.**

    진짜 run_orchestration은 첫 줄에서 state.push_netlist_version을 부르고
    (orchestrator.py:61) 그 뒤로 튜닝이 적용될 때마다 새 버전을 민다. 그것을
    흉내내지 않는 mock은 프로덕션에 존재하지 않는 모양(버전이 없거나, 현재
    덱이 원본과 똑같은 RunState)을 남긴다. 후자가 특히 위험하다: 최적화에
    현재 덱 대신 원본을 넘기는 배선 실수가 그 상태에서는 아무 테스트도 깨지
    않고, 실제 실행에서는 튜닝 결과를 통째로 버린 덱이 확정되고 보고된다."""

    async def fake(initial_netlist_texts, spec, state, agents, **kwargs):
        state.push_netlist_version(initial_netlist_texts)
        state.push_netlist_version({name: TUNED_TEXT for name in initial_netlist_texts})
        if captured is not None:
            captured["spec"] = spec
            captured["state"] = state
            captured["agents"] = agents
        return result

    return fake


def _one_history_event(run_dir: str, step: str) -> dict:
    """history.jsonl에서 그 step 이벤트 하나를 꺼낸다. 없거나 둘 이상이면 실패."""
    with open(os.path.join(run_dir, "history.jsonl")) as f:
        events = [json.loads(line) for line in f if line.strip()]
    matching = [e for e in events if e["step"] == step]
    assert len(matching) == 1, f"expected exactly one {step!r} event, got {len(matching)}"
    return matching[0]


def _pass_result(run_dir: str) -> dict:
    return {
        "status": "PASS",
        "final_netlist_paths": {},
        "run_dir": run_dir,
        "iterations_used": 1,
        "final_criteria": [],
    }


def test_arg_parser_requires_spec_only():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    assert args.spec == "s.yaml"
    assert args.simulator == "ngspice"
    assert args.agent_backend == "claude"
    assert not hasattr(args, "netlist")


def test_build_agent_backend_returns_claude_backend_by_default():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])
    backend = _build_agent_backend(args)
    assert isinstance(backend, ClaudeSDKBackend)


def test_build_agent_backend_returns_openai_compatible_backend_when_configured():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--spec", "s.yaml",
            "--agent-backend", "openai-compatible",
            "--llm-base-url", "http://local",
            "--llm-model", "glm-5.2",
        ]
    )
    backend = _build_agent_backend(args)
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url == "http://local"
    assert backend.model == "glm-5.2"
    assert backend.api_key_env == "LOCAL_LLM_API_KEY"


def test_build_agent_backend_raises_when_openai_compatible_missing_config():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-backend", "openai-compatible"])
    with pytest.raises(ValueError):
        _build_agent_backend(args)


@pytest.mark.asyncio
async def test_run_wires_orchestration_and_returns_its_result(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    fake_result = {
        "status": "PASS",
        "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "runs" / "r1" / "netlist_v0_ac_loop_gain.cir")},
        "run_dir": str(tmp_path / "runs" / "r1"),
        "iterations_used": 1,
        "final_criteria": [],
    }

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r1")]
    )

    with patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)):
        result = await _run(args)

    # `result == fake_result`로는 아무것도 못 지킨다 - _run은 같은 dict를
    # 제자리에서 고치므로 어떤 키를 더해도 그 비교는 참이다. 오케스트레이션이
    # 낸 값이 그대로 통과했는지를 키별로 본다.
    assert result["status"] == "PASS"
    assert result["run_dir"] == str(tmp_path / "runs" / "r1")
    assert result["iterations_used"] == 1
    assert result["final_criteria"] == []


@pytest.mark.asyncio
async def test_propose_topology_fn_calls_propose_topology_swap_with_the_new_contract(tmp_path):
    """propose_topology_fn is the one call site in this whole module no other
    test exercises: test_orchestrator.py injects its own fake `propose_topology`
    directly as `agents.propose_topology`, bypassing this wrapper entirely, and
    the end-to-end test that would hit it for real is skip-gated. Before this
    test, propose_topology_fn still called `propose_topology_swap` with the OLD
    4-positional-argument shape
    (structure_view, judge_result, available_topologies, rejection_feedback,
    backend) - the function's real signature is now
    (structure_view, judge_result, candidates, library, rejection_feedback,
    backend), so a real run would TypeError the first time a topology swap
    triggered, with the 729-test suite staying green throughout.

    autospec=True binds the mock to propose_topology_swap's real signature, so
    the old call shape fails here with a TypeError (missing `backend`) even
    without the explicit position assertions below - those assertions pin the
    *order*, so a future swap of two same-typed positional arguments (e.g.
    candidates/library) is still caught.
    """
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)
    run_dir = str(tmp_path / "runs" / "r1")

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    captured: dict = {}
    with patch(
        "analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir), captured)
    ):
        await _run(args)

    candidates = [SwapCandidate(block_path="AMP", topology_id="miller_basic")]
    judge_result = {"overall_pass": False, "criteria": []}

    with patch("analogcoder.cli.propose_topology_swap", autospec=True) as mock_swap:
        mock_swap.return_value = {"topology_id": "miller_basic", "reasoning": "x", "confidence": 80}
        result = await captured["agents"].propose_topology(
            "structure view", judge_result, candidates, TOPOLOGY_LIBRARY, None
        )

    assert result == {"topology_id": "miller_basic", "reasoning": "x", "confidence": 80}
    called_args = mock_swap.call_args.args
    assert called_args[0] == "structure view"
    assert called_args[1] == judge_result
    assert called_args[2] == candidates
    assert called_args[3] is TOPOLOGY_LIBRARY
    assert called_args[4] is None
    assert isinstance(called_args[5], ClaudeSDKBackend)


@pytest.mark.asyncio
async def test_run_passes_one_netlist_text_per_testbench_to_run_orchestration(tmp_path):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* psr netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
        "  - name: psr_plus\n"
        "    netlist: netlist_psr_plus.cir\n"
        "    analyses: [\"ac\"]\n"
        "    control_block: \".control\\n.endc\\n\"\n"
        "    criteria: []\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r2")])

    captured = {}

    async def fake_run_orchestration(initial_netlist_texts, spec, state, agents, **kwargs):
        state.push_netlist_version(initial_netlist_texts)
        captured["texts"] = initial_netlist_texts
        return {
            "status": "PASS",
            "final_netlist_paths": {},
            "run_dir": str(tmp_path / "runs" / "r2"),
            "iterations_used": 1,
            "final_criteria": [],
        }

    with patch("analogcoder.cli.run_orchestration", new=fake_run_orchestration):
        await _run(args)

    assert captured["texts"] == {"ac_loop_gain": "* ac netlist\n.end\n", "psr_plus": "* psr netlist\n.end\n"}


@pytest.mark.asyncio
async def test_run_skips_pvt_sweep_when_spec_has_no_pvt_corners(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r3")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {}, "run_dir": str(tmp_path / "runs" / "r3"),
        "iterations_used": 1, "final_criteria": [],
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep") as mock_sweep,
    ):
        result = await _run(args)

    mock_sweep.assert_not_called()
    assert result["status"] == "PASS"
    assert "pvt_sweep" not in result


@pytest.mark.asyncio
async def test_run_overrides_pass_to_fail_when_final_pvt_sweep_fails(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r4")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r4"), "iterations_used": 1, "final_criteria": [],
    }
    fake_final_sweep = {
        "overall_pass": False, "criteria": [], "summary": "one or more criteria failed",
        "worst_case_corners": {"gain": {"process": "tt", "voltage": 1.8, "temperature": 27, "value": 5.0}},
    }

    # 이 스펙에는 optimize 블록이 없으므로 최적화는 SKIPPED로 지나가고
    # (pvt_sweep=None) 최종 스윕이 실제로 돈다 - 재사용 경로가 아니다.
    # run_full_pvt_sweep은 mock이라 넷리스트 인자를 보지 않는다.
    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_final_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2  # baseline sweep + final sweep
    assert result["status"] == "FAIL"
    assert "pvt_sweep" in result
    assert result["pvt_sweep"]["worst_case_corners"]["gain"]["process"] == "tt"


@pytest.mark.asyncio
async def test_run_keeps_pass_when_final_pvt_sweep_also_passes(tmp_path):
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria:\n"
        "      - name: gain\n"
        "        measurement: gain_db\n"
        '        operator: ">="\n'
        "        threshold: 10.0\n"
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r5")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r5"), "iterations_used": 1, "final_criteria": [],
    }
    fake_passing_sweep = {
        "overall_pass": True, "criteria": [], "summary": "all criteria passed",
        "worst_case_corners": {},
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=fake_passing_sweep) as mock_sweep,
    ):
        result = await _run(args)

    assert mock_sweep.call_count == 2
    assert result["status"] == "PASS"
    assert result["pvt_sweep"]["overall_pass"] is True


class _StubSimBackend:
    """run_full_pvt_sweep을 진짜로 돌리기 위한 최소 시뮬레이터 대역.
    NgspiceBackend 자체를 갈아 끼우므로 CachingSimulator가 실제로 그 위를
    감싸고, sim_backend.identity()/run()이 실제로 불린다."""

    def identity(self) -> str:
        return "stub-ngspice"

    def run(self, netlist_path, testbench_config):
        return RawSimResult(status="success", measurements={}, raw_log="", warnings=[])


@pytest.mark.asyncio
async def test_the_entry_and_verdict_sweeps_each_log_corner_render_per_testbench(tmp_path):
    """T1: `pvt.run_full_pvt_sweep`의 `log_event`는 기본값 `None`이고, 생산
    호출부(cli.py)가 그것을 넘기지 않으면 `corner_render` 이벤트가 진입 스윕과
    판정 스윕 어디에서도 history.jsonl에 남지 않는다 - 이 사건이
    CLAUDE.md/부록 B가 세는 "조용히 무력한 게이트"의 정확히 그 모양이다.

    `run_full_pvt_sweep` 자체를 mock하면(이 파일의 다른 pvt 테스트들처럼) 이
    결함을 통과시킨다 - mock은 cli.py가 넘기는 kwargs를 실행하지 않으므로.
    그래서 여기서는 그 함수는 진짜로 돌리고 그 아래 `NgspiceBackend`만 스텁으로
    바꾼다: cli.py가 실제로 `log_event=state.log_event`를 넘기는지를 건다."""
    (tmp_path / "netlist.cir").write_text("* netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* netlist2\n.end\n")
    spec_path = tmp_path / "spec_pvt.yaml"
    spec_path.write_text(
        "circuit_name: test\n"
        "pvt_corners:\n"
        "  process: [tt]\n"
        "  voltage: [1.8]\n"
        "  temperature: [27]\n"
        "testbenches:\n"
        "  - name: ac_loop_gain\n"
        "    netlist: netlist.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria: []\n"
        "  - name: psr_plus\n"
        "    netlist: netlist_psr_plus.cir\n"
        '    analyses: ["ac"]\n'
        '    control_block: ".control\\n.endc\\n"\n'
        "    criteria: []\n"
    )

    parser = build_arg_parser()
    run_dir = tmp_path / "runs" / "corner_render_wiring"
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(run_dir)])

    fake_result = {
        "status": "PASS",
        "final_netlist_paths": {
            "ac_loop_gain": str(tmp_path / "netlist.cir"),
            "psr_plus": str(tmp_path / "netlist_psr_plus.cir"),
        },
        "run_dir": str(run_dir), "iterations_used": 1, "final_criteria": [],
    }

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fake_result)),
        patch("analogcoder.cli.NgspiceBackend", return_value=_StubSimBackend()),
    ):
        result = await _run(args)

    assert result["status"] == "PASS"

    with open(os.path.join(str(run_dir), "history.jsonl")) as f:
        events = [json.loads(line) for line in f if line.strip()]
    renders = [e for e in events if e["step"] == "corner_render"]

    # 진입 스윕(테스트벤치당 1건) + 판정 스윕(테스트벤치당 1건) = 4건.
    # log_event가 배선되지 않으면 이 리스트는 비어 있다 - 부분적으로 배선된
    # 경우(둘 중 하나만)와도 구별하려고 테스트벤치별 개수까지 확인한다.
    assert len(renders) == 4, (
        f"expected 4 corner_render events (entry+verdict sweeps x 2 testbenches), "
        f"got {len(renders)}: {renders}"
    )
    per_tb: dict[str, int] = {}
    for e in renders:
        per_tb[e["testbench"]] = per_tb.get(e["testbench"], 0) + 1
    assert per_tb == {"ac_loop_gain": 2, "psr_plus": 2}


@pytest.mark.asyncio
async def test_run_hands_orchestration_include_resolved_netlist_texts(tmp_path):
    # The orchestration loop simulates the netlist copies RunState stages into
    # the run dir, not the originals in the benchmark dir. A bare relative
    # `.include "pdk_corner.inc"` cannot resolve from there, so _run must
    # absolutize includes against the netlist's own directory before handing
    # the texts off - otherwise every simulation of a real PDK-based benchmark
    # fails with "could not find include file" and the whole loop runs on
    # empty measurements.
    (tmp_path / "pdk_corner.inc").write_text("* pdk\n")
    (tmp_path / "netlist.cir").write_text('* netlist\n.include "pdk_corner.inc"\n.end\n')
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", str(tmp_path / "runs" / "r6")])

    fake_result = {
        "status": "PASS", "final_netlist_paths": {"ac_loop_gain": str(tmp_path / "netlist.cir")},
        "run_dir": str(tmp_path / "runs" / "r6"), "iterations_used": 1, "final_criteria": [],
    }

    with patch(
        "analogcoder.cli.run_orchestration", new=AsyncMock(side_effect=_orchestration(fake_result))
    ) as mock_orch:
        await _run(args)

    passed_texts = mock_orch.await_args.args[0]
    assert f'.include "{tmp_path / "pdk_corner.inc"}"' in passed_texts["ac_loop_gain"]


def test_claude_backend_defaults_to_sonnet():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml"])

    backends = _build_agent_backends(args)

    # judge는 여기 없다. 판정은 `judge_tools.evaluate_criteria` 직접 호출로
    # 대체됐으므로 LLM 백엔드를 만들 자리 자체가 없다.
    assert set(backends) == {"simulator", "tuner", "verifier", "optimizer"}
    assert all(b.model == "sonnet" for b in backends.values())


def test_claude_model_flag_sets_every_agent_model():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--claude-model", "haiku"])

    backends = _build_agent_backends(args)

    assert all(b.model == "haiku" for b in backends.values())


def test_agent_model_flag_overrides_a_single_agent():
    # Lets a run drop one agent to a weaker model to see whether the pipeline
    # still holds - the simulator is the one tool-calling agent left, and a
    # lower-capability model has historically struggled with it.
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", "s.yaml", "--claude-model", "sonnet", "--agent-model", "simulator=haiku"]
    )

    backends = _build_agent_backends(args)

    assert backends["simulator"].model == "haiku"
    assert backends["tuner"].model == "sonnet"


def test_agent_model_flag_rejects_an_unknown_agent_name():
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-model", "nosuchagent=haiku"])

    with pytest.raises(ValueError, match="nosuchagent"):
        _build_agent_backends(args)


def test_agent_model_flag_rejects_judge_because_judging_is_no_longer_an_llm():
    # judge는 에이전트가 아니게 됐으므로 `--agent-model judge=...`는 이제
    # 에러다. 조용히 무시하면 사용자가 "판정 모델을 바꿨다"고 믿은 채
    # 아무것도 바뀌지 않은 실행을 얻는다.
    assert "judge" not in AGENT_NAMES

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", "s.yaml", "--agent-model", "judge=haiku"])

    with pytest.raises(ValueError, match="judge"):
        _build_agent_backends(args)


def test_optimizer_is_an_agent_whose_model_can_be_overridden():
    # 최적화 제안도 LLM 호출이므로 다른 에이전트와 같은 자리에 있어야 한다 -
    # 아니면 그 한 에이전트만 모델을 내려 실험할 방법이 없다.
    assert "optimizer" in AGENT_NAMES

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", "s.yaml", "--claude-model", "sonnet", "--agent-model", "optimizer=haiku"]
    )

    backends = _build_agent_backends(args)

    assert backends["optimizer"].model == "haiku"
    assert backends["tuner"].model == "sonnet"


# --- 지뢰 1: 테스트벤치를 가로지르는 status ------------------------------------


async def _capture_simulate_fn(tmp_path, spec_yaml: str, run_dir: str):
    """_run을 한 번 돌려 orchestrator에 넘어가는 simulate 콜러블을 꺼낸다.

    최적화 단계도 **같은 콜러블**을 받으므로(cli.py가 simulate_fn 하나를 두
    소비자에게 준다) 여기서 꺼낸 것이 곧 optimizer가 보는 것이다."""
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    (tmp_path / "netlist_psr_plus.cir").write_text("* psr netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    captured: dict = {}
    with patch(
        "analogcoder.cli.run_orchestration",
        new=_orchestration(_pass_result(run_dir), captured),
    ):
        await _run(args)

    # 프로덕션이 simulate에 넘기는 것은 **state의 현재 덱**이다(orchestrator도
    # optimizer도 push 뒤에 시뮬레이션한다). 빈 dict를 넘기는 대역은 오늘의
    # simulate_fn이 그 인자를 안 읽기 때문에만 통과하고, corner-aware simulate는
    # netlist_texts[tb.name]을 실제로 읽으므로 그 순간 KeyError가 된다 -
    # 대역이 프로덕션 모양을 흉내내지 않아 생기는 실패다.
    return captured["agents"].simulate, captured["spec"], captured["state"].current_netlist_texts()


@pytest.mark.asyncio
async def test_simulate_fn_merges_a_non_success_status_across_testbenches(tmp_path):
    # 한 테스트벤치가 수렴하지 못했는데 합쳐진 결과가 성공으로 보이면,
    # 최적화는 수렴 실패한 해의 측정값으로 마진을 태우는 결정을 내린다
    # (실제로 iq_ua=1.0이 235->1 개선으로 수락된 적이 있다). optimizer는
    # status 키가 **없으면** 성공으로 읽으므로, 이 신호가 없는 것은 틀린 것보다
    # 나쁘다 - 어떤 mock에도 보이지 않는다.
    run_dir = str(tmp_path / "runs" / "s1")
    # control_block은 프로덕션의 SIMULATION_SCHEMA가 required로 두는 키다.
    # 이 대역은 cli.agent_simulate를 통째로 갈아끼워 스키마 검증을 지나치므로
    # 없어도 오늘은 통과하지만, 프로덕션이 내놓지 않는 모양을 흉내내는 대역은
    # 그 키를 읽는 소비자가 생기는 순간 조용히 폴백 경로만 재게 된다.
    per_testbench = [
        {"measurements": {"gain_db": 40.0}, "status": "success", "warnings": [],
         "control_block": ".control\n.endc\n"},
        {"measurements": {"iq_ua": 1.0}, "status": "convergence_failure", "warnings": [],
         "control_block": ".control\n.endc\n"},
    ]

    async def fake_agent_simulate(*args, **kwargs):
        return per_testbench.pop(0)

    with patch("analogcoder.cli.agent_simulate", new=fake_agent_simulate):
        simulate_fn, spec, texts = await _capture_simulate_fn(
            tmp_path, TWO_TESTBENCH_SPEC_YAML, run_dir
        )
        merged = await simulate_fn(texts, spec)

    assert merged["status"] == "convergence_failure"
    # 전부 성공했을 때만 성공이라는 규칙이 소비자 쪽에서 실제로 걸리는가.
    result, reason = await _run_simulation(
        lambda texts_arg, spec_arg: _as_coroutine(merged), texts, spec
    )
    assert result is None
    assert "convergence_failure" in reason


@pytest.mark.asyncio
async def test_simulate_fn_reports_success_only_when_every_testbench_succeeded(tmp_path):
    # 반대 방향. status를 무조건 실패로 박아도 위 테스트는 통과하므로,
    # 성공 경로가 성공으로 남는지 같이 고정한다.
    run_dir = str(tmp_path / "runs" / "s2")
    per_testbench = [
        {"measurements": {"gain_db": 40.0}, "status": "success", "warnings": [],
         "control_block": ".control\n.endc\n"},
        {"measurements": {"iq_ua": 200.0}, "status": "success", "warnings": [],
         "control_block": ".control\n.endc\n"},
    ]

    async def fake_agent_simulate(*args, **kwargs):
        return per_testbench.pop(0)

    with patch("analogcoder.cli.agent_simulate", new=fake_agent_simulate):
        simulate_fn, spec, texts = await _capture_simulate_fn(
            tmp_path, TWO_TESTBENCH_SPEC_YAML, run_dir
        )
        merged = await simulate_fn(texts, spec)

    assert merged["status"] == "success"
    assert merged["measurements"] == {"gain_db": 40.0, "iq_ua": 200.0}
    result, reason = await _run_simulation(
        lambda texts_arg, spec_arg: _as_coroutine(merged), texts, spec
    )
    assert result is merged
    assert reason is None


async def _as_coroutine(value):
    return value


# --- 판정은 LLM이 아니다 -------------------------------------------------------


JUDGED_SPEC_YAML = (
    "circuit_name: test\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria:\n"
    "      - name: gain\n"
    "        measurement: gain_db\n"
    '        operator: ">="\n'
    "        threshold: 60.0\n"
    "        unit: dB\n"
)


async def _capture_judge_fn(tmp_path, run_dir: str):
    """_run을 한 번 돌려 orchestrator에 넘어가는 judge 콜러블을 꺼낸다."""
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(JUDGED_SPEC_YAML)

    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    captured: dict = {}
    with patch(
        "analogcoder.cli.run_orchestration",
        new=_orchestration(_pass_result(run_dir), captured),
    ):
        await _run(args)

    return captured["agents"].judge, captured["spec"]


@pytest.mark.asyncio
async def test_a_missing_measurement_is_judged_NaN_and_never_zero(tmp_path):
    """이 변경이 고치는 결함. 시뮬레이션이 실패해 measurements가 비면 LLM
    judge는 `actual=0, margin=0`을 **지어냈다**(재생한 58개 judge 이벤트 중
    25건). 결정론 경로는 `NaN`을 쓴다 - "재지 못했다"와 "재보니 0이었다"는
    다른 사실이고, 0은 임계값과 비교되는 순간 거짓 마진이 된다."""
    judge_fn, spec = await _capture_judge_fn(tmp_path, str(tmp_path / "runs" / "j1"))

    result = await judge_fn({}, spec)

    assert result["overall_pass"] is False
    (criterion,) = result["criteria"]
    assert criterion["name"] == "gain"
    assert math.isnan(criterion["actual"]), criterion
    assert math.isnan(criterion["margin"]), criterion
    assert criterion["pass"] is False


@pytest.mark.asyncio
async def test_judge_fn_stays_a_coroutine_and_measures_a_present_value(tmp_path):
    # 반대 방향: NaN만 고정하면 "항상 NaN"인 구현도 통과한다.
    # 그리고 orchestrator는 `await agents.judge(...)`로 부르므로 코루틴이어야 한다.
    judge_fn, spec = await _capture_judge_fn(tmp_path, str(tmp_path / "runs" / "j2"))

    assert inspect.iscoroutinefunction(judge_fn)

    result = await judge_fn({"gain_db": 72.5}, spec)

    assert result["overall_pass"] is True
    (criterion,) = result["criteria"]
    assert criterion["actual"] == 72.5
    assert criterion["margin"] == pytest.approx(12.5)
    # `target`에 단위가 없는 것은 의도된 결과다 - 중간 루프와 최종 스윕이
    # 이제 `evaluate_criteria`의 **같은 문자열**을 낸다. LLM judge는 여기에
    # ">=60.0 dB"를 썼다.
    assert criterion["target"] == ">=60.0"


# --- 최적화 배선 --------------------------------------------------------------


def _optimization_result(**overrides) -> dict:
    base = {
        "status": "UNCHANGED",
        "objective_before": 200.0,
        "objective_after": 200.0,
        "area_before": 1.0,
        "area_after": 1.0,
        "steps_accepted": 0,
        "steps_rejected": 0,
        "corner_confirmed": False,
        "corner_failure": None,
        "pvt_sweep": None,
        "final_netlist_paths": {},
    }
    return {**base, **overrides}


async def _run_with_optimization(
    tmp_path, spec_yaml, run_dir, fake_optimization, sweep_result, extra_args=()
):
    """_run을 돌리되 최적화와 스윕을 둘 다 대역으로 바꾼다.
    (result, mock_sweep, captured) 를 돌려준다."""
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)

    parser = build_arg_parser()
    args = parser.parse_args(
        ["--spec", str(spec_path), "--run-dir", run_dir, *extra_args]
    )

    captured: dict = {}

    with (
        patch(
            "analogcoder.cli.run_orchestration",
            new=_orchestration(_pass_result(run_dir), captured),
        ),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=sweep_result) as mock_sweep,
        patch("analogcoder.cli.run_optimization", new=fake_optimization(captured, mock_sweep)),
    ):
        result = await _run(args)

    return result, mock_sweep, captured


@pytest.mark.asyncio
async def test_optimization_runs_after_pass_and_before_the_final_pvt_sweep(tmp_path):
    # 순서가 계약이다. 최종 스윕이 최적화된 넷리스트를 확정하는 역할을 하려면
    # 최적화가 그 앞에 와야 한다 - 뒤에 두면 아무도 확인하지 않은 넷리스트로
    # 실행이 끝난다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            # 여기까지 온 스윕 호출은 baseline 하나뿐이어야 한다.
            captured["sweeps_before_optimization"] = mock_sweep.call_count
            captured["netlist_texts"] = netlist_texts
            captured["optimizer_simulate"] = agents.simulate
            return _optimization_result()

        return run

    result, mock_sweep, captured = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o1"), fake_optimization, passing
    )

    assert captured["sweeps_before_optimization"] == 1  # baseline만 돌았다
    assert mock_sweep.call_count == 2  # 최적화가 코너를 확인하지 못했으니 최종 스윕이 돈다
    # 최적화는 실행의 **현재** 넷리스트 위에서 돈다 - 파일에서 읽은 원본이
    # 아니다. 키 집합은 둘이 같으므로 **내용**을 본다. 원본을 넘기면
    # run_optimization이 인자와 state의 불일치를 보고 원본을 새 버전으로
    # 밀어 넣어(optimizer.py:534) 메인 루프의 수리를 되돌린다.
    assert captured["netlist_texts"] == {"ac_loop_gain": TUNED_TEXT}
    # 메인 루프가 쓰던 것과 **같은** simulate여야 한다. 최적화 전용 래퍼를 따로
    # 두면 status 병합 같은 규칙이 한쪽에만 붙는다.
    assert captured["optimizer_simulate"] is captured["agents"].simulate
    assert result["optimization"]["status"] == "UNCHANGED"
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_the_area_phase_runs_on_pass_before_run_optimization_and_its_final_criteria_lands_in_the_result(
    tmp_path,
):
    """배선 자체를 잰다 - 대역이 아니라 `cli.py`가 실제로 면적 단계를
    부르는가, `run_optimization`보다 먼저 부르는가, 그리고 `run_optimization`이
    아무 것도 못 쟀을 때(선언이 없거나 무의미해 `final_criteria` 키 자체가
    없는 모양 - `_optimization_result()`의 기본값) 면적 단계가 남긴
    `final_criteria`가 최상위 `result`로 올라오는가.

    호출 순서는 두 대역을 하나의 관리자(Mock)에 붙여 `mock_calls`로 잰다 -
    각자 "불렸다"만 보는 두 단언은 순서가 뒤집혀도 통과한다. 이 테스트가
    잡는 변형: `cli.py`에서 `run_area_optimization` 호출 자체를 지운
    변형(실제로 그 호출을 주석 처리하고 이 테스트가 실패하는 것을 확인함) -
    conftest.py의 `_disable_area_optimization`은 이 테스트에는 적용되지
    않는다. 이 테스트가 그것을 자기 것(`area_mock`)으로 다시 덮기
    때문이다."""
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(OPTIMIZE_SPEC_YAML)

    run_dir = str(tmp_path / "runs" / "area_wiring")
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    area_final_criteria = [
        {"name": "iq_ua", "target": "<=300.0", "actual": 211.68, "pass": True, "margin": 88.32}
    ]
    area_result = {
        "status": "OPTIMIZED",
        "objective_before": 212.99,
        "objective_after": 211.68,
        "area_before": 1.0,
        "area_after": 0.92,
        "steps_accepted": 1,
        "steps_rejected": 0,
        "final_criteria": area_final_criteria,
        "final_netlist_paths": {},
    }
    area_mock = AsyncMock(return_value=area_result)
    # "선언은 있었지만 final_criteria 없이 끝났다"의 모양이다 -
    # _optimization_result()의 기본값에는 final_criteria 키가 아예 없다. 그래야
    # 아래 result["final_criteria"] 단언이 면적 단계의 값**만** 반영한다는 것을
    # 잰다 - run_optimization이 뒤에서 뭔가를 남겼다면 그것과 구별이 안 된다.
    opt_mock = AsyncMock(return_value=_optimization_result())

    order = Mock()
    order.attach_mock(area_mock, "area")
    order.attach_mock(opt_mock, "optimization")

    with (
        patch(
            "analogcoder.cli.run_orchestration",
            new=_orchestration(_pass_result(run_dir)),
        ),
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.run_area_optimization", new=area_mock),
        patch("analogcoder.cli.run_optimization", new=opt_mock),
    ):
        result = await _run(args)

    # 순서가 계약이다: 최종 스윕이 최적화된 넷리스트를 확정하는 역할을
    # 그대로 하려면 면적 단계가 run_optimization보다 먼저 착지해야 한다.
    assert [call[0] for call in order.mock_calls] == ["area", "optimization"]
    assert result["area_optimization"] is area_result
    # run_optimization이 아무것도 못 쟀는데도 result["final_criteria"]가
    # 면적 단계 이전 덱(오케스트레이션 판정)을 기준으로 남으면 리포트가
    # 최적화가 옮긴 덱과 다른 덱을 설명하게 된다.
    assert result["final_criteria"] == area_final_criteria
    assert result["status"] == "PASS"


@pytest.mark.asyncio
async def test_optimization_does_not_run_when_the_orchestration_did_not_pass(tmp_path):
    (tmp_path / "netlist.cir").write_text("* ac netlist\n.end\n")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(OPTIMIZE_NO_CORNERS_SPEC_YAML)

    run_dir = str(tmp_path / "runs" / "o2")
    parser = build_arg_parser()
    args = parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])

    fail_result = {**_pass_result(run_dir), "status": "FAIL", "failure_reason": "max iterations"}

    with (
        patch("analogcoder.cli.run_orchestration", new=_orchestration(fail_result)),
        patch("analogcoder.cli.run_optimization", new=AsyncMock()) as mock_opt,
    ):
        result = await _run(args)

    # 통과하지 못한 설계의 마진을 더 깎을 이유가 없다.
    mock_opt.assert_not_called()
    assert "optimization" not in result


@pytest.mark.asyncio
async def test_verify_corners_handed_to_the_optimizer_is_synchronous(tmp_path):
    # run_optimization은 이것을 await 없이 직접 부른다. async로 감싸면
    # 코루틴 객체가 "쓸 수 없는 결과"로 접혀 최적화 단계 전체가 조용히
    # UNCHANGED가 된다 - 크래시도 로그도 없이.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            captured["verify_corners"] = agents.verify_corners
            # run_optimization이 하는 그대로 - await 없이 직접 부른다.
            captured["returned"] = agents.verify_corners({"ac_loop_gain": "* ac netlist\n.end\n"})
            return _optimization_result()

        return run

    _, mock_sweep, captured = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o3"), fake_optimization, passing
    )

    verify_corners = captured["verify_corners"]
    assert verify_corners is not None
    assert not inspect.iscoroutinefunction(verify_corners)
    # 코루틴이면 run_optimization의 _run_sweep이 "overall_pass가 없는 결과"로
    # 접어 코너를 확인하지 못한 채 조용히 지나간다.
    assert not inspect.isawaitable(captured["returned"])
    assert captured["returned"] is passing


@pytest.mark.asyncio
async def test_verify_corners_is_none_when_the_spec_declares_no_corners(tmp_path):
    # 코너를 잴 수단이 없으면 None을 준다 - 그때 run_optimization은
    # 확인하지 않았다고 보고한다. 빈 스윕을 지어내지 않는다.
    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            captured["verify_corners"] = agents.verify_corners
            return _optimization_result()

        return run

    result, mock_sweep, captured = await _run_with_optimization(
        tmp_path,
        OPTIMIZE_NO_CORNERS_SPEC_YAML,
        str(tmp_path / "runs" / "o4"),
        fake_optimization,
        None,
    )

    assert captured["verify_corners"] is None
    mock_sweep.assert_not_called()
    assert "pvt_sweep" not in result


@pytest.mark.asyncio
async def test_a_confirmed_optimization_sweep_is_reused_as_the_final_sweep(tmp_path):
    # 최적화가 착지시킨 지점은 정의상 스윕을 통과한 버전이다. 같은 덱에
    # 같은 값(bandgap 기준 286초)을 두 번 치를 이유가 없다.
    confirmed = {
        "overall_pass": True, "criteria": [], "summary": "all corners passed",
        "worst_case_corners": {"iq": {"process": "ff", "voltage": 1.98, "temperature": 125}},
    }

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(
                status="OPTIMIZED", objective_after=180.0, steps_accepted=2,
                corner_confirmed=True, pvt_sweep=confirmed,
            )

        return run

    result, mock_sweep, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o5"), fake_optimization, confirmed
    )

    assert mock_sweep.call_count == 1  # baseline 스윕 하나뿐이다
    assert result["pvt_sweep"] is confirmed
    assert result["status"] == "PASS"
    # 두 결과가 같은 키 이름을 쓴다. 어느 쪽도 다른 쪽을 덮지 않는다.
    assert result["optimization"]["pvt_sweep"] is confirmed
    assert result["optimization"]["corner_confirmed"] is True
    # 재사용해도 이력에는 남는다 - history.jsonl에서 pvt_final_sweep을 찾는
    # 사람이 하필 스윕을 가장 많이 돌린 실행에서 빈손이 되면 안 된다.
    event = _one_history_event(str(tmp_path / "runs" / "o5"), "pvt_final_sweep")
    assert event["reused_from_optimization"] is True
    assert event["summary"] == "all corners passed"


@pytest.mark.asyncio
async def test_an_unconfirmed_optimization_keeps_its_corner_failure_and_still_sweeps(tmp_path):
    # 스윕이 **돌지 못한** 사유는 결과에 남아야 한다. "코너가 깨져서
    # 되돌아왔다"와 "스윕을 못 돌려서 되돌아왔다"는 다른 사실이고, 후자는
    # 고칠 대상이 회로가 아니다. 그리고 확인이 없었으므로 최종 스윕은 돈다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(
                corner_failure="corner sweep raised RuntimeError: ngspice died"
            )

        return run

    result, mock_sweep, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o6"), fake_optimization, passing
    )

    assert mock_sweep.call_count == 2  # baseline + 최종
    assert result["pvt_sweep"] is passing
    assert (
        result["optimization"]["corner_failure"]
        == "corner sweep raised RuntimeError: ngspice died"
    )
    assert result["optimization"]["pvt_sweep"] is None
    # 실제로 돈 스윕은 재사용이 아니라고 말한다.
    event = _one_history_event(str(tmp_path / "runs" / "o6"), "pvt_final_sweep")
    assert event["reused_from_optimization"] is False


@pytest.mark.asyncio
async def test_a_failing_optimization_sweep_still_turns_the_run_into_a_fail(tmp_path):
    # 브리프의 산문은 "재사용 경로에서는 status가 FAIL로 뒤집힐 수 없다"고
    # 말하지만, 진입 스윕이 실패한 경우 run_optimization은 **통과하지 않은**
    # 스윕을 실어 UNCHANGED를 낸다. 그 넷리스트는 최적화를 돌리지 않았을 때
    # 최종 스윕이 보게 될 바로 그 덱이므로, 판정은 그때와 같아야 한다.
    failing = {
        "overall_pass": False, "criteria": [], "summary": "one or more criteria failed",
        "worst_case_corners": {},
    }

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(pvt_sweep=failing)

        return run

    result, mock_sweep, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o7"), fake_optimization, failing
    )

    assert mock_sweep.call_count == 1  # 같은 덱을 두 번 재지 않는다
    assert result["status"] == "FAIL"
    # 사유는 **실제로 일어난 일**을 말해야 한다. 이 경로에서 최종 스윕은 돌지
    # 않았으므로 "final PVT sweep failed"라고 적으면 history.jsonl과 대조하는
    # 사람이 있지도 않은 스윕을 찾게 된다.
    assert result["failure_reason"] == (
        "PVT sweep from the optimization phase failed: one or more criteria failed"
    )


@pytest.mark.asyncio
async def test_the_reported_netlist_paths_follow_the_version_optimization_landed_on(tmp_path):
    # 최적화는 넷리스트 버전을 밀고 되돌린다. 실행이 내놓는 경로가 그것을
    # 따라가지 않으면 report가 최적화 **이전** 덱을 가리킨다 - 실제로 실린
    # 회로와 보고된 회로가 다른 상태다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            state.push_netlist_version({"ac_loop_gain": "* optimized\n.end\n"})
            captured["landed"] = state.current_netlist_paths()
            return _optimization_result(status="OPTIMIZED", steps_accepted=1, pvt_sweep=passing)

        return run

    result, _, captured = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o9"), fake_optimization, passing
    )

    assert result["final_netlist_paths"] == captured["landed"]
    with open(result["final_netlist_paths"]["ac_loop_gain"]) as f:
        assert f.read() == "* optimized\n.end\n"


@pytest.mark.asyncio
async def test_the_optimizer_proposal_runs_on_the_optimizer_agent_backend(tmp_path):
    # propose가 다른 에이전트의 백엔드에 배선되면 --agent-model optimizer=...가
    # 아무 데도 닿지 않는다. 모델을 갈라 두고 실제로 넘어간 백엔드를 본다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            captured["propose"] = agents.propose
            return _optimization_result()

        return run

    _, _, captured = await _run_with_optimization(
        tmp_path,
        OPTIMIZE_SPEC_YAML,
        str(tmp_path / "runs" / "o8"),
        fake_optimization,
        passing,
        extra_args=["--claude-model", "sonnet", "--agent-model", "optimizer=haiku"],
    )

    with patch("analogcoder.cli.propose_candidates", new=AsyncMock(return_value={})) as mock_propose:
        await captured["propose"]("structure", [{"name": "gain"}], "iq_ua", "netlist")

    args = mock_propose.await_args.args
    assert args[:4] == ("structure", [{"name": "gain"}], "iq_ua", "netlist")
    assert args[4].model == "haiku"


# --- 최종 리뷰 Finding 2: 결과가 최적화 **전** 회로를 설명하고 있었다 --------


@pytest.mark.asyncio
async def test_the_reported_criteria_come_from_the_version_optimization_landed_on(tmp_path):
    # final_netlist_paths는 착지 버전으로 갱신되는데 final_criteria는 메인
    # 루프의 judge 결과(최적화 **전** 덱)로 남아 있었다. 실측 bandgap 실행에서
    # 212.25uA를 재는 넷리스트 경로 옆에 212.99uA가 적혔다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}
    landed = [{"name": "iq", "target": "<=300.0", "actual": 212.25, "pass": True,
               "margin": -87.75}]

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(status="OPTIMIZED", final_criteria=landed)

        return run

    result, _, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o7"), fake_optimization, passing
    )

    assert result["final_criteria"] == landed


@pytest.mark.asyncio
async def test_an_optimization_without_criteria_leaves_the_main_loop_judgement_alone(tmp_path):
    # 최적화가 기준을 재지 못한 경로(기준선 시뮬레이션 실패, 이 단계 자체가
    # 터진 경우)가 있다. 그때 없는 값으로 덮으면 리포트가 통째로 빈다.
    passing = {"overall_pass": True, "criteria": [], "summary": "ok", "worst_case_corners": {}}

    def fake_optimization(captured, mock_sweep):
        async def run(netlist_texts, spec, state, agents):
            return _optimization_result(failure="AgentExecutionError: rate limited")

        return run

    result, _, _ = await _run_with_optimization(
        tmp_path, OPTIMIZE_SPEC_YAML, str(tmp_path / "runs" / "o8"), fake_optimization, passing
    )

    assert result["final_criteria"] == []   # 메인 루프가 남긴 것 그대로
    assert result["optimization"]["failure"] == "AgentExecutionError: rate limited"


# --- 코너 축소 배선: 재진입, 경로 불일치, argmax drift ------------------------


CORNER_REDUCTION_SPEC_YAML = (
    "circuit_name: test\n"
    "pvt_corners:\n"
    "  process: [tt, ff, ss, sf, fs]\n"
    "  voltage: [1.62, 1.8, 1.98]\n"
    "  temperature: [-40, 27, 125]\n"
    "corner_reduction:\n"
    "  enabled: true\n"
    "  retry_budget: 2\n"
    "  probe: true\n"
    "testbenches:\n"
    "  - name: ac_loop_gain\n"
    "    netlist: netlist.cir\n"
    '    analyses: ["ac"]\n'
    '    control_block: ".control\\n.endc\\n"\n'
    "    criteria:\n"
    "      - name: gain\n"
    "        measurement: gain_db\n"
    '        operator: ">="\n'
    "        threshold: 10.0\n"
    "      - name: pm\n"
    "        measurement: phase_margin\n"
    '        operator: ">="\n'
    "        threshold: 50.0\n"
)

CORNERS_BUT_REDUCTION_DISABLED_SPEC_YAML = CORNER_REDUCTION_SPEC_YAML.replace(
    "  enabled: true\n", "  enabled: false\n"
)

ORIGINAL_TEXT = "* ac netlist\n.end\n"


def _corner_args(tmp_path, spec_yaml: str, run_dir: str):
    (tmp_path / "netlist.cir").write_text(ORIGINAL_TEXT)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml)
    parser = build_arg_parser()
    return parser.parse_args(["--spec", str(spec_path), "--run-dir", run_dir])


def _wc(process: str, value: float, voltage: float = 1.98, temperature: float = 125.0) -> dict:
    """worst_case_corners 항목 하나. 좌표는 스펙의 pvt_corners에 실제로 있는
    값이어야 한다 - 없는 좌표는 프로덕션 스윕이 절대 내놓지 않는 모양이다."""
    return {"process": process, "voltage": voltage, "temperature": temperature, "value": value}


def _sweep(worst_corners: dict, failing=()) -> dict:
    """run_full_pvt_sweep이 내놓는 모양.

    criteria 항목의 통과 키는 **"pass"**다(judge_tools.evaluate_criteria) -
    "passed"가 아니다. 여기서 틀리면 실패한 기준 이름이 하나도 안 잡혀,
    성장 경로가 항상 "새 코너 없음"으로 접히고 그것이 경로 불일치로 보고된다."""
    failing = set(failing)
    return {
        "overall_pass": not failing,
        "criteria": [
            {
                "name": name,
                "target": ">=10.0",
                "actual": raw["value"],
                "pass": name not in failing,
                "margin": 0.0,
            }
            for name, raw in worst_corners.items()
        ],
        "summary": "all criteria passed" if not failing else "one or more criteria failed",
        "worst_case_corners": dict(worst_corners),
        "per_corner": [],
    }


def _orchestration_sequence(results, calls: list):
    """호출마다 다음 결과를 주는 run_orchestration 대역. 재진입을 재려면
    같은 결과를 반복해 주는 대역으로는 부족하다 - 몇 번 불렸는지가 요점이다.

    `_orchestration`과 같은 이유로 v0을 push하고 그 위에 튜닝된 v1을 push한다
    (그 docstring 참조). **받은 인자**를 calls에 남기는 것이 핵심이다: state를
    남기면 이 대역이 방금 밀어 넣은 TUNED_TEXT를 되읽게 되어, 재진입에 원본을
    넘기는 배선 실수가 그대로 통과한다.

    결과는 매번 **복사해서** 준다. 진짜 run_orchestration은 호출마다 새 dict를
    만드는데, cli는 그 dict를 제자리에서 고친다(status를 FAIL로 뒤집는다).
    같은 객체를 두 번 주면 첫 시도의 FAIL이 두 번째 시도의 결과에 남는다."""

    async def fake(initial_netlist_texts, spec, state, agents, **kwargs):
        calls.append(dict(initial_netlist_texts))
        state.push_netlist_version(initial_netlist_texts)
        state.push_netlist_version({name: TUNED_TEXT for name in initial_netlist_texts})
        return dict(results[min(len(calls) - 1, len(results) - 1)])

    return fake


def _sweep_sequence(sweeps, calls: list):
    """run_full_pvt_sweep 대역. 진입 스윕이 첫 호출이고 그 뒤가 판정 스윕들이다."""

    def fake(netlist_texts, spec, sim_backend, log_event=None):
        calls.append(dict(netlist_texts))
        return sweeps[min(len(calls) - 1, len(sweeps) - 1)]

    return fake


def _history_events(run_dir: str, step: str) -> list[dict]:
    with open(os.path.join(run_dir, "history.jsonl")) as f:
        events = [json.loads(line) for line in f if line.strip()]
    return [e for e in events if e["step"] == step]


@pytest.mark.asyncio
async def test_a_failing_verdict_sweep_grows_the_set_and_retunes(tmp_path):
    # 오늘은 여기서 FAIL 보고하고 끝난다. 재진입을 지우는 변형은 attempts==0과
    # status=="FAIL"을 남기므로 두 단언이 함께 잡는다.
    run_dir = str(tmp_path / "runs" / "c1")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})
    verdict_fail = _sweep({"gain": _wc("ff", 12.0), "pm": _wc("fs", 55.0)}, failing=["gain"])
    verdict_pass = _sweep({"gain": _wc("ff", 45.0), "pm": _wc("fs", 55.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 2                       # 재진입이 실제로 일어났다
    assert result["corner_reduction"]["attempts"] == 1
    assert result["corner_reduction"]["active"] is True
    assert "ff/1.98/125.0" in result["corner_reduction"]["final_set"]
    assert result["corner_reduction"]["grown"] == [["ff/1.98/125.0"]]
    assert result["status"] == "PASS"
    # 자란 집합은 이력에도 남는다 - 어느 기준의 실패가 어느 코너를 불렀는지.
    grown_events = _history_events(run_dir, "corner_set_grown")
    assert len(grown_events) == 1
    assert grown_events[0]["added"] == ["ff/1.98/125.0"]
    assert grown_events[0]["failing_criteria"] == ["gain"]


@pytest.mark.asyncio
async def test_the_retry_is_seeded_from_the_converged_deck_not_the_original(tmp_path):
    # 되돌리면 앞선 튜닝의 진전을 통째로 버린다. 재진입에 원본을 넘기는
    # 변형을 이 단언이 잡는다 - 두 번째 호출이 받은 덱이 v1이어야 한다.
    run_dir = str(tmp_path / "runs" / "c2")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict_fail = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])
    verdict_pass = _sweep({"gain": _wc("ff", 45.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert orch_calls[0]["ac_loop_gain"] == ORIGINAL_TEXT
    assert orch_calls[1]["ac_loop_gain"] == TUNED_TEXT


@pytest.mark.asyncio
async def test_the_retry_budget_is_respected(tmp_path):
    # 예산을 무시하는 변형은 스윕이 계속 실패하는 시나리오에서 끝나지 않는다.
    # 매번 다른 코너가 실패해야 집합이 계속 자라고 경로 불일치로 빠지지 않는다.
    run_dir = str(tmp_path / "runs" / "c3")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    fails = [_sweep({"gain": _wc(p, 12.0)}, failing=["gain"]) for p in ("ff", "ss", "sf")]

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, *fails], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["corner_reduction"]["attempts"] == 2      # retry_budget=2
    assert len(orch_calls) == 3                             # 최초 1회 + 재진입 2회
    assert result["status"] == "FAIL"
    assert result["corner_reduction"]["path_disagreement"] is None
    # 마지막 실패의 코너(sf)는 예산이 끝났으므로 더해지지 않는다.
    assert result["corner_reduction"]["final_set"] == [
        "(deck)", "fs/1.98/125.0", "ff/1.98/125.0", "ss/1.98/125.0"
    ]


@pytest.mark.asyncio
async def test_a_swap_kept_in_an_earlier_attempt_survives_into_the_final_result(tmp_path):
    """재진입은 **수렴된 덱에서 시작한다**(그 배선은 바로 위 테스트가 못박는다).
    그래서 attempt 0이 유지한 스왑은 마지막에 돌려주는 덱에 구조로 그대로
    남아 있는데, cli는 시도마다 run_orchestration의 결과로 result를 통째로
    덮는다 - 누적하지 않으면 result.json이 "스왑 없음"이라고 말하면서
    final_netlist_paths는 본문이 통째로 교체된 덱을 가리킨다.

    어떤 변형을 잡는가: `all_topology_swaps` 누적을 지우고
    `result["topology_swaps"]`를 그대로 두는 변형(= 이 수정 이전의 동작).
    그러면 마지막 시도의 빈 목록만 남는다."""
    run_dir = str(tmp_path / "runs" / "c_swap1")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict_fail = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])
    verdict_pass = _sweep({"gain": _wc("ff", 45.0)})

    swapped = {
        **_pass_result(run_dir),
        "topology_swaps": [{
            "outer_iter": 4, "block_path": "AMP",
            "topology_id": "miller_nulling_resistor",
            "unconstrained_refdes": 14, "stale_baseline_refdes": 2,
            "outcome": "kept",
        }],
    }
    no_swap = {**_pass_result(run_dir), "topology_swaps": []}

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([swapped, no_swap], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 2          # 재진입이 실제로 일어났다
    assert result["status"] == "PASS"
    assert result["topology_swaps"] == [{
        "attempt": 0,
        "outer_iter": 4,
        "block_path": "AMP",
        "topology_id": "miller_nulling_resistor",
        "unconstrained_refdes": 14,
        "stale_baseline_refdes": 2,
        "outcome": "kept",
    }]


@pytest.mark.asyncio
async def test_the_attempt_index_distinguishes_two_swaps_of_the_same_block(tmp_path):
    """`outer_iter`는 시도마다 1부터 다시 세고 `tried_topologies`도 시도마다
    리셋되므로, 같은 블록이 다른 시도에서 정당하게 다시 스왑될 수 있다 -
    그러면 누적 목록에 `outer_iter`가 같은 레코드가 둘 생긴다. `attempt`가
    없으면 두 줄은 구별되지 않는다(그리고 중복 기록처럼 보인다).

    어떤 변형을 잡는가: 누적할 때 `{"attempt": attempt, **swap}` 대신
    `swap`을 그대로 넣는 변형."""
    run_dir = str(tmp_path / "runs" / "c_swap2")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict_fail = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])
    verdict_pass = _sweep({"gain": _wc("ff", 45.0)})

    def _swap_result(topology_id: str) -> dict:
        return {
            **_pass_result(run_dir),
            "topology_swaps": [{
                "outer_iter": 4, "block_path": "AMP", "topology_id": topology_id,
                "unconstrained_refdes": 14, "stale_baseline_refdes": 2,
                "outcome": "kept",
            }],
        }

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence(
                  [_swap_result("miller_basic"), _swap_result("miller_nulling_resistor")],
                  orch_calls,
              )),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    swaps = result["topology_swaps"]
    assert len(swaps) == 2
    assert [s["attempt"] for s in swaps] == [0, 1]
    # 같은 블록, 같은 outer_iter - attempt만이 둘을 구별한다.
    assert [s["outer_iter"] for s in swaps] == [4, 4]
    assert [s["block_path"] for s in swaps] == ["AMP", "AMP"]


@pytest.mark.asyncio
async def test_a_failure_that_adds_no_new_corner_is_reported_as_a_path_disagreement(tmp_path):
    # 중간 루프가 코너 c에서 통과라 했는데 판정 스윕이 같은 덱의 같은 c에서
    # 실패했다면 두 경로가 다른 말을 하고 있는 것이다. 재시도하면 같은 정보로
    # 같은 결과를 낼 뿐이다. 무조건 재시도하는 변형은 예산을 다 태우므로
    # attempts 단언이 잡는다.
    run_dir = str(tmp_path / "runs" / "c4")
    entry = _sweep({"gain": _wc("fs", 41.0)})            # 씨앗 = {NOMINAL, fs}
    verdict = _sweep({"gain": _wc("fs", 12.0)}, failing=["gain"])   # 이미 집합 안이다

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["corner_reduction"]["attempts"] == 0
    assert len(orch_calls) == 1
    assert result["corner_reduction"]["path_disagreement"] is not None
    assert result["corner_reduction"]["path_disagreement"]["criteria"] == ["gain"]
    assert result["corner_reduction"]["path_disagreement"]["corners"] == ["fs/1.98/125.0"]
    assert "path disagreement" in result["failure_reason"]
    # 사유는 원래의 스윕 실패도 함께 말해야 한다 - 불일치는 그 위에 얹힌 사실이다.
    assert "one or more criteria failed" in result["failure_reason"]
    assert len(_history_events(run_dir, "corner_path_disagreement")) == 1


def _judged_snapshot_build_corner_simulate(promoted_raw: dict | None = None):
    """corner_sim.build_corner_simulate 자리에 들어가는 대역.

    실제 build_corner_simulate는 매 simulate_fn 호출마다 판정 직전 집합을
    corner_state.last_judged_corners에 스냅샷하고, 탐침이 실패하면 *그 뒤에*
    corner_set을 승격한다. 이 테스트들은 run_orchestration 자체를 대역으로
    바꾸므로 실제 simulate_fn은 한 번도 불리지 않는다 - 그래서 그 동작을
    여기서 손으로 재현한다: 호출 시점에 스냅샷을 찍고(`promoted_raw`가 있으면
    그 찍는 시점 *이후에* 승격을 흉내 낸다), corner_state.corner_set과
    last_judged_corners를 실제 코드가 만들 수 있는 모양으로 정확히 갈라 둔다.
    반환하는 simulate_fn이 불리면 이 가정이 깨진 것이므로 즉시 실패시킨다."""

    def factory(agent_simulate_fn, sim_backend, state, corner_state, log_event, max_workers=None):
        corner_state.last_judged_corners = frozenset(
            cli.label(p) for p in corner_state.corner_set.corners
        )
        if promoted_raw is not None:
            new_point = CornerPoint(
                process=promoted_raw["process"],
                voltage=promoted_raw["voltage"],
                temperature=promoted_raw["temperature"],
            )
            corner_state.corner_set = CornerSet(
                corners=(*corner_state.corner_set.corners, new_point),
                probe_order=tuple(
                    p for p in corner_state.corner_set.probe_order if p != new_point
                ),
            )

        async def simulate_fn(netlist_texts, spec):
            raise AssertionError(
                "simulate_fn should never be called - run_orchestration is mocked "
                "in this test, so reaching here means the test's assumptions broke"
            )

        return simulate_fn

    return factory


@pytest.mark.asyncio
async def test_a_corner_already_judged_when_it_failed_stays_a_path_disagreement(tmp_path):
    # (a) 갈래: 실패한 코너가 **마지막 판정 시점에도 이미** 집합 안에 있었다면
    # (탐침 승격이 끼어들지 않았다면) 이것은 진짜 경로 불일치이고, 새 진단이
    # 이 경우를 재진입으로 바꿔서는 안 된다. last_judged_corners를 명시적으로
    # 채워서 "판정한 적 없다"는 핑계가 성립하지 않는 상태를 만든다 - 그냥
    # None으로 두면(기존 test_a_failure_that_adds_no_new_corner_...와 동일)
    # 이 테스트가 실제로 무엇을 가르는지 증명하지 못한다.
    run_dir = str(tmp_path / "runs" / "c_reentry_a")
    entry = _sweep({"gain": _wc("fs", 41.0)})            # 씨앗 = {NOMINAL, fs}
    verdict = _sweep({"gain": _wc("fs", 12.0)}, failing=["gain"])   # 이미 집합 안이고 이미 판정됨

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
        patch("analogcoder.cli.build_corner_simulate",
              new=_judged_snapshot_build_corner_simulate(promoted_raw=None)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 1
    assert result["corner_reduction"]["attempts"] == 0
    assert result["corner_reduction"]["path_disagreement"] is not None
    assert result["corner_reduction"]["path_disagreement"]["criteria"] == ["gain"]
    assert len(_history_events(run_dir, "corner_path_disagreement")) == 1
    assert _history_events(run_dir, "corner_probe_promotion_reentry") == []
    assert result["status"] == "FAIL"


@pytest.mark.asyncio
async def test_a_corner_promoted_after_the_last_judgment_re_enters_instead_of_a_false_path_disagreement(
    tmp_path,
):
    # (b) 갈래: 탐침이 실패한 코너를 승격시켰지만 그 이터레이션의 판정자는
    # 승격 **이전** 집합으로 판정했다(corner_sim.py 실측 동작). 그 코너에서
    # 판정 스윕이 실패하면 corner_set 안에는 이미 있으니 grown_with는 더할
    # 것이 없다고 답하지만, 그 코너는 last_judged_corners에는 없다 - 새
    # 정보이므로 경로 불일치가 아니라 재진입해야 한다.
    run_dir = str(tmp_path / "runs" / "c_reentry_b")
    entry = _sweep({"gain": _wc("fs", 41.0)})             # 씨앗 = {NOMINAL, fs}
    promoted = _wc("ss", 12.0, voltage=1.98, temperature=125.0)
    # 판정 스윕은 승격된(그러나 아직 판정된 적 없는) ss에서 실패한다.
    verdict_fail = _sweep({"pm": promoted}, failing=["pm"])
    verdict_pass = _sweep({"pm": _wc("ss", 55.0, voltage=1.98, temperature=125.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence(
                  [_pass_result(run_dir), _pass_result(run_dir)], orch_calls
              )),
        patch("analogcoder.cli.build_corner_simulate",
              new=_judged_snapshot_build_corner_simulate(promoted_raw=promoted)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 2                       # 재진입이 실제로 일어났다
    assert result["corner_reduction"]["path_disagreement"] is None
    assert _history_events(run_dir, "corner_path_disagreement") == []
    reentry_events = _history_events(run_dir, "corner_probe_promotion_reentry")
    assert len(reentry_events) == 1
    assert reentry_events[0]["criteria"] == ["pm"]
    assert reentry_events[0]["corners"] == ["ss/1.98/125.0"]
    assert "new information" in result.get("failure_reason", "") or result["status"] == "PASS"
    assert result["status"] == "PASS"

    # M10(T19): 승격 재진입은 `grown`에 코너를 하나도 더하지 않지만, 그
    # attempt 자체는 셌으므로 불변식 len(grown) == attempts는 여전히 지켜져야
    # 한다 - grown에 빈 리스트를 밀지 않으면(구현을 되돌리면) 이 단언이
    # attempts=1, len(grown)=0으로 어긋나며 실패한다.
    corner_reduction = result["corner_reduction"]
    assert len(corner_reduction["grown"]) == corner_reduction["attempts"]
    assert corner_reduction["grown"] == [[]]
    # 그리고 두 경로(성장 vs 승격 재진입)를 구별하는 것이 이 결함의 핵심이다 -
    # `promotion_reentries`가 그 attempt·기준·코너를 담아야 한다. 이 필드
    # 자체를 지우면(또는 append를 빼먹으면) 아래 단언들이 실패한다.
    assert corner_reduction["promotion_reentries"] == [
        {"attempt": 1, "criteria": ["pm"], "corners": ["ss/1.98/125.0"]}
    ]


@pytest.mark.asyncio
async def test_the_attempt_boundary_checkpoint_written_during_a_promotion_reentry_carries_it(
    tmp_path,
):
    """I1(T19 리뷰): `_save`가 `build_checkpoint(promotion_reentries=...)`를
    실제로 넘기는지는 지금까지 무테스트였다 - 재개 테스트들은 체크포인트
    payload를 손으로 써넣거나(`grown_labels`와 같은 방식) `build_checkpoint`를
    직접 호출해서 이 배선 자체를 우회했다. C1(`test_cli_resume.py`의
    `..._preserves_the_last_judged_snapshot`)이 쓴 것과 같은 패턴 -
    `read_payload(run_dir)`로 cli가 **실제로 디스크에 쓴** 체크포인트를 읽어
    단언한다 - 을 여기서는 **중단 전** 실행에 적용한다: 승격 재진입이 첫
    시도 안에서 일어나고, 그 attempt 경계 체크포인트가 써진 직후(두 번째
    `run_orchestration` 호출에서) 죽는다.

    **반증 확인 대상**: `cli.py`의 `_save(BOUNDARY_ATTEMPT)` 호출로 가는
    `build_checkpoint(...)`에서 `promotion_reentries=promotion_reentries`
    인자를 지우면 이 단언이 실패한다."""
    run_dir = str(tmp_path / "runs" / "c_reentry_checkpoint")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    promoted = _wc("ss", 12.0, voltage=1.98, temperature=125.0)
    verdict_fail = _sweep({"pm": promoted}, failing=["pm"])

    calls: list = []

    async def first_pass_then_boom(initial_netlist_texts, spec, state, agents, **kwargs):
        calls.append(dict(initial_netlist_texts))
        if len(calls) == 1:
            state.push_netlist_version(initial_netlist_texts)
            state.push_netlist_version({name: TUNED_TEXT for name in initial_netlist_texts})
            return dict(_pass_result(run_dir))
        raise RuntimeError("승격 재진입 뒤 두 번째 시도에서 죽는다 - "
                            "attempt boundary 체크포인트만 남기려는 것이다")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail], [])),
        patch("analogcoder.cli.run_orchestration", new=first_pass_then_boom),
        patch("analogcoder.cli.build_corner_simulate",
              new=_judged_snapshot_build_corner_simulate(promoted_raw=promoted)),
    ):
        with pytest.raises(RuntimeError):
            await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(calls) == 2       # 승격 재진입이 실제로 두 번째 시도를 불렀다
    payload = read_payload(run_dir)
    assert payload["boundary"] == BOUNDARY_ATTEMPT
    assert payload["attempt"] == 1
    assert payload["promotion_reentries"] == [
        {"attempt": 1, "criteria": ["pm"], "corners": ["ss/1.98/125.0"]}
    ]


@pytest.mark.asyncio
async def test_promotion_reentries_is_an_empty_list_not_absent_when_no_promotion_happened(
    tmp_path,
):
    # 이 저장소의 반복된 규칙: "없었다"와 "이 필드가 사라졌다"는 같은 부재가
    # 아니다. 승격 재진입이 아예 없었던(순수 성장) 실행에서도
    # `promotion_reentries`는 **키로서 존재**하고 값은 빈 리스트여야 한다 -
    # `.get`으로 조건부로만 실으면 이 단언이 실패한다(키 자체가 없어지거나
    # None이 된다).
    run_dir = str(tmp_path / "runs" / "c_no_promotion")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})
    verdict_fail = _sweep({"gain": _wc("ff", 12.0), "pm": _wc("fs", 55.0)}, failing=["gain"])
    verdict_pass = _sweep({"gain": _wc("ff", 45.0), "pm": _wc("fs", 55.0)})

    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], [])),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], [])),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert "promotion_reentries" in result["corner_reduction"]
    assert result["corner_reduction"]["promotion_reentries"] == []


@pytest.mark.asyncio
async def test_reduction_is_inactive_and_says_why_without_pvt_corners(tmp_path):
    # 조용히 아무것도 안 하는 것이 이 저장소가 반복해서 당한 실패 모양이다.
    # reason을 None으로 두는 변형은 run 결과만 보고는 축소가 왜 꺼졌는지
    # 알 수 없게 만든다.
    run_dir = str(tmp_path / "runs" / "c5")

    with patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))):
        result = await _run(_corner_args(tmp_path, SPEC_YAML, run_dir))

    assert result["corner_reduction"]["active"] is False
    assert result["corner_reduction"]["reason"] is not None
    assert "pvt_corners" in result["corner_reduction"]["reason"]
    assert _one_history_event(run_dir, "corner_reduction_inactive")


@pytest.mark.asyncio
async def test_reduction_is_inactive_and_says_why_when_the_spec_disables_it(tmp_path):
    # 코너는 잴 수 있는데 스펙이 껐다. "코너가 없다"와는 다른 사실이므로
    # 사유가 달라야 한다 - 두 경우에 같은 문장을 적는 변형을 이 단언이 잡는다.
    run_dir = str(tmp_path / "runs" / "c6")
    passing = _sweep({"gain": _wc("fs", 41.0)})

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        result = await _run(
            _corner_args(tmp_path, CORNERS_BUT_REDUCTION_DISABLED_SPEC_YAML, run_dir)
        )

    assert result["corner_reduction"]["active"] is False
    assert "enabled" in result["corner_reduction"]["reason"]
    assert result["corner_reduction"]["final_set"] == []
    assert _one_history_event(run_dir, "corner_reduction_inactive")


@pytest.mark.asyncio
async def test_a_disabled_reduction_never_retries_a_failing_verdict_sweep(tmp_path):
    # 축소가 꺼졌을 때의 동작은 **정확히 오늘 그대로**여야 한다: 판정 스윕이
    # 실패하면 FAIL을 보고하고 끝이다. reduction_active를 안 보고 재진입하는
    # 변형을 orch 호출 수가 잡는다.
    run_dir = str(tmp_path / "runs" / "c7")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(
            _corner_args(tmp_path, CORNERS_BUT_REDUCTION_DISABLED_SPEC_YAML, run_dir)
        )

    assert len(orch_calls) == 1
    assert result["status"] == "FAIL"
    assert result["corner_reduction"]["attempts"] == 0


@pytest.mark.asyncio
async def test_the_same_corner_aware_simulate_goes_to_the_loop_and_the_optimizer(tmp_path):
    # 회전 탐침과 탐침 승격이 사는 상자는 하나다. 배선을 한 곳만 바꾸면
    # 최적화 탐색은 여전히 nominal만 보고, 회전도 갈라진다. 두 소비자가
    # **같은 콜러블**을 받는지 본다 - 그러면 상자도 정의상 하나다.
    run_dir = str(tmp_path / "runs" / "c8")
    passing = _sweep({"gain": _wc("fs", 41.0)})
    captured: dict = {}

    sentinel = object()

    def fake_build(agent_simulate_fn, sim_backend, state, corner_state, log_event):
        captured["corner_state"] = corner_state
        captured["agent_adapter"] = agent_simulate_fn
        return sentinel

    async def fake_optimization(netlist_texts, spec, state, agents):
        captured["optimizer_simulate"] = agents.simulate
        return _optimization_result()

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.build_corner_simulate", new=fake_build),
        patch("analogcoder.cli.run_optimization", new=fake_optimization),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration(_pass_result(run_dir), captured)),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert captured["agents"].simulate is sentinel
    assert captured["optimizer_simulate"] is sentinel
    # 상자에는 진입 스윕에서 뽑은 씨앗이 들어 있다.
    from analogcoder.corner_selection import label as corner_label

    assert [corner_label(c) for c in captured["corner_state"].corner_set.corners] == [
        "(deck)", "fs/1.98/125.0"
    ]


@pytest.mark.asyncio
async def test_the_corner_simulate_gets_a_two_argument_agent_adapter(tmp_path):
    # corner_sim은 agent_simulate를 (netlist_path, control_block) 두 인자로
    # 부른다. cli.agent_simulate를 그대로 넘기는 변형은 시뮬레이터 백엔드
    # 인자가 빠져 TypeError가 된다 - 그 크래시는 판정 경로에서 난다.
    run_dir = str(tmp_path / "runs" / "c9")
    passing = _sweep({"gain": _wc("fs", 41.0)})
    captured: dict = {}

    def fake_build(agent_simulate_fn, sim_backend, state, corner_state, log_event):
        captured["agent_adapter"] = agent_simulate_fn
        captured["sim_backend"] = sim_backend
        return AsyncMock()

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.build_corner_simulate", new=fake_build),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        await _run(
            _corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir)
        )

    with patch("analogcoder.cli.agent_simulate", new=AsyncMock(return_value={})) as mock_sim:
        await captured["agent_adapter"]("/tmp/deck.cir", ".control\n.endc\n")

    args = mock_sim.await_args.args
    assert args[0] == "/tmp/deck.cir"
    assert args[1] == ".control\n.endc\n"
    assert args[2] is captured["sim_backend"]
    assert args[3].model == "sonnet"          # simulator 에이전트의 백엔드


@pytest.mark.asyncio
async def test_the_run_records_whether_each_criterion_s_worst_corner_moved(tmp_path):
    # 이 숫자 자체가 산출물이다 - 다음에 어떤 축소 기법을 검토할지가 여기서
    # 결정된다. moved를 항상 False로 두는 변형을 이 단언이 잡는다.
    run_dir = str(tmp_path / "runs" / "c10")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("ss", 55.0)})
    verdict = _sweep({"gain": _wc("ff", 45.0), "pm": _wc("ss", 60.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    drift = result["corner_reduction"]["argmax_drift"]
    assert drift["moved_count"] == 1
    assert drift["total"] == 2
    moved = [c for c in drift["criteria"] if c["moved"]]
    assert moved[0]["entry"] == "fs/1.98/125.0" and moved[0]["final"] == "ff/1.98/125.0"
    assert _one_history_event(run_dir, "corner_argmax_drift")["moved_count"] == 1


CORNER_REDUCTION_COVERAGE_SPEC_YAML = CORNER_REDUCTION_SPEC_YAML.replace(
    "  probe: true\n",
    "  probe: true\n"
    "  coverage:\n"
    "    epsilon: 0.05\n"
    "    tau: 1.0\n",
)


@pytest.mark.asyncio
async def test_corner_seed_event_is_logged_unconditionally_in_argmax_mode(tmp_path):
    """`corner_seed`는 coverage가 선언되지 않아도 남는다 - **무조건** 남는다는
    계약을 이 테스트가 지킨다. 이 단언이 없으면 cli.py의 로그 호출을 조건부로
    바꾸거나 통째로 지워도 스위트 전체가 초록으로 남는다 - "오늘 방식으로
    골랐다"와 "기록하는 코드가 사라졌다"가 같은 침묵이 되는 바로 그 모양."""
    run_dir = str(tmp_path / "runs" / "cseed1")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep({"gain": _wc("fs", 41.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    event = _one_history_event(run_dir, "corner_seed")
    assert event["mode"] == "argmax"
    assert event["epsilon"] is None and event["tau"] is None
    assert event["dropped"] == []


@pytest.mark.asyncio
async def test_corner_seed_event_reflects_coverage_when_the_spec_declares_it(tmp_path):
    """coverage가 선언되면 `corner_seed`의 mode/epsilon/tau가 그것을 그대로
    반영한다 - argmax 테스트와 짝을 이루어 두 갈래 모두 검증한다."""
    run_dir = str(tmp_path / "runs" / "cseed2")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})
    entry["per_corner"] = [
        {"corner": {"process": "fs", "voltage": 1.98, "temperature": 125.0},
         "measurements": {"gain_db": 41.0, "phase_margin": 55.0}, "severity": 0.0},
    ]
    verdict = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_COVERAGE_SPEC_YAML, run_dir))

    event = _one_history_event(run_dir, "corner_seed")
    assert event["mode"] == "coverage"
    assert event["epsilon"] == 0.05
    assert event["tau"] == 1.0


@pytest.mark.asyncio
async def test_corner_set_seeded_carries_by_criterion_in_argmax_mode(tmp_path):
    """IMPORTANT 3: argmax 모드의 선택 집합은 `worst_case_corners`의 상(image)
    그 자체이므로 by_criterion은 참인 진술이다 - argmax 쪽은 계속 실어야 한다."""
    run_dir = str(tmp_path / "runs" / "byc1")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})
    verdict = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    event = _one_history_event(run_dir, "corner_set_seeded")
    assert event["by_criterion"] == {"gain": "fs/1.98/125.0", "pm": "fs/1.98/125.0"}


@pytest.mark.asyncio
async def test_corner_set_seeded_omits_by_criterion_in_coverage_mode(tmp_path):
    """IMPORTANT 3: coverage 모드의 선택 집합은 탐욕 피복이 고르므로
    `worst_case_corners`의 상이 아니다 - 측정된 사례에서 이 매핑이 가리키는
    코너가 선택 집합 밖에 있었다(corners=['(deck)', 'ss/1.62/125.0']인데
    by_criterion={'gain': 'fs/...'}). 거짓 구조 주장을 싣느니 키를 아예
    비운다."""
    run_dir = str(tmp_path / "runs" / "byc2")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})
    entry["per_corner"] = [
        {"corner": {"process": "fs", "voltage": 1.98, "temperature": 125.0},
         "measurements": {"gain_db": 41.0, "phase_margin": 55.0}, "severity": 0.0},
    ]
    verdict = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_COVERAGE_SPEC_YAML, run_dir))

    event = _one_history_event(run_dir, "corner_set_seeded")
    assert "by_criterion" not in event


@pytest.mark.asyncio
async def test_result_json_carries_the_seed_record_under_corner_reduction(tmp_path):
    """IMPORTANT 6: result.json/report.md만 보는 사람은 지금까지 argmax와
    ε-coverage 중 무엇이 돌았는지 알 방법이 없었다 - `corner_seed`는
    history.jsonl에만 남았다. `result["corner_reduction"]["seed"]`가 그
    간극을 메운다."""
    run_dir = str(tmp_path / "runs" / "seedresult1")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep({"gain": _wc("fs", 41.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    seed = result["corner_reduction"]["seed"]
    assert seed is not None
    assert seed["mode"] == "argmax"


@pytest.mark.asyncio
async def test_only_the_failing_criteria_s_corners_join_the_set(tmp_path):
    # 성장은 **실패한 기준들의** 최악 코너만 더한다. 통과한 기준의 최악 코너를
    # 함께 더하면 축소 집합이 실패와 무관하게 부풀어 이 하위 프로젝트의 목적이
    # 사라진다. 통과 키를 잘못 읽는 변형(예: judge_tools가 쓰는 "pass" 대신
    # "passed")은 모든 기준을 실패로 읽으므로 여기서 ss까지 딸려 들어온다.
    run_dir = str(tmp_path / "runs" / "c11")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})
    verdict_fail = _sweep(
        {"gain": _wc("ff", 12.0), "pm": _wc("ss", 55.0)}, failing=["gain"]
    )
    verdict_pass = _sweep({"gain": _wc("ff", 45.0), "pm": _wc("ss", 55.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["corner_reduction"]["grown"] == [["ff/1.98/125.0"]]
    assert result["corner_reduction"]["final_set"] == ["(deck)", "fs/1.98/125.0", "ff/1.98/125.0"]


# --- 리뷰 Important 1/2 + Minors -----------------------------------------------


def _sweep_with_an_unmeasured_criterion(name: str, others: dict | None = None) -> dict:
    """어떤 코너에서도 측정값이 안 나온 기준이 하나 있는 판정 스윕.

    pvt.worst_case_measurements는 그 기준을 worst_case_corners에서 **통째로 뺀다**
    (`if not values_with_corner: continue`). evaluate_criteria는 그것을 actual=nan,
    pass=False로 실패시키므로, 그 기준은 실패 목록에는 들어가고 최악 코너는 없다 -
    프로덕션이 실제로 내놓는 모양이다."""
    worst = dict(others or {})
    criteria = [
        {"name": n, "target": ">=10.0", "actual": raw["value"], "pass": True, "margin": 0.0}
        for n, raw in worst.items()
    ]
    criteria.append(
        {"name": name, "target": ">=10.0", "actual": float("nan"), "pass": False,
         "margin": float("nan")}
    )
    return {
        "overall_pass": False,
        "criteria": criteria,
        "summary": "one or more criteria failed",
        "worst_case_corners": worst,
        "per_corner": [],
    }


@pytest.mark.asyncio
async def test_a_failure_with_no_attributed_corner_is_not_called_a_path_disagreement(tmp_path):
    # 어떤 코너에서도 측정값이 안 나온 기준은 최악 코너가 없으므로 집합이 자라지
    # 않는다. 그것을 "경로 불일치"라고 적으면 두 실행 경로에 대해 데이터가
    # 뒷받침하지 않는 주장을 하는 것이다 - `OPAMP2STAGE drives vdd,vss`와 같은
    # 모양의 오류. 재진입하지 않는 것은 양쪽 다 옳고, 달라지는 것은 진단뿐이다.
    run_dir = str(tmp_path / "runs" / "c12")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep_with_an_unmeasured_criterion("pm")

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 1                     # 재진입하지 않는 것은 그대로 옳다
    assert result["status"] == "FAIL"
    assert result["corner_reduction"]["path_disagreement"] is None
    assert "path disagreement" not in result["failure_reason"]
    assert result["corner_reduction"]["unattributed_failures"] == {"criteria": ["pm"]}
    assert "no corner could be attributed" in result["failure_reason"]
    assert _one_history_event(run_dir, "corner_unattributed_failure")["criteria"] == ["pm"]
    assert _history_events(run_dir, "corner_path_disagreement") == []


@pytest.mark.asyncio
async def test_an_attributed_failure_alongside_an_unattributed_one_is_still_a_disagreement(
    tmp_path,
):
    # 반대 방향. 실패한 기준 중 **하나라도** 최악 코너가 붙어 있고 그것이 이미
    # 집합 안이면, 두 경로는 진짜로 서로 다른 말을 하고 있다. 위 테스트만 있으면
    # "무조건 unattributed로 적는다"는 변형이 살아남는다.
    run_dir = str(tmp_path / "runs" / "c13")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep_with_an_unmeasured_criterion("pm", {"gain": _wc("fs", 12.0)})
    verdict["criteria"][0]["pass"] = False          # gain도 실패, 최악 코너는 fs(이미 집합 안)

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["corner_reduction"]["path_disagreement"] == {
        "criteria": ["gain"], "corners": ["fs/1.98/125.0"]
    }
    assert "path disagreement" in result["failure_reason"]
    # 코너가 안 붙은 기준을 불일치 목록에 끌어들이지 않는다 - 그 기준에 대해서는
    # 두 경로가 무엇을 말했는지 알 수 없다.
    assert "pm" not in result["corner_reduction"]["path_disagreement"]["criteria"]
    assert result["corner_reduction"]["unattributed_failures"] is None


@pytest.mark.asyncio
async def test_each_re_entry_re_anchors_the_area_growth_baseline_and_says_so(tmp_path):
    # orchestrator.py:69-74는 면적 기준선을 자기가 **받은** 넷리스트에서 호출마다
    # 한 번 계산한다. 재진입은 수렴된 덱을 넘기므로 기준선이 다시 잡히고, 한
    # 소자가 원래 덱에 대해 허용받는 성장은 tier^(R+1)이 된다. 이 저장소에서
    # 게이트가 조용히 안 걸린 것이 네 번이고 네 번 다 로그에 안 보였다 - 그래서
    # 실행이 쓴 기준선 개수가 결과와 이력 양쪽에 남아야 한다.
    run_dir = str(tmp_path / "runs" / "c14")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    fails = [_sweep({"gain": _wc(p, 12.0)}, failing=["gain"]) for p in ("ff", "ss", "sf")]

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, *fails], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    # 최초 1회 + 재진입 2회 = 기준선 3개.
    assert result["corner_reduction"]["area_baselines"] == 3
    assert result["corner_reduction"]["attempts"] == 2
    assert len(orch_calls) == 3
    grown_events = _history_events(run_dir, "corner_set_grown")
    assert [e["area_baselines_so_far"] for e in grown_events] == [2, 3]
    assert all(e["area_baseline_reanchored"] is True for e in grown_events)


@pytest.mark.asyncio
async def test_a_run_that_never_re_enters_uses_exactly_one_area_baseline(tmp_path):
    # 반대 방향 고정. area_baselines를 상수 1로 박는 변형은 위 테스트가,
    # attempts와 무관하게 키우는 변형은 이 테스트가 잡는다.
    run_dir = str(tmp_path / "runs" / "c15")
    passing = _sweep({"gain": _wc("fs", 41.0)})

    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["corner_reduction"]["area_baselines"] == 1
    # **통과한 판정 스윕에서 루프를 끊는다는 것 자체를 명시적으로 못박는다.**
    # `if final_sweep["overall_pass"]: break`가 안전상 중요한 줄인데, 지금까지는
    # area_baselines가 1이라는 **간접** 단언 하나에만 걸려 있었다. 그 break가
    # 사라지면 통과한 설계를 두고 재진입이 돌기 시작한다.
    #
    # **호출 수만으로는 부족하다는 것을 실제로 확인했다.** break를 지워 보면
    # 뒤이어 실패 기준 목록이 비어 `grown_with`가 아무것도 더하지 못하고
    # "코너를 귀속할 수 없다" 갈래로 빠져 나가므로, orch 호출 수는 여전히 1이다.
    # 바뀌는 것은 **status**다 - 통과한 스윕을 두고 실행이 FAIL로 끝난다.
    # 그래서 두 단언이 함께 있어야 이 줄이 진짜로 못박힌다.
    assert len(orch_calls) == 1
    assert result["status"] == "PASS"
    assert result.get("failure_reason") is None


@pytest.mark.asyncio
async def test_reduction_is_inactive_and_says_why_without_a_corner_reduction_block(tmp_path):
    # 코너는 잴 수 있고 블록 자체가 없다. "코너가 없다"와도 "껐다"와도 다른
    # 사실이며, 셋 중 이 하나만 테스트가 없어서 같은 문장을 돌려주는 변형이
    # 살아남고 있었다.
    run_dir = str(tmp_path / "runs" / "c16")
    passing = _sweep({"gain": _wc("fs", 41.0)})
    spec_yaml = CORNER_REDUCTION_SPEC_YAML.replace(
        "corner_reduction:\n  enabled: true\n  retry_budget: 2\n  probe: true\n", ""
    )
    assert "corner_reduction" not in spec_yaml

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        result = await _run(_corner_args(tmp_path, spec_yaml, run_dir))

    assert result["corner_reduction"]["active"] is False
    assert "no corner_reduction block" in result["corner_reduction"]["reason"]
    assert _one_history_event(run_dir, "corner_reduction_inactive")


@pytest.mark.asyncio
async def test_a_seed_that_cannot_be_built_ends_the_run_as_a_clean_fail(tmp_path):
    # _as_point는 이제 (deck) 항목에 ValueError를 던진다. 그것이 _run 밖으로
    # 새어 나가면 result.json도 report.md도 없이 트레이스백으로 끝난다 -
    # 넷리스트 적용 경로의 ValueError를 깨끗한 FAIL로 접는 것과 같은 이유로
    # 여기서도 접는다.
    run_dir = str(tmp_path / "runs" / "c17")
    passing = _sweep({"gain": _wc("fs", 41.0)})

    def boom(sweep, spec):
        raise ValueError("not a corner: (deck)")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.seed_from_sweep", new=boom),
        patch("analogcoder.cli.run_orchestration", new=AsyncMock()) as mock_orch,
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    mock_orch.assert_not_awaited()
    assert result["status"] == "FAIL"
    assert "not a corner" in result["failure_reason"]
    # 리포트를 쓰는 데 필요한 키가 전부 있어야 깨끗한 FAIL이다.
    assert result["iterations_used"] == 0
    assert result["final_criteria"] == []
    assert result["final_netlist_paths"] == {}
    assert result["corner_reduction"]["active"] is False
    assert _one_history_event(run_dir, "corner_set_seed_failed")


@pytest.mark.asyncio
async def test_a_growth_that_raises_ends_the_run_as_a_clean_fail(tmp_path):
    # 같은 이유로 성장 쪽도 접는다. 이쪽은 판정 스윕이 이미 실패했으므로
    # status는 그대로 FAIL이고 사유만 덧붙는다.
    run_dir = str(tmp_path / "runs" / "c18")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])

    def boom(cs, sweep, failing_names):
        raise ValueError("not a corner: (deck)")

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.grown_with", new=boom),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 1
    assert result["status"] == "FAIL"
    assert "could not be grown" in result["failure_reason"]
    assert result["corner_reduction"]["attempts"] == 0
    assert _one_history_event(run_dir, "corner_set_growth_failed")


def test_the_empty_argmax_record_is_a_fresh_object_every_time():
    # 모듈 수준 상수를 얕게 복사해 돌려주면 result에 실리는 "criteria" 리스트가
    # 그 상수와 **같은 객체**가 되어, 거기에 append하는 소비자 하나가 프로세스
    # 전역을 오염시킨다.
    first = cli._no_drift()
    first["criteria"].append({"name": "x"})
    assert cli._no_drift() == {
        "criteria": [],
        "moved_count": 0,
        "total": 0,
        "compared_count": 0,
        "unmeasured_count": 0,
        "measurability_changed_count": 0,
        "unpaired_count": 0,
    }


def test_a_coordinate_less_entry_is_labelled_as_the_deck():
    # corner_selection._as_point의 거부 조건과 **같은 방향**이어야 한다: 좌표가
    # 둘 중 하나라도 없으면 그것은 코너가 아니다. 한쪽만 `and`로 두면 반쪽짜리
    # 항목에서 저쪽은 거부하고 이쪽은 "ss/1.62/None"이라는 있지도 않은 코너
    # 이름을 적는다.
    assert cli.raw_label(None) is None
    assert cli.raw_label(
        {"process": "(deck)", "voltage": None, "temperature": None, "value": 1.2}
    ) == "(deck)"
    assert cli.raw_label(
        {"process": "ss", "voltage": 1.62, "temperature": None, "value": 1.2}
    ) == "(deck)"
    assert cli.raw_label(
        {"process": "ss", "voltage": 1.62, "temperature": -40.0, "value": 1.2}
    ) == "ss/1.62/-40.0"


# --- 최종 브랜치 리뷰: 재진입 게이트와 시도별 결과 기록 -------------------------


def _fail_result(run_dir: str, reason: str) -> dict:
    """수렴하지 **못하고** 끝난 오케스트레이션. 재진입 테스트가 지금까지 전부
    _pass_result만 먹여 왔기 때문에 이 모양은 어느 테스트도 지나간 적이 없다."""
    return {**_pass_result(run_dir), "status": "FAIL", "failure_reason": reason}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "max iterations reached",
        "tuning proposal repeatedly rejected",
        "agent execution error: rate limited",
    ],
)
async def test_a_tuning_loop_that_did_not_converge_is_never_re_entered(tmp_path, reason):
    # 설계가 말하는 재진입의 전제는 "수렴된 덱"이다. 10 반복을 소진한 덱도,
    # 일부러 토폴로지 교체까지 건너뛰며 하드 FAIL로 만든 "제안이 반복 거부됨"도,
    # 거의 확실히 재발하는 에이전트 실행 오류도 그것이 아니다. 게이트가 없으면
    # 셋 다 예산이 새로 채워진 완전한 튜닝 루프를 최대 retry_budget번 더 돌린다.
    #
    # **어떤 변형을 잡는가.** `if orch_status != "PASS": break`를 지우는 변형
    # (= 이 수정 이전의 코드)을 잡는다. 그러면 orch 호출이 1이 아니라 3이 되고
    # attempts가 2가 된다.
    run_dir = str(tmp_path / "runs" / "c20")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    fails = [_sweep({"gain": _wc(p, 12.0)}, failing=["gain"]) for p in ("ff", "ss", "sf")]

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, *fails], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_fail_result(run_dir, reason)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 1
    assert result["corner_reduction"]["attempts"] == 0
    assert result["status"] == "FAIL"
    # 조용히 break하지 않는다 - attempts==0만으로는 "더할 코너가 없었다"와
    # 구별되지 않는다.
    skipped = result["corner_reduction"]["reentry_skipped"]
    assert skipped["orchestration_status"] == "FAIL"
    assert skipped["orchestration_failure_reason"] == reason
    assert _one_history_event(run_dir, "corner_reentry_skipped")["attempt"] == 0
    assert "never converged" in result["failure_reason"]
    assert reason in result["failure_reason"]


@pytest.mark.asyncio
async def test_a_converged_loop_still_re_enters(tmp_path):
    # 반대 방향 고정. 게이트를 `!= "PASS"`가 아니라 무조건 break로 두는 변형은
    # 재진입을 통째로 죽이는데, 위 테스트만 있으면 그것이 살아남는다.
    run_dir = str(tmp_path / "runs" / "c21")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict_fail = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])
    verdict_pass = _sweep({"gain": _wc("ff", 45.0)})

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict_fail, verdict_pass], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert len(orch_calls) == 2
    assert result["corner_reduction"]["reentry_skipped"] is None


@pytest.mark.asyncio
async def test_every_orchestration_attempt_is_logged_with_its_own_outcome(tmp_path):
    # cli는 result["failure_reason"]을 스윕 사유로 덮고, _final_result는
    # history.jsonl에 아무것도 쓰지 않는다. 그래서 재진입한 실행에서 앞선 시도의
    # status/iterations_used/failure_reason은 **어디에도** 남지 않았다.
    #
    # **어떤 변형을 잡는가.** log_event("orchestration_attempt", ...) 호출을
    # 지우는 변형(= 이 수정 이전)을 잡는다.
    run_dir = str(tmp_path / "runs" / "c22")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    fails = [_sweep({"gain": _wc(p, 12.0)}, failing=["gain"]) for p in ("ff", "ss", "sf")]

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, *fails], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    attempts = _history_events(run_dir, "orchestration_attempt")
    assert [e["attempt"] for e in attempts] == [0, 1, 2]
    assert all(e["status"] == "PASS" for e in attempts)
    assert all(e["iterations_used"] == 1 for e in attempts)
    assert result["corner_reduction"]["attempts"] == 2


@pytest.mark.asyncio
async def test_the_sweep_reason_is_appended_to_the_loop_s_own_reason(tmp_path):
    # 덮어쓰기는 이 브랜치보다 오래됐지만, 이 브랜치 전에는 사유 하나를 버렸고
    # 지금은 최대 세 개를 버린다. 오케스트레이션이 보고한 사유와 스윕 사유는
    # 서로 다른 사실이고 둘 다 필요하다.
    #
    # 축소가 **꺼진** 스펙으로 잰다. 켜져 있으면 재진입 건너뛰기 문장이
    # 오케스트레이션 사유를 한 번 더 싣게 되어, 덮어쓰기 변형이 그 문장에
    # 가려 살아남는다(실제로 그렇게 확인했다).
    run_dir = str(tmp_path / "runs" / "c23")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep({"gain": _wc("ff", 12.0)}, failing=["gain"])

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence(
                  [_fail_result(run_dir, "max iterations reached")], orch_calls)),
    ):
        result = await _run(
            _corner_args(tmp_path, CORNERS_BUT_REDUCTION_DISABLED_SPEC_YAML, run_dir)
        )

    assert "max iterations reached" in result["failure_reason"]
    assert "final PVT sweep failed" in result["failure_reason"]


@pytest.mark.asyncio
async def test_a_passing_loop_reports_only_the_sweep_reason(tmp_path):
    # 오케스트레이션이 사유를 남기지 않았을 때 앞에 "None; "이 붙으면 안 된다.
    run_dir = str(tmp_path / "runs" / "c24")
    entry = _sweep({"gain": _wc("fs", 41.0)})
    verdict = _sweep({"gain": _wc("fs", 12.0)}, failing=["gain"])   # fs는 이미 집합 안

    sweep_calls: list = []
    orch_calls: list = []
    with (
        patch("analogcoder.cli.run_full_pvt_sweep",
              new=_sweep_sequence([entry, verdict], sweep_calls)),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration_sequence([_pass_result(run_dir)], orch_calls)),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["failure_reason"].startswith("final PVT sweep failed:")


@pytest.mark.asyncio
async def test_the_probe_rotation_is_frozen_while_the_optimizer_searches(tmp_path):
    """최적화가 도는 동안 상자는 얼어 있고, 끝나면 다시 녹는다.

    상자를 공유하는 것은 의도다(선택 집합이 갈라지면 탐색이 메인 루프가 배운
    코너를 못 본 채 여유분을 요구한다). 얼리는 것은 **회전뿐**이다: 탐색 도중의
    승격은 서로 다른 코너 집합에서 잰 목적값을 비교하게 만들고, 그 뒤의 모든
    단계를 원인이 아닌 knob을 지목하는 사유로 거부시킨다.

    **어떤 변형을 잡는가.** cli가 `probe_frozen`을 세우지 않는 변형(관측값이
    False가 된다)과, `finally`로 되돌리지 않는 변형(재진입한 메인 루프가 탐침을
    영영 잃는다).
    """
    run_dir = str(tmp_path / "runs" / "c25")
    passing = _sweep({"gain": _wc("fs", 41.0)})
    captured: dict = {}
    observed: list = []

    def fake_build(agent_simulate_fn, sim_backend, state, corner_state, log_event):
        captured["corner_state"] = corner_state
        return AsyncMock()

    async def fake_optimization(netlist_texts, spec, state, agents):
        observed.append(captured["corner_state"].probe_frozen)
        return _optimization_result()

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.build_corner_simulate", new=fake_build),
        patch("analogcoder.cli.run_optimization", new=fake_optimization),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert observed == [True]
    assert captured["corner_state"].probe_frozen is False


@pytest.mark.asyncio
async def test_the_box_is_unfrozen_even_when_the_optimizer_raises(tmp_path):
    # run_optimization은 자기 예외를 삼키지만 이 배선이 그것에 기대면 안 된다 -
    # try/finally를 그냥 try로 바꾸는 변형을 이 단언이 잡는다.
    run_dir = str(tmp_path / "runs" / "c26")
    passing = _sweep({"gain": _wc("fs", 41.0)})
    captured: dict = {}

    def fake_build(agent_simulate_fn, sim_backend, state, corner_state, log_event):
        captured["corner_state"] = corner_state
        return AsyncMock()

    async def exploding_optimization(netlist_texts, spec, state, agents):
        raise RuntimeError("boom")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", return_value=passing),
        patch("analogcoder.cli.build_corner_simulate", new=fake_build),
        patch("analogcoder.cli.run_optimization", new=exploding_optimization),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        with pytest.raises(RuntimeError):
            await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert captured["corner_state"].probe_frozen is False


# --- 이월 불변식 I3 (키 존재 계약): 코너 시드 실패의 이른 반환 ---------------


@pytest.mark.asyncio
async def test_the_corner_seed_failure_early_return_still_carries_topology_swaps(tmp_path):
    """어느 종료 갈래로 끝나든 result 는 같은 필수 키 집합을 갖는다.

    근거는 `_final_result` 의 독스트링(`orchestrator.py`)이다: *"키를 조건부로
    넣지 않는 이유도 같다: '스왑이 없었다'와 '기록이 사라졌다'가 같은 부재로
    보이면 안 된다."* `cli.py` 의 코너 시드 실패 이른 반환은 그 계약을 어기고
    `topology_swaps` 만 빠뜨렸다 — `report.py` 는 `if not swaps: return []` 라
    report.md 가 무사해서 사람 눈에는 안 보이고, `result.json` 을 기계로 읽는
    소비자에게만 "스왑 0건"과 "이 실행은 스왑 기록을 아예 안 쓴다"가 같은
    부재가 된다.

    **이 갈래는 오늘 실행으로는 도달하지 않는다.** 그 자리의 주석이 그렇게
    적고 있다: `_as_point` 가 거부하는 `(deck)` 항목을 `run_full_pvt_sweep` 은
    만들지 않는다. 그래서 이 상태는 테스트가 직접 구성해야 하고
    (`seed_from_sweep` 을 `ValueError` 로 대체), 그것이 이 테스트가 존재하는
    이유다 — 도달 불가한 갈래의 계약 위반은 실행이 아니라 테스트에서만
    발화한다. 도달하게 되는 날 이 단언이 먼저 서 있다.
    """
    run_dir = str(tmp_path / "runs" / "seedfail")
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("fs", 55.0)})

    def exploding_seed(sweep, spec):
        raise ValueError("no usable corner coordinates in the entry sweep")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=_sweep_sequence([entry], [])),
        patch("analogcoder.cli.seed_from_sweep", new=exploding_seed),
        patch("analogcoder.cli.run_orchestration",
              new=_orchestration(_pass_result(run_dir))),
    ):
        result = await _run(_corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir))

    assert result["status"] == "FAIL"
    assert _one_history_event(run_dir, "corner_set_seed_failed")
    assert result["topology_swaps"] == []
    # 계약 전체를 여기서 한 번 확인한다. 필수 키는 `before` 에서 파생하지 않고
    # 리터럴로 적는다 - 양쪽에서 동시에 사라진 키는 "일관됨"으로 통과하기 때문.
    assert {
        "status",
        "final_netlist_paths",
        "run_dir",
        "iterations_used",
        "final_criteria",
        "topology_swaps",
        "resumed_from",
        "corner_reduction",
    } <= set(result)
    # M10(T19): 같은 계약이 corner_reduction 안의 promotion_reentries에도
    # 걸린다 - 이 갈래는 재진입 루프를 시작조차 못 했으므로 승격 재진입도
    # 0건이고, 키 부재가 아니라 빈 리스트로 말해야 한다.
    assert result["corner_reduction"]["promotion_reentries"] == []


# --- 감사 §2.2: argmax 이동은 argmax끼리만 비교한다 --------------------------


# `runs/pvt_sonnet_1` (2026-07-26, two_stage_opamp, 45 코너)의 진입/판정 스윕에서
# `worst_case_corners`를 **그대로 옮겨 온** 값이다. `runs/`는 .gitignore에 있어
# 그 실행이 사라져도 이 테스트가 서 있어야 하므로 여기 박아 둔다. 이 쌍의 성질:
# 일곱 기준의 **측정값이 양쪽에서 전부 동일**하고(3.13783 / None / None /
# 26.2352 / 9.53353 / None / None), 코너 이름이 다른 두 건은 **양쪽 다 값이
# None**이다 - 즉 argmax가 아니라 `worst_case_measurements`가 적은
# `missing_corners[0]`, "그 측정이 처음으로 안 나온 코너"다(`pvt.py:326`).
PVT_SONNET_1_ENTRY_WC = {
    "dc_gain": {"process": "fs", "voltage": 1.98, "temperature": 125.0, "value": 3.13783},
    "unity_gain_bandwidth": {"process": "fs", "voltage": 1.98, "temperature": -40.0, "value": None},
    "phase_margin": {"process": "fs", "voltage": 1.98, "temperature": -40.0, "value": None},
    "psr_plus": {"process": "sf", "voltage": 1.62, "temperature": -40.0, "value": 26.2352},
    "psr_minus": {"process": "sf", "voltage": 1.8, "temperature": -40.0, "value": 9.53353},
    "settling_time_hi": {"process": "tt", "voltage": 1.98, "temperature": 125.0, "value": None},
    "settling_time_lo": {"process": "tt", "voltage": 1.98, "temperature": 125.0, "value": None},
}

PVT_SONNET_1_FINAL_WC = {
    "dc_gain": {"process": "fs", "voltage": 1.98, "temperature": 125.0, "value": 3.13783},
    "unity_gain_bandwidth": {"process": "ss", "voltage": 1.62, "temperature": 125.0, "value": None},
    "phase_margin": {"process": "ss", "voltage": 1.62, "temperature": 125.0, "value": None},
    "psr_plus": {"process": "sf", "voltage": 1.62, "temperature": -40.0, "value": 26.2352},
    "psr_minus": {"process": "sf", "voltage": 1.8, "temperature": -40.0, "value": 9.53353},
    "settling_time_hi": {"process": "tt", "voltage": 1.98, "temperature": 125.0, "value": None},
    "settling_time_lo": {"process": "tt", "voltage": 1.98, "temperature": 125.0, "value": None},
}


def _wc_sweep(worst_corners: dict) -> dict:
    """`_argmax_drift`가 읽는 것은 `worst_case_corners` 하나뿐이다."""
    return {"worst_case_corners": worst_corners}


def test_a_corner_named_only_because_a_measurement_was_missing_is_not_a_moved_argmax():
    """측정값이 하나도 안 변한 스윕 쌍에서 "2/7이 움직였다"가 나오면 안 된다.

    이 숫자의 유일한 용도는 **코너 지속성**이다 - 설계가 움직일 때 최악 코너가
    얼마나 따라 움직이는가. `value is None` 항목은 argmax가 아니라 "그 측정이
    처음으로 안 나온 코너"이고, 그 "처음"은 코너 **선언 순서**가 정한다. 그것을
    이동으로 세면 이 지표는 회로가 아니라 스펙의 리스트 순서를 재게 된다.

    "이 지표가 다른 답을 낼 조건이 내가 잰 런에 있었는가" - D1을 무효로 만든
    질문이 여기서는 지표 **정의** 안에서 깨져 있었다.

    **수정 전 실측**: `moved_count = 2`, 두 건 전부 양쪽 값 None.
    """
    drift = _argmax_drift(_wc_sweep(PVT_SONNET_1_ENTRY_WC), _wc_sweep(PVT_SONNET_1_FINAL_WC))

    assert drift["moved_count"] == 0
    assert drift["total"] == 7
    # moved_count의 분모는 total이 아니라 compared_count다. 둘을 함께 실어야
    # "0/7"이 "일곱 개를 비교했더니 안 움직였다"로 잘못 읽히지 않는다.
    assert drift["compared_count"] == 3
    assert drift["unmeasured_count"] == 4
    assert drift["measurability_changed_count"] == 0
    assert drift["unpaired_count"] == 0

    status = {c["name"]: c["status"] for c in drift["criteria"]}
    assert status["dc_gain"] == "compared"
    assert status["unity_gain_bandwidth"] == "unmeasured"
    assert status["phase_margin"] == "unmeasured"
    # 라벨 자체는 그대로 남긴다 - 그 코너가 "처음으로 값이 안 나온 코너"라는 것도
    # 사실이다. 지우는 것이 아니라 **다른 칸에 세는** 것이 이 수정이다.
    unmeasured = [c for c in drift["criteria"] if c["status"] == "unmeasured"]
    assert {c["entry"] for c in unmeasured} == {"fs/1.98/-40.0", "tt/1.98/125.0"}


def test_reordering_the_process_axis_cannot_change_the_argmax_verdict():
    """측정값을 하나도 바꾸지 않는 스펙 편집이 판정을 뒤집으면 안 된다.

    `pvt.py`의 `missing_corners[0]`는 `all_corners(spec.pvt_corners)`의 순서를
    그대로 따르므로, `process: [tt, ss, ff]`를 `[ff, ss, tt]`로 적기만 해도
    값 없는 항목이 가리키는 코너가 바뀐다. 아래 두 쌍은 **측정된 값이 전부
    동일**하고 값 없는 항목이 가리키는 코너만 다르다.

    **수정 전 실측**: 첫 쌍 moved_count=2, 둘째 쌍 moved_count=0 - 같은 회로,
    같은 측정, 다른 답.
    """
    # 재정렬된 스펙: 값 없는 네 항목이 양쪽 스윕에서 같은 "첫 코너"를 가리키게 된다.
    reordered_entry = {
        name: (
            raw if raw["value"] is not None
            else {**raw, "process": "tt", "voltage": 1.62, "temperature": -40.0}
        )
        for name, raw in PVT_SONNET_1_ENTRY_WC.items()
    }
    reordered_final = {
        name: (
            raw if raw["value"] is not None
            else {**raw, "process": "tt", "voltage": 1.62, "temperature": -40.0}
        )
        for name, raw in PVT_SONNET_1_FINAL_WC.items()
    }

    as_shipped = _argmax_drift(
        _wc_sweep(PVT_SONNET_1_ENTRY_WC), _wc_sweep(PVT_SONNET_1_FINAL_WC)
    )
    reordered = _argmax_drift(_wc_sweep(reordered_entry), _wc_sweep(reordered_final))

    for key in ("moved_count", "total", "compared_count", "unmeasured_count"):
        assert as_shipped[key] == reordered[key], key
    assert as_shipped["moved_count"] == 0


def test_a_criterion_that_gained_a_measurement_is_not_counted_as_a_moved_argmax():
    """한쪽만 값이 있는 것은 **이동이 아니라 측정 가능성의 변화**다.

    진입 스윕에서 어느 코너에도 값이 안 나오던 기준이 판정 스윕에서는 나왔다면,
    두 코너 이름은 애초에 같은 종류의 것이 아니다(하나는 "처음으로 빠진 코너",
    다른 하나는 진짜 argmax). 그것을 이동으로 세면 지표가 회로의 코너 지속성이
    아니라 **수렴 여부**를 재게 되고, 그 둘은 다른 사실이다.

    **수정 전 실측**: moved_count=1.
    """
    entry = _wc_sweep({"gain": {"process": "fs", "voltage": 1.98, "temperature": -40.0, "value": None}})
    final = _wc_sweep({"gain": _wc("ss", 41.0)})

    drift = _argmax_drift(entry, final)

    assert drift["moved_count"] == 0
    assert drift["measurability_changed_count"] == 1
    assert drift["compared_count"] == 0
    assert [c["status"] for c in drift["criteria"]] == ["measurability_changed"]
    # 반대 방향(값이 있다가 사라짐)도 같은 칸이다.
    back = _argmax_drift(final, entry)
    assert back["moved_count"] == 0
    assert back["measurability_changed_count"] == 1


def test_a_criterion_present_in_only_one_sweep_is_recorded_as_unpaired():
    """짝이 없으면 "움직였다"고 말할 근거가 없다 - 이미 moved=False였고, 이제
    그 사실이 **이름을 갖는다**. 값 없는 항목(unmeasured)과 뭉뚱그리면 "이 스윕에
    그 기준이 아예 없었다"와 "있었는데 어느 코너에서도 값이 안 나왔다"가 같은
    숫자가 된다."""
    entry = _wc_sweep({"gain": _wc("fs", 41.0)})
    final = _wc_sweep({"gain": _wc("fs", 41.0), "pm": _wc("ss", 55.0)})

    drift = _argmax_drift(entry, final)

    assert drift["total"] == 2
    assert drift["unpaired_count"] == 1
    assert drift["compared_count"] == 1
    assert drift["moved_count"] == 0
    status = {c["name"]: c["status"] for c in drift["criteria"]}
    assert status["pm"] == "unpaired"


def test_the_empty_argmax_record_has_the_same_shape_as_a_measured_one():
    """빈 기록과 측정된 기록의 **키가 같아야** 한다. 코너를 못 재는 스펙의
    결과에서만 `unmeasured_count`가 사라지면, 그 키를 읽는 소비자가 하필
    "아무것도 안 잰 실행"에서 KeyError로 죽는다."""
    measured = _argmax_drift(_wc_sweep(PVT_SONNET_1_ENTRY_WC), _wc_sweep(PVT_SONNET_1_FINAL_WC))
    assert set(cli._no_drift()) == set(measured)


# --- 감사 §2.6: 진입·판정 PVT 스윕이 가드 밖에 있었다 ------------------------


def _main_with(argv: list[str]):
    """`main()`을 실제로 돌린다. 산출물이 써지는지가 이 절의 요점이므로
    `_run`만 부르는 것으로는 부족하다 - `write_result_json`/`write_report_md`는
    `main()`에 있고, 가드 밖에서 터진 예외는 정확히 그 두 줄을 건너뛴다."""
    with patch.object(sys, "argv", ["analogcoder", *argv]):
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    return exit_info.value.code


def _artifacts(run_dir: str) -> tuple[dict, str]:
    with open(os.path.join(run_dir, "result.json")) as f:
        result = json.load(f)
    with open(os.path.join(run_dir, "report.md")) as f:
        report = f.read()
    return result, report


BAD_OPERATOR_SPEC_YAML = CORNER_REDUCTION_SPEC_YAML.replace('operator: ">="', 'operator: "=~"', 1)


def test_an_unknown_operator_is_refused_at_spec_load_before_anything_simulates(tmp_path):
    """**이 트리거는 이제 경계에서 닫힌다.** 예전 이 테스트는 반대를 못박고
    있었다: `_load_criteria`가 operator를 검증 없이 싣고
    `judge_tools._OPERATORS[c.operator]`가 그것을 색인해, 오타 하나가 45코너
    스윕 **한가운데서** `KeyError`가 된다는 사실을.

    `spec.ALLOWED_OPERATORS`가 그 자리를 옮겼다 - 이제 스펙 로드가
    `ValueError`로 거절하므로 시뮬레이션은 한 번도 돌지 않는다. `load_spec`이
    던지는 다른 거절들(빈 축, 중복 코너, 양쪽 모양 동시 선언)과 같은
    범주다: 시작하지 않은 실행은 산출물을 남기지 않는다.

    `==`도 같은 문에서 걸리며 그 이유는 오타와 다르다 - 세 소비자가 서로
    다른 뜻으로 구현했기 때문이다. `tests/unit/test_spec.py`가 그쪽을
    본다."""
    from analogcoder.spec import load_spec

    (tmp_path / "netlist.cir").write_text(ORIGINAL_TEXT)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(BAD_OPERATOR_SPEC_YAML)

    with pytest.raises(ValueError, match="operator"):
        load_spec(str(spec_path))


def test_an_entry_sweep_that_cannot_run_still_writes_the_run_s_artifacts(tmp_path):
    """진입 스윕이 터져도 `result.json`과 `report.md`가 써져야 한다.

    여기서 쓰는 대역은 예외를 손으로 던지지 않는다 - 프로덕션
    `run_full_pvt_sweep`이 코너마다 하는 일(덱을 그 코너로 렌더링하는 것)을
    그대로 하고, 다시 쓸 수 없는 공급 라인(괄호 없는 bare PWL)이
    `CornerRenderError`를 만든다. 즉 이 테스트가 재현하는 것은 "예외가
    나면"이 아니라 **"덱이 이 코드가 다룰 수 있는 모양이 아니면"**이다.

    **트리거가 바뀌었다.** 원래 이 대역은 스펙의 `operator: "=~"` 오타가
    `judge_tools._OPERATORS`에서 내는 `KeyError`를 썼다. 그 채널은
    `spec.ALLOWED_OPERATORS`가 닫았다 - 이제 그런 스펙은 로드되지 않으므로
    스윕까지 오지 못한다(`test_an_unknown_operator_is_refused_at_spec_load_
    before_anything_simulates`). 이 테스트가 지키는 사실은 그 트리거가
    아니라 **산출물 보장**이므로, 여전히 프로덕션 경로에 실재하는 다른
    예외로 옮겼다. `cli.py`가 `Exception`을 통째로 잡는 근거도 그대로다:
    예외 **종류를 열거하는 것 자체가 추측**이고, 실제로 그 목록에서 한
    종류가 이렇게 사라진다.

    **수정 전 실측**: 예외가 `_run` -> `asyncio.run` -> `main()`을 그대로
    뚫고, run-dir에 `history.jsonl`만 남는다.
    """
    from analogcoder.pvt import render_corner_report

    run_dir = str(tmp_path / "runs" / "entryboom")
    args = _corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir)
    # 괄호 없는 bare PWL - ngspice는 받지만 렌더러는 추측하지 않고 거절한다.
    (tmp_path / "netlist.cir").write_text(
        "* ac netlist\nVdd vdd 0 PWL 0 0 1u 1.8\n.end\n"
    )

    def production_shaped_sweep(netlist_texts, spec, sim_backend, log_event=None):
        for text in netlist_texts.values():
            render_corner_report(text, "ss", 1.62, -40.0, str(tmp_path))
        raise AssertionError("unreachable: the unrenderable supply must raise first")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=production_shaped_sweep),
        patch("analogcoder.cli.run_orchestration", new=AsyncMock()) as mock_orch,
    ):
        code = _main_with(["--spec", args.spec, "--run-dir", run_dir])

    assert code == 1
    mock_orch.assert_not_awaited()   # 튜닝 루프는 시작조차 하지 않았다

    result, report = _artifacts(run_dir)
    assert result["status"] == "FAIL"
    assert "entry PVT sweep could not run" in result["failure_reason"]
    assert "CornerRenderError" in result["failure_reason"]
    # '스윕이 돌지 못했다'와 '스윕이 돌았고 실패했다'는 다른 사실이다.
    assert result["pvt_sweep"] is None
    assert result["pvt_sweep_error"]["phase"] == "entry"
    assert "## PVT sweep" not in report
    assert "entry PVT sweep could not run" in report
    assert _one_history_event(run_dir, "pvt_sweep_failed")["phase"] == "entry"


def test_a_verdict_sweep_that_cannot_run_still_writes_the_run_s_artifacts(tmp_path):
    """튜닝 루프가 PASS한 뒤 판정 스윕이 터져도 산출물이 남아야 한다.

    이것이 비싼 쪽이다 - two_stage_opamp 기준 103분짜리 루프가 끝난 뒤,
    45코너 스윕의 워커 하나가 `OSError`(ENOSPC/EAGAIN)를 만나면 run-dir에
    `history.jsonl`과 `netlist_v*.cir`만 남았다.

    상태는 **FAIL**이다. 판정을 내리는 것이 이 스윕이므로, 돌지 못한 스윕을
    통과로 읽는 것은 아무도 확인하지 않은 넷리스트를 PASS로 출하하는 것이다.
    """
    run_dir = str(tmp_path / "runs" / "verdictboom")
    args = _corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, run_dir)
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("ss", 55.0)})
    calls: list = []

    def entry_then_boom(netlist_texts, spec, sim_backend, log_event=None):
        calls.append(dict(netlist_texts))
        if len(calls) == 1:
            return entry
        raise OSError(28, "No space left on device")

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=entry_then_boom),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        code = _main_with(["--spec", args.spec, "--run-dir", run_dir])

    assert code == 1
    assert len(calls) == 2

    result, report = _artifacts(run_dir)
    assert result["status"] == "FAIL"
    assert "final PVT sweep could not run" in result["failure_reason"]
    assert "OSError" in result["failure_reason"]
    assert result["pvt_sweep"] is None
    assert result["pvt_sweep_error"]["phase"] == "verdict"
    assert "## PVT sweep" not in report
    # 재진입도 하지 않는다: 같은 정보로 다시 도는 것이 아니라 스윕 자체가 돌지
    # 못했으므로, 코너 집합에 더할 것이 없다.
    assert result["corner_reduction"]["attempts"] == 0
    # 판정 스윕이 없으므로 argmax 기록도 비어 있어야 한다 - 없는 비교를
    # 0/0이 아닌 다른 무엇으로 적으면 그것이 곧 지어낸 값이다.
    assert result["corner_reduction"]["argmax_drift"] == cli._no_drift()
    assert _one_history_event(run_dir, "pvt_sweep_failed")["phase"] == "verdict"


def test_both_sweep_guards_hold_for_an_exception_outside_oserror_and_valueerror(tmp_path):
    """**이 테스트의 트리거는 일부러 인위적이다 - 그것이 이 테스트의 일이다.**

    "현실성" 쪽은 위의 두 테스트가 맡는다: 진입 쪽은 프로덕션 렌더러가 내는
    `CornerRenderError`(= `ValueError`), 판정 쪽은 워커의 `OSError`다. 그런데
    그 둘만 있으면 `cli.py`의 두 `except Exception`을
    `except (OSError, ValueError)`로 **좁혀도 전부 초록으로 남는다** - 즉
    "가드가 저 두 종류보다 넓어야 한다"는 사실을 아무것도 못박고 있지 않다.
    그 사실이 곧 `# noqa: BLE001` 옆 20줄 주석의 주장이다: 예외 **종류를
    열거하는 것 자체가 추측**이고, 실제로 그 목록에서 `KeyError` 채널 하나가
    `spec.ALLOWED_OPERATORS` 때문에 사라졌다.

    **그러니 이 테스트를 "억지스럽다"는 이유로 고치지 말 것.** 여기서 던지는
    `KeyError`는 오늘의 프로덕션 채널을 재현하는 것이 아니라, 내일 그 자리에
    도달할 **아직 이름이 없는 결정론적 실패**(스윕 소비자에서 나는
    `KeyError`/`TypeError`/`AttributeError`)를 대신한다. 좁히면 그 예외가
    `_run` -> `asyncio.run` -> `main()`을 뚫고, two_stage_opamp 기준 103분짜리
    실행이 `history.jsonl`만 남기고 끝난다.

    두 가드(`cli.py`의 진입 쪽과 판정 쪽)를 **각각** 건드린다 - 한쪽만 재면
    다른 한쪽이 같은 방식으로 좁혀질 수 있다.
    """

    def _boom(*_args, **_kwargs):
        # (OSError, ValueError) 어느 쪽도 아니고 상속 관계도 없다.
        raise KeyError("a consumer of the sweep result went looking for a name")

    # --- 진입 스윕 ---
    entry_dir = str(tmp_path / "runs" / "entrykey")
    entry_args = _corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, entry_dir)
    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=_boom),
        patch("analogcoder.cli.run_orchestration", new=AsyncMock()) as mock_orch,
    ):
        code = _main_with(["--spec", entry_args.spec, "--run-dir", entry_dir])

    assert code == 1
    mock_orch.assert_not_awaited()
    result, report = _artifacts(entry_dir)
    assert result["status"] == "FAIL"
    assert "entry PVT sweep could not run" in result["failure_reason"]
    assert "KeyError" in result["failure_reason"]   # 종류가 그대로 실린다
    assert result["pvt_sweep"] is None
    assert result["pvt_sweep_error"]["phase"] == "entry"
    assert "entry PVT sweep could not run" in report
    assert _one_history_event(entry_dir, "pvt_sweep_failed")["phase"] == "entry"

    # --- 판정 스윕 ---
    verdict_dir = str(tmp_path / "runs" / "verdictkey")
    verdict_args = _corner_args(tmp_path, CORNER_REDUCTION_SPEC_YAML, verdict_dir)
    entry_sweep = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("ss", 55.0)})
    calls: list = []

    def entry_then_key_error(netlist_texts, spec, sim_backend, log_event=None):
        calls.append(dict(netlist_texts))
        if len(calls) == 1:
            return entry_sweep
        _boom()

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=entry_then_key_error),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(verdict_dir))),
    ):
        code = _main_with(["--spec", verdict_args.spec, "--run-dir", verdict_dir])

    assert code == 1
    assert len(calls) == 2
    result, report = _artifacts(verdict_dir)
    assert result["status"] == "FAIL"
    assert "final PVT sweep could not run" in result["failure_reason"]
    assert "KeyError" in result["failure_reason"]
    assert result["pvt_sweep"] is None
    assert result["pvt_sweep_error"]["phase"] == "verdict"
    assert _one_history_event(verdict_dir, "pvt_sweep_failed")["phase"] == "verdict"


def test_a_sweep_that_ran_and_failed_is_not_reported_as_one_that_could_not_run(tmp_path):
    """대조군. 같은 FAIL이지만 **다른 사실**이고, 산출물에서 구별되어야 한다.

    돌아서 실패한 스윕은 `pvt_sweep`을 싣고 리포트에 PVT 섹션을 그린다(어느
    코너가 깨졌는지가 그 스윕이 만든 유일한 추가 정보다). 돌지 못한 스윕은
    실을 것이 없고, 그 사실이 `pvt_sweep_error`와 사유 문구로 남는다."""
    run_dir = str(tmp_path / "runs" / "verdictfail")
    args = _corner_args(tmp_path, CORNERS_BUT_REDUCTION_DISABLED_SPEC_YAML, run_dir)
    entry = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("ss", 55.0)})
    verdict = _sweep({"gain": _wc("ff", 4.0), "pm": _wc("ss", 55.0)}, failing=["gain"])

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=_sweep_sequence([entry, verdict], [])),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        code = _main_with(["--spec", args.spec, "--run-dir", run_dir])

    assert code == 1
    result, report = _artifacts(run_dir)
    assert result["status"] == "FAIL"
    assert "final PVT sweep failed:" in result["failure_reason"]
    assert "could not run" not in result["failure_reason"]
    assert result["pvt_sweep"] is not None
    assert result["pvt_sweep_error"] is None
    assert "## PVT sweep" in report


def test_a_run_whose_sweeps_both_ran_says_so_with_a_null_error(tmp_path):
    """"확인했고 멀쩡했다"와 "그 기록이 사라졌다"가 같은 부재면 안 된다 -
    `optimize_guard_infeasible`이 이미 치른 값이다. 그래서 `pvt_sweep_error`는
    스윕이 멀쩡히 돈 실행에도 **null로 항상** 실린다."""
    run_dir = str(tmp_path / "runs" / "sweepok")
    args = _corner_args(tmp_path, CORNERS_BUT_REDUCTION_DISABLED_SPEC_YAML, run_dir)
    passing = _sweep({"gain": _wc("fs", 41.0), "pm": _wc("ss", 55.0)})

    with (
        patch("analogcoder.cli.run_full_pvt_sweep", new=_sweep_sequence([passing], [])),
        patch("analogcoder.cli.run_orchestration", new=_orchestration(_pass_result(run_dir))),
    ):
        code = _main_with(["--spec", args.spec, "--run-dir", run_dir])

    assert code == 0
    result, _ = _artifacts(run_dir)
    assert result["status"] == "PASS"
    assert "pvt_sweep_error" in result and result["pvt_sweep_error"] is None
